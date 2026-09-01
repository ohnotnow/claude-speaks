# CLAUDE.md

Notes for future Claude sessions working on `claude-speaks`. The README is the
user-facing description; this file is the things that aren't obvious from the
code and would otherwise need to be re-derived by reading every file.

## Shape of the project

After the provider-pluggable refactor:

```
main.py            ~85 lines  thin entry point: stdin → provider → audio
llm.py            ~100 lines  LLM(model).complete(system, user) — litellm, or Agent SDK for anthropic/ + OAuth token
audio.py          ~140 lines  stitch + play, archive rotation, word replacements
play_locked.py     ~45 lines  flock wrapper the player runs under: one playback at a time, machine-wide
config.py         ~110 lines  load_config, load_env_file, classifier_model, tts_provider, features, personas, notification_languages
logging_util.py    ~35 lines  log, trim_log, LOG_FILE, PROJECT_DIR
text_util.py       ~25 lines  strip_markdown, cap_length
history.py         ~25 lines  notification-history.txt read/append
prompts.py         ~75 lines  load_prompt(provider, name), load_persona(value) — local-first lookups + comment strip; safe_format
providers/
  base.py          ~75 lines  Provider abstract base + Clip dataclass + self.prompt() helper + gap_variant
  __init__.py      ~30 lines  auto-discovery via pkgutil.iter_modules
  mistral.py      ~235 lines  full Mistral implementation
  xai.py          ~195 lines  full xAI implementation
  openai.py       ~255 lines  OpenAI TTS implementation
  elevenlabs.py   ~170 lines  ElevenLabs implementation
  kokoro.py       ~330 lines  local Kokoro, no API key (cli subprocess or in-process mlx engine)
  README.md                   guide for adding a new provider
prompts/
  README.md                   pointer file for end-users (override mechanism)
  mistral/                    classifier.md, summary.md, preamble.md, notification.md
  xai/                        summary.md, preamble.md, notification.md
  openai/                     summary.md, preamble.md, notification.md
  elevenlabs/                 summary.md, preamble.md, notification.md
  kokoro/                     summary.md, preamble.md, notification.md (no {language} in notification — see below)
prompts.local/                gitignored; user overrides drop in here with the same layout
personas/
  README.md                   guide to the personas mechanism
  marvin.md                   default character description, slotted into {persona}
personas.local/               gitignored; user persona files drop in here (flat, no provider axis)
```

- Python 3.14, dependencies managed via `uv` (`pyproject.toml`, `uv.lock`).
  Run with `uv run main.py`. Third-party deps: `litellm`, `elevenlabs`,
  `claude-agent-sdk`, plus `mlx-audio` / `misaki[en]` / `soundfile` /
  `numpy` / the pinned `en-core-web-sm` wheel for kokoro's mlx engine.
  The agent SDK and the mlx stack are imported lazily: only anthropic/ +
  OAuth-token users pay the former's startup cost, only engine=mlx pays
  the latter's.
- Invoked by Claude Code as a hook — reads a JSON payload on stdin, returns
  nothing meaningful on stdout. The user wires it up in
  `~/.claude/settings.json` for two events: `Stop` and `Notification`.

## Two entry points, dispatched on `hook_event_name`

`main()` reads stdin → JSON → looks up the configured provider class from
`PROVIDERS` (auto-discovered from `providers/`) → instantiates it with
`(llm, api_key, settings, voices_config)` → dispatches:

- `Stop` → `handle_stop` → `provider.plan_stop_clips(text)` → `play_clips`.
- `Notification` → `handle_notification` → `provider.plan_notification_clip()`
  → `append_notification_history` → `play_clips`.

Anything else is logged as `<unhandled event>` and ignored.

## Provider interface

The single most important architectural fact: provider-specific behaviour
lives in `providers/<name>.py` plus `prompts/<name>/*.md`, not in
`main.py`. Each Python file subclasses `Provider` and implements
`plan_stop_clips`, `plan_notification_clip`, and `synthesise`. The system
prompts the provider sends to the LLM live as separate markdown files
under `prompts/<name>/`, accessed via `self.prompt("preamble")` etc.
`main.py` knows nothing about Mistral, xAI, classifiers, tag vocabularies,
or prompts — it just asks the provider for clips.

The contract is in `providers/base.py`. Worked examples:

- `providers/mistral.py` — **suffix-style**: voice id is a *prefix*, the
  classifier picks one of nine emotional styles and `_<style>` is appended
  (`gb_jane` → `gb_jane_neutral`). Three parallel LLM calls. Summariser
  only runs on long replies (>60 words). No prosody tags. `voice_for` is
  overridden to do the suffix.
- `providers/xai.py` — **inline-tag style**: voice id is *literal*, no
  classifier, the summariser embeds prosody tags (`<soft>`, `<emphasis>`,
  `<slow>`) directly. Two parallel LLM calls. Summariser runs every time.
  Uses base-class `voice_for`.
- `providers/kokoro.py` — **local-CLI style**: `api_key_env = None`,
  `synthesise` shells out to the `kokoro-tts` binary (temp dir in, mp3
  bytes out). No tags, no classifier; summariser is threshold-gated like
  Mistral's. Its mp3s are 24 kHz, so it sets `gap_variant = "24k"` (see
  the sample-rate section). ~13s wall for a two-clip stop event — each
  subprocess reloads the 310 MB onnx model, and since the hook is a fresh
  process per turn, an in-process library would pay the same load anyway.
  Model files (`kokoro-v1.0.onnx`, `voices-v1.0.bin`) live gitignored in
  the project root; paths configurable via `provider_settings.kokoro`,
  relative ones resolved against PROJECT_DIR, not CWD.
  Since 2026-09-01 kokoro has a second engine:
  `provider_settings.kokoro.engine = "mlx"` (the user's setting) runs
  mlx-audio in-process on the GPU instead. Same voice ids (blends are
  cli-only); model `mlx-community/Kokoro-82M-bf16` via `mlx_model`,
  auto-fetched to the HF cache on first use. Measured on the M1: 86
  CPU-seconds (cli) vs 7.5 (mlx) for a 90s clip, model load 0.5s vs ~7s.
  Facts that cost time to learn: generation is serialised under a
  module-level lock because the pipeline is not thread-safe and
  play_clips synthesises in parallel threads (at ~6x real time,
  sequential is cheap); output is soundfile-encoded 24 kHz mp3, so
  gap_variant is unchanged; and misaki's English G2P raises
  SystemExit, not an Exception, when the `en_core_web_sm` spacy model
  package is missing, because spacy.cli.download shells out to pip and
  uv venvs have no pip. That model wheel is therefore a pinned URL
  dependency in pyproject.toml, and the mlx synth path catches
  BaseException so the failure is logged and audible rather than a
  silently dead thread.

When adding a new provider, see `providers/README.md` for the contract,
the skeleton, and the gotchas.

## Parallel LLM calls

Each provider's `plan_stop_clips` owns its own fan-out shape. Mistral runs
three parallel calls (classifier, preamble, summariser) via
`ThreadPoolExecutor`. xAI runs two (preamble, summariser). A new provider
might run one, four, or none — that's its call. The clip TTS calls in
`audio.play_clips` also run in parallel.

Latency matters because Claude Code blocks on the hook returning —
unless the user has set `"async": true` on the hook definition (see
README → "Running the hook in the background"; the user runs it that
way for Kokoro). Don't assume async: not every install will set it, so
prefer extending the existing executor over adding a serial step.

## Voice resolution

The base class `Provider.voice_for(role, *, style=None)` reads
`self.voices_config[role]` from the user's config and falls back to
`self.default_voices[role]`. Both bare-string shorthand (`"main": "Eve"`)
and dict form (`"main": {"voice": "Eve", "language": "de"}`) are
accepted.

Mistral overrides `voice_for` to append `_<style>` when role is `main` and
style is non-neutral. xAI uses the base implementation untouched. Any
provider with a quirky voice scheme overrides this method.

`language_for(role)` matters to xAI and Kokoro — Mistral's `synthesise`
ignores `clip.language`. The base reads it from `voices_config[role].language`,
defaulting to `"en"`. Kokoro needs a concrete dialect, so it maps a bare
`"en"` to `provider_settings.kokoro.language` (default `en-gb`).

## Audio stitching has a sample-rate landmine

The `gaps/*.mp3` files are 22050 Hz mono. `afplay` will silently truncate
playback at a sample-rate boundary when concatenated mp3s disagree. The
xAI provider explicitly pins `sample_rate=22050, bit_rate=64000` in its
synth payload to match. Mistral's TTS happens to default to a matching
rate.

This is now every provider author's responsibility, not an internal
concern. The constraint is documented in `providers/README.md` and the
xAI provider exposes `sample_rate` / `bit_rate` overrides via
`provider_settings.xai` (see `config.example.json`). Any new provider
must either match the gaps or ship variant gap files (below).

For engines whose rate is fixed (Kokoro is always 24 kHz), the provider
sets the `gap_variant` class attribute and `audio.gap_blob(provider)`
stitches with `gaps/<gap_file>_<variant>.mp3` — the `*_24k.mp3` files
exist for all three durations, ffmpeg-rendered at `r=24000`. If a
variant file is missing, `gap_blob` returns `b""` (no gap) rather than
a wrong-rate gap, because clips butting together beats silent
truncation. User `gap_file` config still picks the duration; the
variant only swaps the rate.

Stitching itself is naive byte concatenation in `audio.play_clips`:
`audio_parts[0] + gap + part2 + gap + part3 + ...`. No ffmpeg, no
demuxing. That works only because the codec and sample rate match.
One cosmetic trap: Kokoro's mp3s carry a Xing/LAME header, so `afinfo`
reports a stitched file's duration as the *first clip only*. afplay
ignores the header and plays everything — verified empirically; do not
"fix" this.

## Playback is serialised machine-wide

Concurrent hook invocations (a second Claude session, a sub-agent
finishing while a long Kokoro summary is still talking) used to spawn
overlapping afplays. Now `audio.play_audio_file` never runs the player
directly: it spawns a detached `sys.executable play_locked.py <lock>
<player argv...>` child, which takes an exclusive `flock` on
`/tmp/claude-speaks-player.lock`, runs the player, and exits when
playback ends. Queue, not drop: a dropped clip would be a silent
failure, and the overlapping clip is usually news the user wants.

Facts to hold on to when touching this:

- The flock is kernel-owned and dies with the process (pkill, kill -9,
  crash: all release it). There is no stale-lock cleanup path because
  none is needed. Verified empirically: two parallel 2s players took 4s
  total; killing a lock-holding player freed the next within ~1s.
- macOS has the `flock()` syscall but no `flock(1)` binary, which is
  why the wrapper is Python, not a shell one-liner. It imports only
  stdlib plus `logging_util` (same directory), because it runs as a
  bare script under whatever `sys.executable` spawned it, outside the
  uv project.
- `killall afplay` (the panic-button script) only kills the *current*
  clip; a queued wrapper then acquires the lock and starts talking.
  The user knows and doesn't mind: their hotkey use is "not now"
  while sat reading the reply. README documents
  `pkill -f play_locked; killall afplay` for a full flush.
- The wrapper caps a single playback at 600s (`PLAYER_TIMEOUT_S`) so a
  wedged player can't hold the lock forever and silence the machine.
- `handsfree.maybe_arm` waits on the wrapper Popen, which is still
  correct (it exits when audio ends), but its 120s
  `PLAYBACK_TIMEOUT_S` now also covers time spent queued behind other
  sessions' clips. Expiry skips arming, which is the safe direction.
- The fallback Funk.aiff goes through the same lock, so even the
  failure beep queues rather than overlaps.

## Error handling philosophy

The hook is best-effort. The user is making coffee — silence is worse
than a slightly-wrong line. So:

- Every LLM-call wrapper inside a provider has a try/except that logs and
  returns `(fallback_value, err_label)`. `plan_stop_clips` collects the
  err_labels and prepends a spoken "Heads up — the X call fell over" so
  failures are *audible*, not silent.
- `Provider.synthesise` should catch its own HTTP/network errors and
  return `None`. `audio._safe_synthesise` is a defensive wrapper that
  catches anything the provider missed.
- If all TTS synthesis fails, `play_fallback_sound()` plays
  `/System/Library/Sounds/Funk.aiff` so the user at least hears
  *something*.
- `trim_log()` and `rotate_audio_archive()` are wrapped so housekeeping
  never breaks the hook.

When adding new code paths, preserve this — don't let an exception kill
the whole hook.

## Logging

Everything goes to `stop-hook.log` via `log()` (in `logging_util.py`).
Conventions:

- Tagged with `<category>` prefixes: `<stop>`, `<summary>`,
  `<notification>`, `<mistral synth>`, `<xai http error>`,
  `<config error>`, `<provider load error>`, etc. Search by tag when
  debugging.
- Auto-trimmed when the file exceeds 1 MB — keeps the most recent ~500 KB.
- The log is the primary debugging surface. There's no test suite;
  reading the log after a turn is how you confirm changes worked.

The last 10 turns are also archived as `/tmp/claude-speaks-<stamp>.{mp3,txt}`
pairs. The `.txt` records each clip's voice id and the exact text that was
synthesised — useful when a voice sounds wrong and you need to know what
was sent.

## Feature toggles

`config.json` has a `features` block with three booleans (defaults all `true`):
`monologue`, `main`, `notification`. They're loaded by `config.features()` and
threaded into the provider via `Provider.__init__(features=...)`.

- `notification` is enforced in `main.handle_notification` — if off, the
  handler returns before any LLM/TTS work. Providers don't need to know.
- `monologue` and `main` are enforced inside each provider's
  `plan_stop_clips`. Skip the corresponding LLM submission *and* the
  resulting clip; the point is to avoid paying for output you'll throw
  away. If both are off, return `[]`.
- The "Heads up — the X call fell over" prepend is only useful when there's
  a main clip to attach it to. If `main` is off, suppress the prepend.

When adding a new provider, copy the gating pattern from `mistral.py` /
`xai.py`. The base class defaults `self.features` to all-on so providers
that ignore the field still work.

## Config and env

- The dotfile lives next to `main.py` and is loaded by `load_env_file()`
  in `config.py` (a tiny hand-rolled parser, *not* python-dotenv). Keys
  are assigned unconditionally so **.env wins over inherited env** — the
  opposite of the python-dotenv default. Deliberate: a stale key exported
  in a shell profile once shadowed a freshly rotated key in `.env` and
  produced misleading billing errors from the old key's org.
- `config.json` is loaded fresh on every call to `load_config()` — there's
  no caching. Fine for a hook that runs once per turn; don't add caching
  unless you have a reason.
- `config.example.json` is the canonical template.
- `provider_settings.<name>` is the user-overrides slot for per-provider
  knobs; each provider reads its own slice via `self.settings`. xAI uses
  it for `sample_rate` / `bit_rate`.
- **Do not touch the dotfile yourself.** The user has been burned by
  leaked secrets. If you need a new env var documented, edit
  `dotenv.example` (or similar) and ask the user to copy values across.

## Claude subscription billing via the Agent SDK

`llm.py` has a second completion path: when the model starts with
`anthropic/` *and* `CLAUDE_CODE_OAUTH_TOKEN` is set (from
`claude setup-token`), `complete()` routes through the Claude Agent SDK
(`claude-agent-sdk`) instead of litellm, billing the user's Claude
subscription rather than API credit. `wants_agent_sdk(model)` is the
fork; non-anthropic models and API-key-only setups are byte-identical
to before.

Facts verified against SDK 0.2.123 source, not docs-from-memory:

- **Credential precedence is the whole game.** Claude Code checks
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` *before* the OAuth
  token, so both-set means silent API billing. `llm.auth_conflict()`
  detects this and `main.process_payload` refuses the event — logs
  `<auth guard>`, plays the fallback sound, returns. The guard lives in
  `process_payload` (not `main()`) so the `server.py` HTTP path is
  covered too, and *inside* the `config_overlay` block because a
  per-request overlay can switch `llm_model`. Deliberate scoping: it
  only trips when the model is `anthropic/` — both vars set while
  running a Mistral LLM is harmless.
- **`setting_sources=[]` is load-bearing.** The SDK default (`None`)
  loads *all* filesystem settings including `~/.claude/settings.json` —
  which contains the Stop hook that runs this very code. Recursion.
  Verified empirically that `[]` prevents the inner session firing the
  hook (no log entries from the inner turn). The SDK also strips
  `CLAUDECODE` from the child env itself, so nested-inside-a-session is
  handled upstream.
- **litellm calls `load_dotenv()` at import** (`litellm/__init__.py`),
  re-reading `.env` from CWD. Any "pop the key from `os.environ`, then
  proceed" reasoning breaks if the pop happens before litellm's import.
  This bit a test harness here; the production guard is unaffected
  because it never pops — it refuses.
- The SDK subprocess env is `os.environ` merged with
  `ClaudeAgentOptions.env` on top, so overrides *are* possible that
  way — unused here because the guard makes them unnecessary.
- Empty user turns: litellm's `modify_params` inserts `"."` as the
  placeholder user message for system-only anthropic calls; the SDK
  path mirrors that (`prompt=user or "."`).
- **`thinking={"type": "disabled"}` is load-bearing for latency.** The
  SDK defaults to adaptive thinking at high effort, which had Haiku
  spending 22-55s of hidden reasoning per 40-word summary (the log's
  `<agent sdk>` timing lines showed api_ms ≈ wall_ms, no retries — it
  really was all thinking). Disabled: ~2-3s per call, parallel calls
  fine. Diagnosed the hard way: it masqueraded first as CLI boot cost,
  then as per-account concurrency throttling (a serialising lock was
  added and reverted — the "tiny probe fast, real prompt slow" split
  was thinking depth tracking task difficulty, not queueing).
- Every SDK call logs `<agent sdk> ... api_ms= cli_ms= wall_ms=`:
  api_ms is time inside Anthropic's API, wall_ms - cli_ms ≈ CLI boot
  (~1.5-2s). If subscription turns get slow again, read those three
  numbers before theorising.
- Model self-reports are not evidence: the inner model cheerfully
  claimed it could see "memory files and project settings" when it
  couldn't (the log proved no settings loaded). Verify isolation via
  the log, not by asking the model.

## Markdown stripping is regex-based and crude

`strip_markdown()` (in `text_util.py`) does the obvious things (fenced
code blocks, links, backticks, headings, bullets) and accepts that some
markdown will leak through. The README calls this out as a known rough
edge. If a user reports TTS reading an asterisk aloud, that's the place
to look — but resist the urge to swap in a full markdown parser; the
unsubtlety is intentional.

## Notification history

`notification-history.txt` keeps the last 10 Marvin quips, fed back into
the prompt to suppress repetition. The provider reads via
`history.load_notification_history()` when building its prompt;
`main.handle_notification` calls `append_notification_history(clip.text)`
on the way out. Don't accidentally clobber this when changing
notification logic — it's load-bearing for variety.

## Prompt overrides

Prompts live as markdown files, not Python strings. `prompts.py`
exposes `load_prompt(provider, name)` which checks
`prompts.local/<provider>/<name>.md` first (gitignored user override),
falls back to `prompts/<provider>/<name>.md` (shipped default), and
strips a leading `<!-- ... -->` HTML-comment block from whichever it
loaded. Both missing → `FileNotFoundError` (install is broken).

`Provider.prompt(name)` on the base class wraps that. So inside a
provider, getting a system prompt looks like:

```python
self.llm.complete(self.prompt("summary"), user_text, max_tokens=16000)
```

Two consequences worth holding in mind:

- The shipped `prompts/<provider>/<name>.md` files open with a
  `<!-- ... -->` doc block describing the prompt's purpose, the method
  that calls it, and any `{placeholders}` it accepts. The block is
  stripped at load time so it never reaches the model. **HTML comments
  cannot contain a literal `-->` in their body** — the regex stops at
  the first one and you get a half-stripped prompt. Caught this once
  the hard way.
- Format-string substitution on the notification prompt uses
  `prompts.safe_format(template, **kwargs)`, which falls back to the
  raw template on any `KeyError`/`IndexError`/`ValueError` and logs
  `<prompt format error>`. This is for when a user's custom prompt has
  a stray `{` that isn't a real placeholder — keeps the hook running
  rather than crashing the event.

`notification_languages` (the weighted list of languages the idle quip
can be generated in) is no longer a per-provider Python constant. It
moved to `config.json` (read via `config.notification_languages()`),
defaulting to the original seven-language list if the key is missing
or malformed. To get English-only quips, the user sets
`"notification_languages": [["English", 1]]`. Kokoro deliberately
ignores this setting — its voices are single-language and phonemise
other scripts into garbage — so its `notification.md` has no
`{language}` placeholder and quips default to English (the prompt's
comment block tells users how to change that).

## Personas

A lighter-weight customisation layer than `prompts.local/`. The
character speaking each role lives in `config.json` under `personas`;
each value resolves via `prompts.load_persona(value)`:

1. If `personas.local/<value>.md` exists, load it.
2. Else if `personas/<value>.md` exists, load it.
3. Otherwise, treat the value as a freeform character description and
   pass it through verbatim.

The resolution rule is deliberately dumb — no slug regex, no
normalisation. A user writing `"monologue": "marvin"` expects a file
lookup; one writing `"monologue": "a wildly excited pantomime dame"`
expects passthrough. Both are predictable from glancing at the config.

`config.personas()` reads with defaults `{monologue: marvin,
notification: marvin, main: None}`. The base `Provider.persona(role)`
wraps this and returns the resolved text (or `None`).

Three roles, two verbs:

- `monologue` and `notification` — the LLM **adopts** the character.
  Each provider's `preamble.md` / `notification.md` accepts a
  `{persona}` placeholder, slotted in via `safe_format`. Pass
  `persona=self.persona(role) or ""` so a `None` value coerces to
  empty rather than stringifying.
- `main` — the summariser **preserves** an existing voice. Implemented
  as a runtime *append* to the summariser system prompt inside
  `reformat_text()`, not a template placeholder. When
  `self.persona("main")` is `None` (the default), nothing is appended
  and behaviour is byte-identical to before personas existed.

The asymmetry is intentional: `summary.md` files are persona-neutral
by design and a placeholder there would risk drifting from
byte-identical defaults. Appending only on opt-in does not.

Backward compatibility falls out of `safe_format`: a user with their
own `prompts.local/<provider>/preamble.md` that doesn't include
`{persona}` keeps working untouched — the extra kwarg is silently
ignored. And anyone who hasn't customised anything gets byte-identical
output because the persona defaults load Marvin's deduplicated
paragraph back into the same slot.

Known foot-gun, documented not fixed: OpenAI users who change the
persona but leave `voices.openai.<role>.instructions` at the default
get a non-Marvin **prompt** delivered in Marvin's **voice** (the TTS
`instructions` field is a separate surface from the LLM prompt and
drifts independently). Mentioned in `prompts/openai/preamble.md` and
`personas/README.md`.

## Things to be careful about when editing

- Changing the prompt files in `prompts/<provider>/` directly affects
  what the user hears. The examples inside the prompts are tuned —
  don't casually rewrite them. If you do edit one, remember the user's
  `prompts.local/<provider>/<name>.md` (if they have one) wins, so a
  default change won't help anyone who's already overridden it.
- The "always return a complete grammatical phrase, never stop mid-sentence
  to meet a word count" guidance in the prompts is deliberate. The user
  has noticed truncated lines before and asked for this.
- `MAX_SPEAK_CHARS = 800` (in `text_util.py`) is the safety net on
  *every* clip sent to TTS — main, preamble, and notification. It is
  the only thing bounding TTS spend: the LLM `max_tokens` budgets are
  deliberately huge (16000 summariser / 4000 elsewhere) because
  reasoning models spend from that budget on hidden reasoning before
  emitting text — small budgets caused systematic summariser failures
  on long replies, and the raw reply got spoken instead. Each provider
  applies `cap_length(...)` to every clip it builds; keep that when
  adding clips. One deliberate exception: Kokoro's main clip caps at
  `provider_settings.kokoro.max_speak_chars` (default 3000) because
  local synthesis is free and its summary prompt is two-mode: routine
  replies still compress to under 80 words, but replies carrying
  technical substance (trade-offs, decisions, caveats) may run to 250
  words. (First cut of that mode allowed 400 and briefed "walk through
  the options"; with a worked example retaining ~75% of its input, it
  produced near-full rewrites, 207 words spoken for a 294-word reply.
  Retuned 2026-08-11: keep the decision, sharpest caveat, and question;
  drop the evidence trail. The worked example is the strongest lever —
  the model copies its retention ratio.) See `prompts/kokoro/summary.md`.
  A side effect: if the
  summariser call fails, the raw-reply fallback is also spoken up to
  3000 chars, not 800.
- The preamble clip is tighter: `cap_length(..., MAX_MONOLOGUE_CHARS)`
  (200 chars), and the preamble LLM output goes through `first_line()`
  before the strip chain. gpt-5.x occasionally *answers the reply*
  instead of writing the one-line quip — both observed cases were
  triggered by a reply ending in a direct question to the user, and the
  babble is always multi-line with a usable quip as its first line.
  (Mistral models followed "up to 10 words" to the letter, which is why
  this never surfaced before the model switch.) When the guard fires it
  logs `<preamble guard>`; keep that log line — it's the only evidence
  the slip happened.
- New TTS providers go in `providers/<name>.py` *and* a matching
  `prompts/<name>/` directory of default prompts. See
  `providers/README.md` for the contract, the worked examples, and the
  sample-rate constraint.

## Conventions the user cares about

- British English in user-visible text (the README and prompts already
  reflect this).
- Simple solutions over clever ones. Each module is small on purpose;
  resist abstracting "for symmetry".
- No git commits, pushes, or branch operations unless explicitly asked.
