# CLAUDE.md

Notes for future Claude sessions working on `claude-speaks`. The README is the
user-facing description; this file is the things that aren't obvious from the
code and would otherwise need to be re-derived by reading every file.

## Shape of the project

After the provider-pluggable refactor:

```
main.py            ~85 lines  thin entry point: stdin → provider → audio
llm.py             ~30 lines  LLM(model).complete(system, user) — wraps litellm
audio.py          ~135 lines  stitch + play, archive rotation, word replacements
config.py          ~45 lines  load_config, load_env_file, classifier_model, tts_provider
logging_util.py    ~35 lines  log, trim_log, LOG_FILE, PROJECT_DIR
text_util.py       ~25 lines  strip_markdown, cap_length
history.py         ~25 lines  notification-history.txt read/append
providers/
  base.py          ~55 lines  Provider abstract base + Clip dataclass
  __init__.py      ~30 lines  auto-discovery via pkgutil.iter_modules
  mistral.py      ~275 lines  full Mistral implementation
  xai.py          ~230 lines  full xAI implementation
  README.md                   guide for adding a new provider
```

- Python 3.14, dependencies managed via `uv` (`pyproject.toml`, `uv.lock`).
  Run with `uv run main.py`. Only third-party dep is `litellm`.
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
lives in `providers/<name>.py`, not in `main.py`. Each file subclasses
`Provider` and implements `plan_stop_clips`, `plan_notification_clip`, and
`synthesise`. `main.py` knows nothing about Mistral, xAI, classifiers, tag
vocabularies, or prompts — it just asks the provider for clips.

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

When adding a new provider, see `providers/README.md` for the contract,
the skeleton, and the gotchas.

## Parallel LLM calls

Each provider's `plan_stop_clips` owns its own fan-out shape. Mistral runs
three parallel calls (classifier, preamble, summariser) via
`ThreadPoolExecutor`. xAI runs two (preamble, summariser). A new provider
might run one, four, or none — that's its call. The clip TTS calls in
`audio.play_clips` also run in parallel.

Latency matters because Claude Code blocks on the hook returning. Prefer
extending the existing executor over adding a serial step.

## Voice resolution

The base class `Provider.voice_for(role, *, style=None)` reads
`self.voices_config[role]` from the user's config and falls back to
`self.default_voices[role]`. Both bare-string shorthand (`"main": "Eve"`)
and dict form (`"main": {"voice": "Eve", "language": "de"}`) are
accepted.

Mistral overrides `voice_for` to append `_<style>` when role is `main` and
style is non-neutral. xAI uses the base implementation untouched. Any
provider with a quirky voice scheme overrides this method.

`language_for(role)` is xAI-only in practice — Mistral's `synthesise`
ignores `clip.language`. The base reads it from `voices_config[role].language`,
defaulting to `"en"`.

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
must either match the gaps or replace the gap mp3s and document the
new rate.

Stitching itself is naive byte concatenation in `audio.play_clips`:
`audio_parts[0] + gap + part2 + gap + part3 + ...`. No ffmpeg, no
demuxing. That works only because the codec and sample rate match.

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

## Config and env

- The dotfile lives next to `main.py` and is loaded by `load_env_file()`
  in `config.py` (a tiny hand-rolled parser, *not* python-dotenv). Keys
  are `os.environ.setdefault`'d so existing env wins.
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

## Things to be careful about when editing

- Changing the prompt strings in any provider file directly affects what
  the user hears. The examples inside the prompts are tuned — don't
  casually rewrite them.
- The "always return a complete grammatical phrase, never stop mid-sentence
  to meet a word count" guidance in the prompts is deliberate. The user
  has noticed truncated lines before and asked for this.
- `MAX_SPEAK_CHARS = 800` (in `text_util.py`) is the safety net after the
  summariser. The summariser usually keeps things well under it; the cap
  is for when the summariser fails or is skipped. Each provider applies
  it inside `plan_stop_clips` via `cap_length(...)`.
- New TTS providers go in `providers/<name>.py`. See `providers/README.md`
  for the contract, the worked examples, and the sample-rate constraint.

## Conventions the user cares about

- British English in user-visible text (the README and prompts already
  reflect this).
- Simple solutions over clever ones. Each module is small on purpose;
  resist abstracting "for symmetry".
- No git commits, pushes, or branch operations unless explicitly asked.
