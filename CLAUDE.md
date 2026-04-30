# CLAUDE.md

Notes for future Claude sessions working on `claude-speaks`. The README is the
user-facing description; this file is the things that aren't obvious from the
README and would otherwise need to be re-derived by reading `main.py` end to
end.

## Shape of the project

- Single-file hook: `main.py` (~770 lines). Everything lives there. No package
  layout, no tests.
- Python 3.14, dependencies managed via `uv` (`pyproject.toml`, `uv.lock`).
  Run with `uv run main.py`. Only third-party dep is `litellm`.
- Invoked by Claude Code as a hook — reads a JSON payload on stdin, returns
  nothing meaningful on stdout. The user wires it up in
  `~/.claude/settings.json` for two events: `Stop` and `Notification`.

## Two entry points, dispatched on `hook_event_name`

`main()` reads stdin → JSON → dispatches:

- `Stop` → `handle_stop` — Jane reads the final assistant reply, with a Marvin
  preamble in front.
- `Notification` → `handle_notification` — Marvin alone, a one-liner in a
  randomly-weighted language.

Anything else is logged as `<unhandled event>` and ignored.

## Two TTS providers, different code paths

The single most important architectural fact: `tts_provider()` (`mistral` or
`xai`, from `config.json`) changes behaviour in several places. When editing,
think about both paths:

- **Mistral** — voice id is a *prefix*; the classifier picks one of nine
  emotional styles and `_<style>` is appended (`gb_jane` → `gb_jane_neutral`).
  The summariser only runs on long replies (>60 words). No prosody tags.
- **xAI** — voice id is *literal*. The classifier is skipped entirely. The
  summariser runs on every reply (even short ones) so it can embed inline
  prosody tags (`<soft>`, `<emphasis>`, `<slow>`, …). Two parallel LLM calls
  instead of three.

The branching lives in `handle_stop`, `summarise_for_tts`, `voice_for`,
`generate_stop_preamble`, and the synth dispatcher.

## Parallel LLM calls

`handle_stop` runs LLM work concurrently via `ThreadPoolExecutor`. On Mistral
it's three calls (classifier, preamble, summariser); on xAI it's two
(preamble, summariser). The clip TTS calls in `play_clips` also run in
parallel.

If you add another LLM-driven feature to the Stop path, prefer extending the
existing executor rather than adding a serial step — latency matters because
Claude Code blocks on the hook returning.

## Voice resolution

`_voice_block(role)` → `voice_for(role)` → `language_for(role)`. Roles are
`main`, `monologue`, `notification`. The fallback chain is:

- `notification` falls back to `monologue`
- `monologue` falls back to `main` (with `_sarcasm` suffix on Mistral)
- `main` falls back to `DEFAULT_MAIN_VOICE[provider]`

A bare string under a role is shorthand for `{"voice": "..."}`. Languages are
xAI-only — Mistral ignores them.

## Audio stitching has a sample-rate landmine

The `gaps/*.mp3` files are 22050 Hz mono / 56 kbps. `afplay` will silently
truncate playback at a sample-rate boundary when concatenated mp3s disagree.
That's why `_synthesise_xai` explicitly requests `sample_rate: 22050,
bit_rate: 64000` — match the gaps. Any new gap mp3 or any new TTS provider
must keep this constraint or the second clip vanishes mid-playback with no
error.

Stitching is naive byte concatenation: `audio_parts[0] + gap + part2 + gap +
part3 + ...`. No ffmpeg, no demuxing. That works only because the codec and
sample rate match.

## Error handling philosophy

The hook is best-effort. The user is making coffee — silence is worse than a
slightly-wrong line. So:

- Every LLM call has a try/except that logs and returns `(fallback, err_name)`.
- If any of the three (classifier/preamble/summariser) fails, a spoken
  "Heads up — the X call fell over" is prepended to the reply so failures are
  *audible*, not silent.
- If all TTS synthesis fails, `play_fallback_sound()` plays
  `/System/Library/Sounds/Funk.aiff` so the user at least hears *something*.
- `trim_log()` and `rotate_audio_archive()` are wrapped so housekeeping never
  breaks the hook.

When adding new code paths, preserve this — don't let an exception kill the
whole hook.

## Logging

Everything goes to `stop-hook.log` via `log()`. Conventions:

- Tagged with `<category>` prefixes: `<stop>`, `<summary>`, `<notification>`,
  `<mistral synth>`, `<xai http error>`, `<config error>`, etc. Search by tag
  when debugging.
- Auto-trimmed when the file exceeds 1 MB — keeps the most recent ~500 KB.
- The log is the primary debugging surface. There's no test suite; reading
  the log after a turn is how you confirm changes worked.

The last 10 turns are also archived as `/tmp/claude-speaks-<stamp>.{mp3,txt}`
pairs. The `.txt` records each clip's voice id and the exact text that was
synthesised — useful when a voice sounds wrong and you need to know what was
sent.

## Config and env

- `.env` lives next to `main.py` and is loaded by `load_env_file()` (a tiny
  hand-rolled parser, *not* python-dotenv). Keys are `os.environ.setdefault`'d
  so existing env wins.
- `config.json` is loaded fresh on every call to `load_config()` — there's
  no caching. Fine for a hook that runs once per turn; don't add caching
  unless you have a reason.
- `config.example.json` is the canonical template.
- **Do not touch `.env` files yourself.** The user has been burned by leaked
  secrets. If you need a new env var documented, edit `dotenv.example` (or
  similar) and ask the user to copy values across.

## Markdown stripping is regex-based and crude

`strip_markdown()` does the obvious things (fenced code blocks, links,
backticks, headings, bullets) and accepts that some markdown will leak
through. The README calls this out as a known rough edge. If a user reports
TTS reading an asterisk aloud, that's the place to look — but resist the urge
to swap in a full markdown parser; the unsubtlety is intentional.

## Notification history

`notification-history.txt` keeps the last 10 Marvin quips, fed back into the
prompt to suppress repetition. Don't accidentally clobber this when changing
notification logic — it's load-bearing for variety.

## Things to be careful about when editing

- Changing the prompt strings (`SUMMARY_PROMPT`, `XAI_SUMMARY_PROMPT`,
  `STOP_PREAMBLE_PROMPT`, etc.) directly affects what the user hears. The
  examples inside the prompts are tuned — don't casually rewrite them.
- The "always return a complete grammatical phrase, never stop mid-sentence
  to meet a word count" guidance in the prompts is deliberate. The user has
  noticed truncated lines before and asked for this.
- `MAX_SPEAK_CHARS = 800` is the safety net after the summariser. The
  summariser usually keeps things well under it; the cap is for when the
  summariser fails or is skipped.
- New TTS providers need: a `_synthesise_<name>` function, dispatch in
  `synthesise()`, an entry in `DEFAULT_MAIN_VOICE`, an env-var branch in
  `tts_api_key()`, and (critically) matching sample rate against the gap
  mp3s.

## Conventions the user cares about

- British English in user-visible text (the README and prompts already
  reflect this).
- Simple solutions over clever ones. The whole project is roughly 770 lines
  on purpose. Resist abstracting "for symmetry".
- No git commits, pushes, or branch operations unless explicitly asked.
