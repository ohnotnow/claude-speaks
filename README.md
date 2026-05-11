# claude-speaks

**Work in progress.** A small experiment: have Claude Code read its final
reply out loud, so you can wander off and make a coffee while it works and
still catch what it said.

It's a [Claude Code Stop hook](https://docs.claude.com/en/docs/claude-code/hooks)
that:

1. Catches the `last_assistant_message` when Claude finishes a turn.
2. Strips markdown and drops fenced code blocks.
3. Hands the text to the configured TTS provider, which fans out
   parallel LLM calls via LiteLLM (model configurable — see
   Configuration) to produce a Marvin-the-Paranoid-Android-style sigh
   and a TTS-friendly version of the reply. Each provider does this its
   own way: Mistral classifies the tone into one of nine emotional
   styles to pick a matching Jane voice; xAI skips the classifier and
   asks the summariser to embed inline prosody tags (`<soft>`,
   `<emphasis>`, `<slow>`, …) directly in the text.
4. Synthesises two TTS clips — Marvin's sigh in a monologue voice, then
   the reply in the main voice.
5. Stitches them with a short silent mp3 gap and plays the result via
   `afplay` as a detached subprocess.

The codebase splits along those lines: `main.py` is a thin entry point
(~85 lines, reads stdin and dispatches), each TTS backend lives in its
own file under `providers/`, and `audio.py` does the stitching and
playback. Adding a new backend is a single new file in `providers/` —
see [providers/README.md](providers/README.md) for the contract.

There's also a Notification handler for when Claude Code is idle or
waiting for permission — Marvin pipes up with a short weary quip in the
notification voice (a separate role from monologue, so the idle nag can
have its own voice and language). A rolling history of recent lines is
fed back into the prompt to stop him repeating himself, and Python
rolls a weighted die to pick the language — by default English, German,
Japanese, Chinese, Hindi, Korean, or Vietnamese, with English weighted
at 1 and the others at 5 each (so English shows up roughly one time in
31). The list is editable in `config.json` — set it to a single
language if multilingual Marvin isn't your thing. See
[Customising the personality](#customising-the-personality).

If any of the LLM calls fail, the hook prepends a short spoken heads-up
("heads up — the summariser call fell over, raw reply coming up") before
the reply, so silent failures are audible rather than mysterious.

## Requirements

- macOS (uses `afplay` for playback)
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- An API key for whichever TTS provider you've configured (Mistral and xAI
  ship in the box; others can be dropped into `providers/` — see
  [providers/README.md](providers/README.md))

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

## Remote mode (Raspberry Pi → Mac)

If you run Claude Code on a headless box (a Raspberry Pi left ticking
away, a remote server you've SSH'd into, etc.) but want the audio out
of your Mac's speakers, `server.py` gives you a small HTTP shim.

The Pi-side hook POSTs the same JSON it would normally hand to
`main.py` on stdin; the Mac receives it, runs the full
provider → LLM → TTS pipeline, and plays the audio locally. The Pi
never sees a TTS response.

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

### On the Pi (client side)

The repo ships `scripts/remote-hook.py` — stdlib-only Python, so the
Pi doesn't need uv or any dependencies installed. On the Pi:

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

The server replies with `202 Accepted` as soon as the payload is
queued, so the Pi's hook returns in milliseconds — TTS work happens
in a background thread on the Mac.

A quick smoke test from the Pi:

```bash
curl -i http://your-mac.local:8765/health   # → 200 ok
echo '{"hook_event_name":"Notification"}' \
  | CLAUDE_SPEAKS_TOKEN=... CLAUDE_SPEAKS_URL=http://your-mac.local:8765/hook \
    python3 scripts/remote-hook.py
```

If the token is missing or wrong you'll get `401 unauthorized`; if the
server can't read its own `CLAUDE_SPEAKS_TOKEN`, it refuses to start.

### Per-request overrides

If you've got Claude on the Mac, Claude on the Pi, and Hermes all
piping audio through the same Mac, hearing the same voice three times
gets confusing fast. Every payload accepts an optional `claude_speaks`
block that deep-merges onto `config.json` *for that request only* — no
restart, no second config file. Anything from `config.json` is fair
game: `tts_provider`, `voices`, `features`, `llm_model`,
`provider_settings`, etc.

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
export CLAUDE_SPEAKS_OVERRIDES='{"voices":{"mistral":{"main":"gb_jane_confident"}}}'
```

The Hermes plugin additionally ships with a sensible default of
`{"features": {"monologue": false, "notification": false}}` so
Marvin's preamble doesn't gatecrash a non-Claude agent. Setting
`CLAUDE_SPEAKS_OVERRIDES` replaces that default, so include the
features block yourself if you still want those stages off.

Each merged-in overlay is logged on the Mac as `<config overrides>`
so you can tell which client triggered which voice when something
unexpected comes out of the speakers.

### Other agents (Hermes, etc.)

The endpoint only cares about two JSON keys — `hook_event_name`
(`"Stop"` or `"Notification"`) and `last_assistant_message` — so any
agent that lets you run code at end-of-turn can drive it.

`scripts/hermes_speaks_plugin.py` is a worked example for
[Hermes](https://nousresearch.com)' `transform_llm_output` hook: it
wraps the model's reply in the right JSON shape, POSTs to the server,
and returns `None` so Hermes delivers the original text unchanged.
Drop it in Hermes' plugin directory, set the same `CLAUDE_SPEAKS_URL`
and `CLAUDE_SPEAKS_TOKEN` env vars, and the Mac speaks for Hermes too.

## Configuration

API keys live in `.env`. Everything else lives in `config.json` (copy
`config.example.json` to get started). If `config.json` is missing or a key
is absent, the defaults below kick in.

The LLM (used for the Marvin preamble, summariser, and any classifier the
provider wants) and the TTS provider (used to actually speak) are
independent. Set `llm_model` to anything LiteLLM supports — Claude,
GPT, Mistral chat, a local Ollama model — and `tts_provider` to
whichever speech backend you fancy. They don't have to share a vendor.

`.env`:

| Env var | Default | Notes |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required when `tts_provider` is `mistral`. Also used for the LLM calls if `llm_model` points at a Mistral model. |
| `XAI_API_KEY` | — | Required when `tts_provider` is `xai`. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. | — | Only needed if `llm_model` points at that provider. |

`config.json`:

| Key | Default | Notes |
|---|---|---|
| `llm_model` | `mistral/mistral-small-latest` | Any LiteLLM-supported model, used for the LLM-shaped work each provider needs (classifier, preamble, summariser, notification line). Try `mistral/ministral-3b-latest` for speed, or `anthropic/claude-haiku-4-5-20251001` for quality. |
| `tts_provider` | `mistral` | The `name` of any provider in `providers/`. Mistral and xAI ship in the box; see [Switching to xAI](#switching-to-xai) for what changes when you flip between them, and [providers/README.md](providers/README.md) for adding more. |
| `voices` | (per-provider defaults) | Per-provider voice config keyed by provider name and role. See [Voices](#voices). |
| `provider_settings` | `{}` | Per-provider knobs (model id, output format, sample rate, etc.). Each provider reads its own slice via `self.settings`. See [provider_settings](#provider_settings). |
| `gap_file` | `0_75s` | Which silent mp3 in `gaps/` to stitch between the preamble and the main reply. See below. |
| `word_replacements` | `{}` | Phonetic swap map — see [Word replacements](#word-replacements). |
| `features` | all `true` | Per-stage toggles — see [Features](#features). |
| `notification_languages` | seven-language weighted list (see below) | Which languages the idle quip can be generated in, and how often. See [Customising the personality](#customising-the-personality). |

### Voices

`voices` is keyed first by provider, then by role. Three roles: `main`
(Jane, who speaks the actual reply), `monologue` (Marvin, who sighs
before the reply on Stop), and `notification` (Marvin again, but for the
idle "still waiting" quip — kept separate so the two flavours of Marvin
don't have to share a voice or language). Each role takes a `voice` and
an optional `language` (xAI only — Mistral ignores it).

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
  }
}
```

Configure both providers and flipping `tts_provider` between `mistral` and
`xai` will pick up the matching voice block automatically — no shuffling
voice ids around when you switch.

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
Korean, or Vietnamese — picked by a weighted die in Python (English at
1, the others at 5 each, so English shows up roughly one time in 31).
The chosen language is logged as `<notification language>` so you can
tell whether a surprising line was the LLM misbehaving or just the
dice. The TTS `language` you set under `notification` is independent of
the dice — the LLM picks the words, your config picks the accent, and
pleasing mismatches (Japanese text through a German-flagged voice, say)
are very much encouraged.

On Mistral, the `main` voice is treated as a **prefix** — the classifier's
nine-style suffix (`_neutral`, `_sarcasm`, etc.) gets appended automatically.
So `"voice": "gb_jane"` becomes `gb_jane_<style>` at synthesis time. The
`monologue` voice is a full voice id — no suffix is appended.

On xAI, both voices are literal ids and the prosody tags inside the text
do the emotional work. Setting `monologue.language` to `fr` is the trick
for getting xAI close to `fr_marie_sad`'s dejection — see below.

Jane's nine emotional styles (Mistral only): `neutral`, `sarcasm`, `confused`,
`shameful`, `sad`, `jealousy`, `frustrated`, `curious`, `confident`. The
classifier is biased towards `neutral`, so you'll mostly hear that one and
only get the other flavours when Claude's reply genuinely calls for it.

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
- The summariser runs even on short replies, so a "Done." can still pick
  up a `<slow>` if the model thinks it deserves one.
- Marvin gets his own voice id — pick something distinct from Jane's so
  the preamble doesn't blur into the reply (`Ara` is a reasonable contrast
  with `Eve`). Then set `monologue.language` to `fr` so xAI speaks the
  English line with a French inflection — much closer to the right level
  of resentment than any of xAI's default voices manage on their own.

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

Unknown or malformed tags are ignored by xAI rather than rejected, so no
sanitiser is needed — Claude's reply goes through as-is.

### provider_settings

Per-provider knobs — model ids, output formats, sample rates, anything
the backend wants — live in `provider_settings.<provider_name>`. Each
provider reads its own slice via `self.settings` and merges it over its
own internal defaults, so omitting the block entirely is fine.

xAI is the only built-in that exposes anything here:

```json
"provider_settings": {
  "xai": {
    "sample_rate": 22050,
    "bit_rate": 64000
  }
}
```

These match the gap mp3s in `gaps/` and shouldn't be changed unless you
also swap the gap files — see [Gaps between clips](#gaps-between-clips)
for what happens if they don't agree.

### Adding a TTS provider

`providers/` is a drop-in folder. To add ElevenLabs, OpenAI, Replicate,
Piper, or anything else, write one new file that implements the
provider contract and the rest of the project picks it up automatically
— no edits to `main.py`, no entry in a registry. See
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

### Features

Three independent toggles let you switch off any of the spoken stages —
useful if you want the Marvin preamble but not the full reply, or want
silence when Claude is idle. Default is all three on, so omitting the
block keeps the original behaviour.

```json
"features": {
  "monologue": true,
  "main": true,
  "notification": false
}
```

| Toggle | What it controls |
|---|---|
| `monologue` | The Marvin sigh that runs before the reply on Stop. Disabling it skips the preamble LLM call entirely — no wasted tokens. |
| `main` | The summarised/spoken version of Claude's actual reply. Disabling it skips the summariser (and the Mistral tone classifier). Switching this off and leaving `monologue` on gives you only the Marvin quip — the use case you'd reach for if you only want the personality, not the recap. |
| `notification` | The idle "still waiting" nag. Disabling it short-circuits the Notification handler entirely — no LLM call, no audio. |

If both `monologue` and `main` are off, Stop events go quiet — nothing
is synthesised or played. The hook still returns cleanly.

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
and edit to taste. If the section's missing or malformed, the hook just
skips the step.

## Customising the personality

The Marvin shtick is the default, not the law. Each provider's prompts
live as plain markdown files under `prompts/<provider>/` and are
overrideable per-user without forking the project.

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
the matching `prompts/<provider>/<name>.md` is used. So you only need
to copy the prompts you actually want to change, and `git pull` for
upstream fixes never touches `prompts.local/`.

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
`prompts.py`, so leaving it in your override copy is harmless — it
won't get read aloud — and editing or deleting it is fine.

The notification prompts use two placeholders:

- `{language}` — picked at random from `notification_languages` in
  `config.json`. Drop this and Marvin will freestyle the language
  himself (with mixed results).
- `{history}` — a bullet list of the last ten quips from
  `notification-history.txt`, used to nudge against repetition. Drop
  this if you'd rather he repeat himself.

If your custom prompt accidentally introduces a stray `{` (a JSON
example, say), the hook logs `<prompt format error>` and falls back to
the unformatted template rather than crashing.

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
hotkey and you've got a panic button.

A ready-made script lives at `scripts/shut-marvin-up.sh`. Pick whichever of
the three options below suits your setup.

### Raycast

The script ships with Raycast metadata in the header, so Raycast will treat
it as a Script Command out of the box.

1. Open Raycast → Settings → Extensions → Script Commands.
2. Add the project's `scripts/` directory as a script directory.
3. Find "Shut Marvin Up" in the list and assign a hotkey (something like
   `⌃⌥⌘.` is unlikely to clash with Teams' own shortcuts).

`@raycast.mode silent` means no Raycast window pops up — the hotkey just
kills `afplay` and gets out of the way.

### macOS Shortcuts.app

No Raycast needed:

1. Open Shortcuts.app → new shortcut.
2. Add a "Run Shell Script" action with `killall afplay 2>/dev/null`.
3. In the shortcut's info panel (the ⓘ on the right), set a keyboard
   shortcut. It works system-wide, including during Teams calls.

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
- The hook blocks for roughly 2–3 seconds while the classifier and TTS
  calls complete. Playback itself is detached and non-blocking.
- macOS-only, because of `afplay`.
- No way to interrupt Jane mid-sentence from inside Claude Code itself —
  see [Shutting Marvin up mid-sentence](#shutting-marvin-up-mid-sentence)
  for the hotkey options.

## Log

Every Stop event gets appended to `stop-hook.log`, including the chosen
voices, the rewritten text, and each TTS call's outcome (with byte counts
on success or the API error body on failure). Handy when tuning prompts,
chasing voice 404s, or working out why a clip didn't play.

The last ten turns are also kept in `/tmp/` as a pair of files:

- `claude-speaks-<timestamp>.mp3` — preamble + main reply stitched
  into a single mp3.
- `claude-speaks-<timestamp>.txt` — each clip's voice id and the exact
  text that was spoken, separated by blank lines.

Older pairs are pruned on each new run.

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
