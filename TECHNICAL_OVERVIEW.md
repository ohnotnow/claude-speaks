# Technical Overview

Last updated: 2026-07-24

## What This Is

`claude-speaks` is a Python hook that turns Claude Code `Stop` and `Notification` events into spoken audio, optionally accepting the same events over HTTP from remote Claude Code or Hermes sessions.

## Stack

- Python 3.14+; dependencies and the virtual environment are managed with `uv`.
- Direct dependencies: LiteLLM 1.83.8, ElevenLabs 2.45.0, and Claude Agent SDK 0.2.123 (versions currently locked in `uv.lock`).
- Standard-library `http.server` provides remote mode; there is no database or web framework.
- Cloud TTS backends: Mistral, xAI, OpenAI, and ElevenLabs. Kokoro is a local CLI backend.
- Audio playback defaults to macOS `afplay`; `player_command` makes Linux players usable.

## Architecture at a Glance

```text
Claude Code hook stdin                 Remote/Hermes client
          |                              POST /hook + Bearer token
          v                                         |
       main.py <-------------------- server.py ------+
          |
          +-- load .env, config.json, request overlay
          +-- validate LLM auth and discover TTS provider
          +-- Stop: strip markdown -> provider.plan_stop_clips()
          |            -> [monologue Clip, main Clip]
          +-- Notification: provider.plan_notification_clip()
                       |
                       v
             audio.play_clips()
          parallel TTS -> word replacements -> MP3 gap stitching
                       -> /tmp archive -> detached audio player
                       -> optional hands-free microphone arm
```

`Clip` is the central value object: `text`, `voice`, `language`, and optional TTS `instructions`. Providers plan clips and synthesise each clip; the shared audio layer owns playback.

## Directory Structure

```text
main.py                 Local hook entry point and shared event dispatcher
server.py               Authenticated, fire-and-forget HTTP receiver
config.py               Defaults, `.env`, JSON config, thread-local overlays
llm.py                  LiteLLM / Claude Agent SDK completion adapter
audio.py                Parallel synthesis, MP3 stitching, archive, playback
handsfree.py            Optional claude-listens microphone handoff
providers/              Provider contract, discovery, and five TTS backends
prompts/<provider>/     Shipped provider-specific LLM system prompts
prompts.local/          Gitignored user prompt overrides (optional)
personas/               Shipped reusable character descriptions
personas.local/         Gitignored user personas (optional)
gaps/                   22.05 kHz and 24 kHz silent MP3 separators
scripts/                Remote client, server test client, services, Hermes plugin
config.example.json     Complete example and canonical configuration shape
dotenv.example          API key and remote-token reference
```

## Event and Runtime Flow

1. `main.main()` loads `.env` without overriding exported environment variables, trims the log, parses stdin JSON, then calls `process_payload()`.
2. `process_payload()` removes an optional `claude_speaks` object and deep-merges it over `config.json` for this thread and request only.
3. It rejects conflicting Anthropic subscription/API credentials, looks up the configured provider in the auto-discovered `PROVIDERS` map, and resolves its API key.
4. `Stop` strips Markdown and fenced code, asks the provider to plan enabled monologue/main clips, plays them, then optionally hands control to `claude-listens`.
5. `Notification` generates one history-informed quip when enabled, appends it to the ten-line history, and plays it. Generation failure triggers the fallback sound.
6. Unknown events are logged and ignored. The hook produces no meaningful stdout response.

Providers run independent LLM work concurrently: Mistral uses up to three calls (tone, preamble, summary); the other backends use up to two. `audio.py` then synthesises all planned clips concurrently. Feature-disabled stages incur neither LLM nor TTS work.

## Provider Model

Every `providers/*.py` module is imported at startup. A module-level `PROVIDER` subclass is registered by its `name`; import failures are logged and skipped. The contract in `providers/base.py` requires:

- `name`, `api_key_env`, and role-based `default_voices`.
- `plan_stop_clips(text)`, `plan_notification_clip()`, and `synthesise(clip)`.
- Matching shipped prompts under `prompts/<name>/` for every prompt requested.

| Provider | Key | Planning behavior | TTS/output details |
|---|---|---|---|
| Mistral | `MISTRAL_API_KEY` | Classifies nine emotional styles; summarises only over 60 words | Voxtral; appends style to the main voice prefix |
| xAI | `XAI_API_KEY` | Always reformats so it can add inline prosody tags | Literal voices; configurable 22.05 kHz/64 kbps |
| OpenAI | `OPENAI_API_KEY` | Summarises only over 60 words; carries role instructions | `gpt-4o-mini-tts`; configurable 22.05 kHz/56 kbps |
| ElevenLabs | `ELEVENLABS_API_KEY` | Summarises only over 60 words | SDK; defaults to `eleven_v3`, `mp3_22050_32` |
| Kokoro | none | Summarises only over 60 words; notifications stay single-language | Local `kokoro-tts`; fixed 24 kHz and `*_24k.mp3` gaps |

All providers cap main text at 800 characters and monologues at 200. Failed LLM stages fall back to raw text and prepend an audible warning where possible; partial TTS success still plays successful clips.

## Configuration

Copy `config.example.json` to the gitignored `config.json`. Resolution order is:

```text
built-in defaults <- config.json <- payload.claude_speaks deep overlay
```

Nested overlay objects merge key-by-key; arrays and scalar values replace. Event-time configuration is read fresh, so most `config.json` edits and request overlays do not require a restart; listener host/port are read when `server.py` starts. Relative paths are generally project-relative where documented; the player and fallback paths are passed to the OS.

| Key | Default | Purpose |
|---|---|---|
| `llm_model` | `mistral/mistral-small-latest` | LiteLLM model for planning; `anthropic/...` can use the Agent SDK |
| `tts_provider` | `mistral` | Provider registry name |
| `features` | all three `true` | Toggle `monologue`, `main`, and `notification` independently |
| `personas` | Marvin/Marvin/null | Prompt character for monologue, notification, and main-summary preservation |
| `voices` | provider defaults | Per-provider `main`, `monologue`, `notification` voice settings |
| `provider_settings` | `{}` | Backend-specific model, output, path, rate, speed, and timeout settings |
| `notification_languages` | weighted seven-language list | LLM language selection for cloud-provider notifications |
| `gap_file` | `0_75s` | Separator basename from `gaps/`, without `.mp3` |
| `word_replacements` | `{}` | Case-insensitive phonetic substitutions immediately before TTS |
| `player_command` | `afplay` | String (shell-split) or argv array; audio path is appended |
| `fallback_sound` | macOS `Funk.aiff` | Played when generation or every synthesis path fails |
| `server.host` / `.port` | `127.0.0.1` / `8765` | Remote listener address |
| `handsfree_arm_command` | unset | Non-empty argv array used to arm claude-listens |

Voice values may be a string or `{ "voice": "...", "language": "..." }`. OpenAI also reads per-role `instructions`. Mistral treats the main voice as a prefix for its classifier suffix; other providers use literal voice IDs.

Prompts resolve from `prompts.local/<provider>/<name>.md` before shipped `prompts/`. Personas similarly resolve from `personas.local/<name>.md`, then `personas/<name>.md`, then use the configured value as freeform text. A leading HTML comment is stripped from both.

## Environment and Authentication

Copy `dotenv.example` to `.env`. Existing process environment values win over `.env` because loading uses `os.environ.setdefault()`.

- Set the selected TTS backend key plus any key required by `llm_model`.
- For `anthropic/...`, `CLAUDE_CODE_OAUTH_TOKEN` routes completions through the Claude Agent SDK. Do not also set `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`; the dispatcher refuses the event to prevent unintended API billing.
- `CLAUDE_SPEAKS_TOKEN` is required by `server.py` and remote clients. The server compares Bearer tokens with `hmac.compare_digest`.
- `CLAUDE_SPEAKS_URL` and `CLAUDE_SPEAKS_OVERRIDES` configure `scripts/remote-hook.py`.
- Hermes prefers `HERMES_SPEAKS_URL`, `_TOKEN`, and `_OVERRIDES`, falling back to the Claude names. Its default override disables monologue and notification.

## HTTP Interface

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /health` | None | Returns `200 ok`; confirms only that the server is listening |
| `POST /hook` | Bearer token | Accepts a JSON object up to 1 MiB, starts a daemon worker, returns `202` |

`POST /hook` expects the same shape as the local hook: `hook_event_name`, `last_assistant_message` for `Stop`, optional `session_id`, and optional `claude_speaks`. Bind beyond loopback only on a trusted LAN/VPN; the protocol is plain HTTP and has no rate limiting.

## Audio, Files, and Failure Handling

- Successful audio is written as `/tmp/claude-speaks-<timestamp>.mp3` with a companion `.txt` containing voice and final spoken text. The newest ten pairs are retained.
- Stitching is raw MP3 byte concatenation, not decoding/remuxing. The provider output must match its gap sample rate. A missing provider-specific gap produces no silence rather than risking truncated playback.
- Playback is a detached subprocess. Asynchronous Claude hooks can overlap and speak simultaneously; `scripts/shut-marvin-up.sh` stops `afplay` playback.
- `stop-hook.log` is project-local, grows to 1 MB, then retains roughly the newest 500 KB. Provider imports, request payloads/overrides, voice choices, timings, and failures are logged.
- `notification-history.txt` retains ten generated lines to reduce repetition. Both runtime files are gitignored.

## Optional Hands-Free Loop

After a `Stop` clip starts, `handsfree.maybe_arm()` activates only when `~/.claude-voice/handsfree` exists, a session ID and player process exist, `handsfree_arm_command` is configured, and a matching claude-listens registry entry is found. It waits for playback, writes a one-shot reply target, then arms the microphone. Any failure is logged and cannot interrupt speech; failed arming removes the stale target.

## Local Development and Operations

```bash
uv sync                         # install locked dependencies
cp dotenv.example .env          # add only the credentials in use
cp config.example.json config.json
uv run main.py                  # reads one hook JSON object from stdin
uv run server.py                # start authenticated remote mode
uv run scripts/poke-server.py --health
uv run scripts/poke-server.py -m "Can you hear me?"
```

Configure Claude Code `Stop` and `Notification` hooks to run `uv run --project /absolute/path /absolute/path/main.py`. Adding `"async": true` avoids blocking Claude Code, especially with Kokoro, but permits overlapping playback.

The repository ships editable launchd and systemd user-service templates in `scripts/`. Use a user service so the process has access to the logged-in audio session. On Linux, replace `afplay` and usually the fallback sound.

## Testing and Extension Points

There is currently no automated test suite or configured test framework. Validate changes with direct stdin payloads and `scripts/poke-server.py`; provider changes should exercise `Stop`, `Notification`, disabled feature stages, partial failures, and matching gap/sample rates.

To add a provider, implement the base contract in one new module, export `PROVIDER`, add its prompt files, and document any `.env` key and `provider_settings`. No central registry edit is needed. Preserve the best-effort rule: one external failure should be logged, audible where possible, and should not break the calling agent.
