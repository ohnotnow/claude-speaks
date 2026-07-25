# claude-speaks

**Work in progress.** A small experiment: have Claude Code read its final
reply out loud.

It's a [Claude Code Stop hook](https://docs.claude.com/en/docs/claude-code/hooks)
that:

1. Catches the `last_assistant_message` when Claude finishes a turn.
2. Strips markdown and drops fenced code blocks.
3. Hands the text to the configured TTS provider, which fans out
   parallel LLM calls via LiteLLM (model configurable — see
   Configuration) to produce a Marvin-the-Paranoid-Android-style sigh
   and a TTS-friendly version of the reply.
4. Synthesises two TTS clips — Marvin's sigh in a monologue voice, then
   the reply in the main voice.
5. Stitches them with a short silent mp3 gap and plays the result via
   `afplay`.

Internals live in [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md).

There's also a Notification handler for when Claude Code is idle or
waiting for permission — a short weary Marvin quip in the notification
voice. A rolling history of recent lines is fed back into the prompt to
stop him repeating himself, and the language is picked at random from a
weighted list in `config.json` - set it to a single language if
multilingual Marvin isn't your thing. See
[Customising the personality](#customising-the-personality).

If any of the LLM calls fail, the hook prepends a short spoken heads-up
("heads up — the summariser call fell over, raw reply coming up") before
the reply.

## Requirements

- macOS (uses `afplay` for playback by default; override via `player_command` in `config.json` if you're on something else)
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- An API key for whichever TTS provider you've configured (Mistral, xAI,
  OpenAI, ElevenLabs, and Kokoro ship in the box; others can be dropped
  into `providers/` — see [providers/README.md](providers/README.md)).
  Kokoro is the odd one out: it does text-to-speech on your own machine,
  so no API key — see [Switching to Kokoro](#switching-to-kokoro-local-free)

## Setup

```bash
git clone git@github.com:ohnotnow/claude-speaks.git
cd claude-speaks
uv sync
```

Create a `.env` file with the API key for whichever TTS provider you're
using (and any other provider keys you need for `llm_model`):

```
MISTRAL_API_KEY=your-key-here
# or, if you've set tts_provider to "xai":
# XAI_API_KEY=xai-...
```

Copy the example config and tweak to taste:

```bash
cp config.example.json config.json
```

Then wire it up as a Stop hook in your Claude Code settings
(`~/.claude/settings.json`):

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run --project /path/to/claude-speaks /path/to/claude-speaks/main.py"
          }
        ]
      }
    ]
  }
}
```

Restart your Claude Code session and Claude should start speaking back.

### Running the hook in the background

Claude Code normally waits for a hook to finish before handing the
session back to you — and this hook does real work (LLM calls, then TTS
synthesis) before it returns. Depending on your machine and provider,
latency can be a problem however. Adding `"async": true` to the hook
definition tells Claude Code to fire the hook and move on:

```json
{
  "type": "command",
  "command": "uv run --project /path/to/claude-speaks /path/to/claude-speaks/main.py",
  "async": true
}
```

Strongly recommended for Kokoro.

One gotcha: with `async` on, nothing stops a quick follow-up turn
kicking off a second run while the first is still talking, so very
occasionally you'll hear two streams of audio at once. The
`killall afplay` panic button in
[Shutting Marvin up mid-sentence](#shutting-marvin-up-mid-sentence)
silences both.

## Remote mode (Raspberry Pi → Mac)

If you run Claude Code on a headless box (a Raspberry Pi left ticking
away, a remote server you've SSH'd into, etc.) but want the audio out
of your Mac's speakers, `server.py` gives you a small HTTP shim.

### On the Mac (server side)

1. Pick a shared secret and add it to `.env`:

   ```
   CLAUDE_SPEAKS_TOKEN=long-random-string-here
   ```

   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`
   generates a sensible one.)

2. Optionally tweak `server.host` / `server.port` in `config.json`
   (defaults: `127.0.0.1:8765`). For LAN access, bind to your LAN IP
   or `0.0.0.0`; if your machines share a Tailscale / WireGuard mesh,
   bind to that interface instead and keep it off the open network.

3. Start the server:

   ```bash
   uv run server.py
   ```

   It logs to the same `stop-hook.log` as the local hook, tagged with
   `<server ...>`.

#### Running it as a service

Running `uv run server.py` in a terminal works fine, but is a faff if
you want the server up across reboots. The repo ships example service
files for both platforms — they need a couple of path edits before
they'll work, so read the comments at the top of each file before
loading it.

**macOS (launchd LaunchAgent):**

```bash
# Edit the two YOUR_USER placeholders and project path inside the plist first.
cp scripts/com.claude-speaks.server.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-speaks.server.plist
```

It's a LaunchAgent (per-user) rather than a LaunchDaemon — daemons run
before login and don't have access to your audio session, so `afplay`
would produce silence. Reload after editing with
`launchctl kickstart -k gui/$(id -u)/com.claude-speaks.server`; stop
with `launchctl bootout gui/$(id -u)/com.claude-speaks.server`.

**Linux (systemd user unit):**

```bash
mkdir -p ~/.config/systemd/user
cp scripts/claude-speaks-server.service ~/.config/systemd/user/
# Edit the %h-relative paths inside the file.
systemctl --user daemon-reload
systemctl --user enable --now claude-speaks-server.service
```

It's a user unit, not a system one, for the same reason — the system
PID-1 has no PulseAudio / PipeWire session to play into. If you want
the server to keep running while you're logged out, `loginctl
enable-linger "$USER"`. Logs land in the journal:
`journalctl --user -u claude-speaks-server -f`.

Heads up for Linux: the default `player_command` is `afplay`, which
isn't on Linux. Set `player_command` (and likely `fallback_sound`) in
`config.json` before the unit will actually produce noise — see the
[Configuration](#configuration) table.

### On the Pi (client side)

The repo ships `scripts/remote-hook.py` — stdlib-only Python. On the Pi:

1. Clone the repo (or just copy that one script).
2. Set two env vars where Claude Code will see them (e.g. in
   `~/.profile` or a wrapper):

   ```
   export CLAUDE_SPEAKS_TOKEN=same-string-as-on-the-mac
   export CLAUDE_SPEAKS_URL=http://your-mac.local:8765/hook
   ```

3. Wire the script as the Stop / Notification hook in
   `~/.claude/settings.json` on the Pi:

   ```json
   {
     "hooks": {
       "Stop": [
         { "matcher": "", "hooks": [
           { "type": "command", "command": "/usr/bin/env python3 /path/to/claude-speaks/scripts/remote-hook.py" }
         ]}
       ],
       "Notification": [
         { "matcher": "", "hooks": [
           { "type": "command", "command": "/usr/bin/env python3 /path/to/claude-speaks/scripts/remote-hook.py" }
         ]}
       ]
     }
   }
   ```

The server replies with `202 Accepted` as soon as the payload hits, so
the Pi's hook returns right away.

A quick smoke test from the Pi:

```bash
curl -i http://your-mac.local:8765/health   # → 200 ok
echo '{"hook_event_name":"Notification"}' \
  | CLAUDE_SPEAKS_TOKEN=... CLAUDE_SPEAKS_URL=http://your-mac.local:8765/hook \
    python3 scripts/remote-hook.py
```

If the token is missing or wrong you'll get `401 unauthorized`.

### Per-request overrides

If you've got Claude on the Mac, Claude on the Pi, and Hermes all
piping audio through the same Mac, hearing the same voice three times
gets confusing fast. Every payload accepts an optional `claude_speaks`
block that deep-merges onto `config.json` *for that request only*.
Anything from `config.json` is fair game: `tts_provider`, `voices`,
`features`, `llm_model`, `provider_settings`, etc.

Example payload from the Pi, configuring "rpi-claude" to use a French
Marvin voice and skip the idle nag:

```json
{
  "hook_event_name": "Stop",
  "last_assistant_message": "All done.",
  "claude_speaks": {
    "voices": {
      "mistral": {"main": "fr_marie"}
    },
    "features": {
      "notification": false
    }
  }
}
```

Both shipped clients pick this up automatically via the
`CLAUDE_SPEAKS_OVERRIDES` env var — set it to a JSON object on the
client machine and it gets injected into every payload:

```bash
# On the Pi
export CLAUDE_SPEAKS_OVERRIDES='{"voices":{"mistral":{"main":"fr_marie"}}}'

# For Hermes (via systemd unit, ~/.profile, however Hermes is launched)
export HERMES_SPEAKS_OVERRIDES='{"voices":{"mistral":{"main":"gb_jane_confident"}}}'
```

The Hermes plugin additionally ships with a default of
`{"features": {"monologue": false, "notification": false}}`. Setting
`HERMES_SPEAKS_OVERRIDES` (or its `CLAUDE_SPEAKS_OVERRIDES` fallback)
replaces that default, so include the features block yourself if you
still want those stages off.

Each merged-in overlay is logged on the Mac as `<config overrides>`.

### Other agents (Hermes, etc.)

The endpoint only cares about two JSON keys — `hook_event_name`
(`"Stop"` or `"Notification"`) and `last_assistant_message` — so any
agent that lets you run something when it finishes writing can drive it.

`scripts/hermes-speaks/` ships a worked example for
[Hermes](https://nousresearch.com): a proper Hermes plugin (manifest
plus `__init__.py`) that hooks `post_llm_call` and POSTs the final
reply to the server.

Install it on the Pi (or wherever Hermes runs) by copying the two
files into Hermes' user-plugins directory and enabling it:

```bash
mkdir -p ~/.hermes/plugins/hermes-speaks
cp scripts/hermes-speaks/plugin.yaml \
   scripts/hermes-speaks/__init__.py \
   ~/.hermes/plugins/hermes-speaks/
hermes plugins enable hermes-speaks
```

Then set the URL and token where Hermes can see them — Hermes-specific
names are preferred, with the existing `CLAUDE_SPEAKS_*` vars accepted
as a fallback:

```bash
export HERMES_SPEAKS_URL='http://your-mac.local:8765/hook'
export HERMES_SPEAKS_TOKEN='same-string-as-on-the-mac'
# Optional — overrides config.json on the Mac for Hermes' payloads only:
export HERMES_SPEAKS_OVERRIDES='{"voices":{"mistral":{"main":"gb_jane_confident"}}}'
```

Restart Hermes (`hermes gateway restart`, or just start a fresh
`hermes` session) so the plugin loads and picks up the env vars. The
plugin defaults to disabling the `monologue` and `notification` stages.
Override `HERMES_SPEAKS_OVERRIDES` to change that.

If `HERMES_SPEAKS_URL`/`TOKEN` aren't set (and neither are their
`CLAUDE_SPEAKS_*` fallbacks), the plugin silently no-ops.

## Hands-free voice replies (optional, off by default)

claude-speaks has a sibling project,
[claude-listens](https://github.com/ohnotnow/claude-listens), which closes
the loop in the other direction: after claude reads a reply aloud, your
microphone turns on, you answer out loud, and your words land back in
the same Claude Code session. claude-speaks' entire contribution to that
loop is one small hook (`handsfree.py`).

To set it up, in `config.json`:

```json
"handsfree_arm_command": ["/absolute/path/to/claude-listens/bin/ears", "arm"]
```

The command must *attempt* to arm the recorder and exit non-zero when it
did not get the microphone (recorder busy, daemon down) — claude-listens'
`ears arm` does exactly that. See the claude-listens README for the rest
of the loop.

## Configuration

API keys live in `.env`. Everything else lives in `config.json` (copy
`config.example.json` to get started). If `config.json` is missing or a key
is absent, the defaults below kick in.

The LLM (used for the Marvin preamble, summariser, and any classifier the
provider wants) and the TTS provider (used to actually speak) are
independent. Set `llm_model` to anything LiteLLM supports — Claude,
GPT, Mistral chat, a local Ollama model — and `tts_provider` to
whichever speech backend you fancy.

`.env`:

| Env var | Default | Notes |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required when `tts_provider` is `mistral`. Also used for the LLM calls if `llm_model` points at a Mistral model. |
| `XAI_API_KEY` | — | Required when `tts_provider` is `xai`. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. | — | Only needed if `llm_model` points at that provider. |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Alternative to `ANTHROPIC_API_KEY` for `anthropic/...` models: bills your Claude subscription instead of API credit. See [Using your Claude subscription](#using-your-claude-subscription). |

### Using your Claude subscription

If `llm_model` is an `anthropic/...` model, you can pay for the LLM calls
with your Claude subscription rather than API credit. Run
`claude setup-token`, put the resulting token in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`, and the hook routes those calls through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
instead of LiteLLM. (Anthropic permit this for personal, non-commercial
use — a hook that makes Marvin sigh at you qualifies comfortably.)

Set the token *or* `ANTHROPIC_API_KEY` — not both. Claude Code's
credential precedence puts the API key above the token, so with both set
every call would quietly bill your API account. The hook refuses to run
the event: it logs `<auth guard>` and plays the fallback sound. If you
hear a system sound and see that log line, remove one of the two from
`.env` (and check your shell isn't exporting `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN` globally either).

One trade-off: each SDK call adds a few seconds of latency per turn.

`config.json`:

| Key | Default | Notes |
|---|---|---|
| `llm_model` | `mistral/mistral-small-latest` | Any LiteLLM-supported model, used for the LLM-shaped work each provider needs (classifier, preamble, summariser, notification line). Try `mistral/ministral-3b-latest` for speed, or `anthropic/claude-haiku-4-5-20251001` for quality. |
| `tts_provider` | `mistral` | The `name` of any provider in `providers/`. Mistral, xAI, OpenAI, ElevenLabs, and Kokoro ship in the box; see [Switching to xAI](#switching-to-xai) / [Switching to Kokoro](#switching-to-kokoro-local-free) for what changes when you flip between them, and [providers/README.md](providers/README.md) for adding more. |
| `voices` | (per-provider defaults) | Per-provider voice config keyed by provider name and role. See [Voices](#voices). |
| `provider_settings` | `{}` | Per-provider knobs (model id, output format, sample rate, etc.). Each provider reads its own slice via `self.settings`. See [provider_settings](#provider_settings). |
| `gap_file` | `0_75s` | Which silent mp3 in `gaps/` to stitch between the preamble and the main reply. See below. |
| `word_replacements` | `{}` | Phonetic swap map — see [Word replacements](#word-replacements). |
| `features` | all `true` | Per-stage toggles — see [Features](#features). |
| `notification_languages` | seven-language weighted list (see below) | Which languages the idle quip can be generated in, and how often. See [Customising the personality](#customising-the-personality). |
| `player_command` | `afplay` | Command used to play the stitched mp3. Accepts a string (shlex-split) or an argv list. Set to e.g. `"mpg123 -q"` or `"ffplay -nodisp -autoexit -loglevel quiet"` on Linux. The file path is appended as the last argument. |
| `fallback_sound` | `/System/Library/Sounds/Funk.aiff` | Sound played when every TTS path fails. Must be a format your `player_command` understands — swap to an mp3 if you've replaced `afplay`. |

### Voices

`voices` is keyed first by provider, then by role. Three roles: `main`
(Jane, who speaks the actual reply), `monologue` (Marvin, who sighs
before the reply on Stop), and `notification` (Marvin again, but for the
idle "still waiting" quip). Each role takes a `voice` and an optional
`language` (xAI only — Mistral ignores it).

```json
"voices": {
  "mistral": {
    "main":         {"voice": "gb_jane"},
    "monologue":    {"voice": "fr_marie_sad"},
    "notification": {"voice": "fr_marie_sad"}
  },
  "xai": {
    "main":         {"voice": "Eve", "language": "en"},
    "monologue":    {"voice": "Ara", "language": "fr"},
    "notification": {"voice": "Ara", "language": "ja"}
  },
  "kokoro": {
    "main":         {"voice": "bm_george"},
    "monologue":    {"voice": "bf_emma"},
    "notification": {"voice": "bf_emma"}
  }
}
```

Configure several providers and flipping `tts_provider` between them
will pick up the matching voice block automatically.

A bare string is accepted as shorthand for the default object form:
`"main": "Eve"` is the same as `"main": {"voice": "Eve"}`.

**Defaults** if a role is missing: each provider declares its own
`default_voices` map (see the file for that provider in `providers/`).
Mistral defaults to `gb_jane` for main and `gb_jane_sarcasm` for
monologue and notification; xAI defaults to `Eve` for all three. If you
omit a role from your `voices` block entirely, that provider's default
fills in. Languages default to `en`.

**Notification languages.** The idle notification line is generated in
one of seven languages — English, German, Japanese, Chinese, Hindi,
Korean, or Vietnamese — picked at random and weighted (English at 1, the
others at 5 each). The chosen language is logged as
`<notification language>`. The TTS `language` you set under
`notification` is independent — the LLM picks the words, your config
picks the accent. (Kokoro ignores the setting: its voices are
single-language, so quips default to English. See
[Switching to Kokoro](#switching-to-kokoro-local-free).)

On Mistral, the `main` voice is treated as a **prefix** — the classifier's
nine-style suffix (`_neutral`, `_sarcasm`, etc.) gets appended automatically.
So `"voice": "gb_jane"` becomes `gb_jane_<style>` at synthesis time. The
`monologue` voice is a full voice id — no suffix is appended.

On xAI, both voices are literal ids and the prosody tags inside the text
do the emotional work. Setting `monologue.language` to `fr` is the trick
for getting xAI close to `fr_marie_sad`'s dejection — see below.

Jane's nine emotional styles (Mistral only): `neutral`, `sarcasm`, `confused`,
`shameful`, `sad`, `jealousy`, `frustrated`, `curious`, `confident`. The
classifier is biased towards `neutral`, so you'll mostly hear that one.

### Switching to xAI

xAI's TTS doesn't have an emotional-style enum. Instead it accepts a small
set of inline prosody tags wrapped around spans of text — `<soft>`,
`<emphasis>`, `<slow>`, `<lower-pitch>`, and so on. When `tts_provider` is
`xai`:

- The classifier call is skipped — the configured `main` voice is used
  literally, no nine-style suffix.
- The summariser and preamble prompts are swapped for xAI variants that
  list the allowed tags and ask the LLM to wrap a span or two where it
  genuinely aids delivery.
- The summariser runs even on short replies.
- Marvin gets his own voice id — pick something distinct from Jane's so
  the preamble doesn't blur into the reply (`Ara` is a reasonable contrast
  with `Eve`). Then set `monologue.language` to `fr` so xAI speaks the
  English line with a French inflection.

Example xAI config:

```json
{
  "llm_model": "mistral/mistral-small-latest",
  "tts_provider": "xai",
  "voices": {
    "xai": {
      "main":         {"voice": "Eve", "language": "en"},
      "monologue":    {"voice": "Ara", "language": "fr"},
      "notification": {"voice": "Ara", "language": "ja"}
    }
  }
}
```

### Switching to Kokoro (local, free)

[Kokoro](https://github.com/nazdridoy/kokoro-tts) does text-to-speech on
your own machine — no API key, no per-character bill. Depending on your
machine, latency can be a problem however. Set `"async": true` on the
hook so the wait happens in the background rather than blocking your
session — see
[Running the hook in the background](#running-the-hook-in-the-background).

Setup:

1. Install the CLI: `uv tool install kokoro-tts`
2. Download the two model files — `kokoro-v1.0.onnx` and
   `voices-v1.0.bin` — into the claude-speaks project directory (the
   [kokoro-tts README](https://github.com/nazdridoy/kokoro-tts) has the
   current download links). Different locations work too, via
   `provider_settings.kokoro.model_path` / `.voices_path`.
3. Set `"tts_provider": "kokoro"` in `config.json`. No `.env` entry
   needed — though the LLM calls still use whatever `llm_model` points
   at, so that key stays.

When `tts_provider` is `kokoro`:

- No classifier, no prosody tags — Kokoro reads text literally, so the
  prompts are plain-prose variants.
- The summariser only runs on replies longer than ~60 words, like
  Mistral's.
- `notification_languages` is ignored — Kokoro voices are
  single-language. Quips are English by default; the comment block in
  `prompts/kokoro/notification.md` explains how to change that if
  you've configured a non-English voice.
- Voice ids are literal (`bm_george`, `bf_emma`, `af_sky`, …) — run
  `kokoro-tts --help-voices` for the list. Blends work as plain strings:
  `"voice": "af_sarah:60,am_adam:40"`.
- Playback is stitched with the 24 kHz gap files (`gaps/*_24k.mp3`)
  automatically — see [Gaps between clips](#gaps-between-clips).

Example kokoro config:

```json
{
  "llm_model": "mistral/mistral-small-latest",
  "tts_provider": "kokoro",
  "voices": {
    "kokoro": {
      "main":         {"voice": "bm_george"},
      "monologue":    {"voice": "bf_emma"},
      "notification": {"voice": "bf_emma"}
    }
  }
}
```

### provider_settings

Per-provider knobs — model ids, output formats, sample rates, anything
the backend wants — live in `provider_settings.<provider_name>`.
Omitting the block entirely is fine.

xAI exposes its output format, and Kokoro its paths and pacing:

```json
"provider_settings": {
  "xai": {
    "sample_rate": 22050,
    "bit_rate": 64000
  },
  "kokoro": {
    "cli_path": "kokoro-tts",
    "model_path": "kokoro-v1.0.onnx",
    "voices_path": "voices-v1.0.bin",
    "language": "en-gb",
    "speed": 1.0,
    "timeout": 120
  }
}
```

xAI's rates match the gap mp3s in `gaps/` and shouldn't be changed unless
you also swap the gap files — see [Gaps between clips](#gaps-between-clips)
for what happens if they don't agree. Kokoro's relative paths resolve
against the claude-speaks directory (absolute paths work too), `language`
is the accent used when a voice doesn't set its own, and `timeout` is the
per-clip synthesis cap in seconds.

### Adding a TTS provider

`providers/` is a drop-in folder. To add Replicate, Piper, or anything
else, write one new file that implements the provider contract and the
rest of the project picks it up automatically. See
[providers/README.md](providers/README.md) for the contract, the worked
examples, and a copy-pasteable skeleton.

### Gaps between clips

When a Stop event triggers both a Marvin preamble and a main reply, the two
synthesised mp3s are stitched into a single file and played back-to-back.
To stop them running into each other, a short chunk of silence gets spliced
in between.

The `gaps/` directory holds a few pre-rendered silent mp3s:

- `0_5s.mp3` — half a second, snappy
- `0_75s.mp3` — three-quarters of a second (default)
- `1_0s.mp3` — a full second, more theatrical

Pick one with the `gap_file` key in `config.json` (no extension — e.g.
`"gap_file": "1_0s"`). To add your own, drop another silent mp3 into
`gaps/` and reference it by filename. Any mp3-encoding tool will do;
`ffmpeg` is the usual suspect:

```bash
ffmpeg -f lavfi -i anullsrc=r=22050:cl=mono -t 1.5 -b:a 56k gaps/1_5s.mp3
```

The shipped gaps are 22050 Hz mono. Any custom gap should match that, and
any TTS provider you add should emit mp3 at the same rate — afplay
refuses to cross sample-rate boundaries cleanly when stitching, so a
44.1 kHz gap between two 22 kHz clips (or vice versa) will silently
truncate playback at the boundary. The xAI provider pins its output
explicitly via `provider_settings.xai`; if you swap in a provider that
emits something else, expect to either match the rate or replace the
gaps and update `provider_settings` to match.

Kokoro emits 24 kHz mp3s, so each gap also ships in a `_24k` variant
(`0_75s_24k.mp3` and friends) generated at that rate. Providers declare
the variant they need (`gap_variant = "24k"` in `providers/kokoro.py`)
and `audio.py` picks the right file for whatever `gap_file` you've
chosen. If you add a custom gap duration for Kokoro, render it at
`r=24000` and name it `<name>_24k.mp3`.

### Features

Three independent toggles let you switch off any of the spoken stages.
Default is all three on.

```json
"features": {
  "monologue": true,
  "main": true,
  "notification": false
}
```

| Toggle | What it controls |
|---|---|
| `monologue` | The Marvin sigh that runs before the reply on Stop. Disabling it skips the preamble LLM call entirely. |
| `main` | The summarised/spoken version of Claude's actual reply. Disabling it skips the summariser (and the Mistral tone classifier). Switching this off and leaving `monologue` on gives you only the Marvin quip. |
| `notification` | The idle "still waiting" nag. Disabling it short-circuits the Notification handler entirely — no LLM call, no audio. |

If both `monologue` and `main` are off, Stop events go quiet.

### Word replacements

TTS engines mispronounce plenty of technical jargon — `vite` comes out
as "vite" (rhymes with "kite") rather than "veet", for example. Add a
`word_replacements` object to `config.json` with a flat map of problem
words to phonetic spellings and they'll be swapped in before the text
hits the TTS, regardless of provider. Matching is case-insensitive and on word boundaries, so
`Vite` and `vite` both get caught but `invitation` doesn't.

```json
{
  "word_replacements": {
    "vite": "veet",
    "nginx": "engine-ex",
    "kubectl": "koob-control"
  }
}
```

`config.example.json` ships with a starter set — copy it to `config.json`
and edit to taste.

## Customising the personality

The Marvin shtick is the default, not the law. There are two layers
of customisation, lightest first.

### Swap the persona (one config line)

The character that speaks the monologue and the idle quip is set in
`config.json` under `personas`:

```json
"personas": {
  "monologue": "marvin",
  "notification": "marvin",
  "main": null
}
```

Each value resolves like this: if `personas[.local]/<value>.md` exists,
that file's contents are used as the character description; otherwise
the value is passed straight through as a freeform description. So
both of these work:

```json
"personas": {
  "monologue": "panto-dame",                       // loads personas.local/panto-dame.md
  "notification": "a film noir detective, voice like cold whisky"   // used verbatim
}
```

Drop new persona files in `personas.local/` (gitignored, survives
`git pull`) — see [personas/README.md](personas/README.md) for the
file format. The shipped `personas/marvin.md` is the source of truth
for the Marvin character.

`personas.main` controls a third, subtler thing: when set, the
summariser is asked to **preserve** a beat of that voice when
compressing Claude's actual reply. Defaults to `null`.

> **OpenAI users:** the TTS-level `voices.openai.<role>.instructions`
> field shapes the *spoken delivery* separately from the LLM prompt.
> Changing the persona without also updating those instructions gets
> you a panto dame's words delivered in Marvin's weary voice.

### Override the prompts themselves (heavier)

If swapping the persona isn't enough, the provider prompts live as plain
markdown files under `prompts/<provider>/` and are overrideable per-user
without forking the project.

### The directory layout

```
prompts/                   ← shipped defaults, checked in.
  mistral/
    classifier.md          (Mistral only — picks one of nine emotional styles)
    summary.md             (compresses long replies for TTS)
    preamble.md            (the Marvin sigh before the reply)
    notification.md        (the idle "still waiting" quip)
  xai/
    summary.md             (compresses + adds inline prosody tags)
    preamble.md            (the Marvin sigh, with prosody tags allowed)
    notification.md        (the idle quip)

prompts.local/             ← your overrides, gitignored.
  mistral/
    notification.md        (overrides only this one; others fall through)
```

At runtime `prompts.local/<provider>/<name>.md` wins; if it's absent,
the matching `prompts/<provider>/<name>.md` is used.

### A worked example: making Marvin cheerful

```bash
mkdir -p prompts.local/mistral
cp prompts/mistral/preamble.md prompts.local/mistral/preamble.md
$EDITOR prompts.local/mistral/preamble.md
```

Rewrite the prompt body to taste (the file's leading `<!-- ... -->`
comment block tells you what the prompt is for and what placeholders
are available — see below). Save, run any Claude Code turn, and the new
voice takes over. To revert, delete the file in `prompts.local/`.

### What's safe to edit, what isn't

The shipped prompts open with an HTML comment block documenting the
prompt's purpose, any `{placeholders}` it uses, and which Provider
method calls it. That comment block is **stripped at load time** by
`prompts.py`, so editing or deleting it is fine.

The notification prompts use two placeholders:

- `{language}` — picked at random from `notification_languages` in
  `config.json`. Drop this and Marvin will freestyle the language
  himself (with mixed results).
- `{history}` — a bullet list of the last ten quips from
  `notification-history.txt`, used to nudge against repetition. Drop
  this if you'd rather he repeat himself.

If your custom prompt accidentally introduces a stray `{` (a JSON
example, say), the hook logs `<prompt format error>` and falls back to
the unformatted template.

### Just the languages, please

If the personality's fine but you want to disable the multi-language
notification roulette, set `notification_languages` in `config.json`
to a single entry:

```json
"notification_languages": [["English", 1]]
```

The format is a list of `[name, weight]` pairs. The name is sent
verbatim to the LLM as the language to write in (so "Glaswegian" or
"medieval English" both work — the LLM does the rest). Weights are
positive integers; relative, not absolute.

### Turning a stage off entirely

Often easier than rewriting a prompt: set `features.monologue` or
`features.notification` to `false` in `config.json` to skip the
relevant LLM call (and clip) entirely. See [Features](#features).

## Shutting Marvin up mid-sentence

If Claude finishes a turn while you're in a Teams call (or otherwise need
silence in a hurry), `killall afplay` stops playback dead. Bind it to a
hotkey and you've got a panic button. (If you've changed `player_command`
to something other than `afplay`, swap the binary name in the commands
and scripts below to match.)

A ready-made script lives at `scripts/shut-marvin-up.sh`. Pick whichever of
the three options below suits your setup.

### Raycast

The script ships with Raycast metadata in the header, so Raycast will treat
it as a Script Command out of the box.

1. Open Raycast → Settings → Extensions → Script Commands.
2. Add the project's `scripts/` directory as a script directory.
3. Find "Shut Marvin Up" in the list and assign a hotkey (something like
   `⌃⌥⌘.` is unlikely to clash with Teams' own shortcuts).

`@raycast.mode silent` means no Raycast window pops up.

### macOS Shortcuts.app

No Raycast needed:

1. Open Shortcuts.app → new shortcut.
2. Add a "Run Shell Script" action with `killall afplay 2>/dev/null`.
3. In the shortcut's info panel (the ⓘ on the right), set a keyboard
   shortcut.

### Hammerspoon

If you already have Hammerspoon, one line in `~/.hammerspoon/init.lua`:

```lua
hs.hotkey.bind({"ctrl", "alt", "cmd"}, ".", function()
  hs.execute("killall afplay")
end)
```

Reload the config and the chord is live.

## Known rough edges

- On Mistral, the nine-style enum assumes Jane's flavour set — other Mistral
  voices may not have all nine `_<style>` variants.
- Markdown stripping is regex-based and unsubtle.
- The hook blocks while the classifier and TTS calls complete
  (noticeably longer on Kokoro) — see
  [Running the hook in the background](#running-the-hook-in-the-background).
- macOS-by-default, because `afplay` is the assumed player. Set `player_command` in `config.json` to point at a different binary (e.g. `mpg123`, `ffplay`) to run elsewhere — you'll also want to override `fallback_sound` since the default points at a macOS system aiff.
- No way to interrupt Jane mid-sentence from inside Claude Code itself —
  see [Shutting Marvin up mid-sentence](#shutting-marvin-up-mid-sentence)
  for the hotkey options.

## Log

Every Stop event gets appended to `stop-hook.log`, including the chosen
voices, the rewritten text, and each TTS call's outcome (with byte counts
on success or the API error body on failure).

The last ten turns are also kept in `/tmp/` as a pair of files:

- `claude-speaks-<timestamp>.mp3` — preamble + main reply stitched
  into a single mp3.
- `claude-speaks-<timestamp>.txt` — each clip's voice id and the exact
  text that was spoken, separated by blank lines.

## Example

![Example audio](examples/claude-speaks-20260417-142352-252142.mp3)

```
voice: fr_marie_sad
Oh wonderful, another avalanche of endless knobs to twiddle ... ...

voice: gb_jane_neutral
Perfect — that's exactly the tone we were aiming for. Voice intact, fiddly detail gone, still comfortably under the 30-second mark before Marvin can wander in with his polite cough.

And I love that Marvin himself called it "another avalanche of endless knobs to twiddle" — self-aware to the end.
```

## Licence

MIT. See [LICENSE](LICENSE).
