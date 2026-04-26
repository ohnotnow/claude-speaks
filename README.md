# claude-speaks

**Work in progress.** A small experiment: have Claude Code read its final
reply out loud, so you can wander off and make a coffee while it works and
still catch what it said.

It's a [Claude Code Stop hook](https://docs.claude.com/en/docs/claude-code/hooks)
that:

1. Catches the `last_assistant_message` when Claude finishes a turn.
2. Strips markdown and drops fenced code blocks.
3. Fans out three parallel LLM calls via LiteLLM (model configurable, see
   Configuration) — one to classify the tone into one of nine emotional
   styles, one to generate a short Marvin-the-Paranoid-Android-style sigh
   to play before the reply, and one to compress the reply down to roughly
   50 spoken words if it's longer than 60.
4. Synthesises two TTS clips via Mistral's `/v1/audio/speech` — the Marvin
   sigh in a monologue voice (e.g. `fr_marie_sad`), then the reply in a
   Jane voice matching the classified tone (e.g. `gb_jane_confident`).
5. Stitches them with a short silent mp3 gap and plays the result via
   `afplay` as a detached subprocess.

There's also a Notification handler for when Claude Code is idle or
waiting for permission — Marvin pipes up with a short weary quip in the
monologue voice, with a rolling history of recent lines fed back into
the prompt to keep him from repeating himself.

If any of the LLM calls fail, the hook prepends a short spoken heads-up
("heads up — the summariser call fell over, raw reply coming up") before
the reply, so silent failures are audible rather than mysterious.

## Requirements

- macOS (uses `afplay` for playback)
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- A Mistral API key (or an xAI API key — see [Switching to xAI](#switching-to-xai))

## Setup

```bash
git clone git@github.com:ohnotnow/claude-speaks.git
cd claude-speaks
uv sync
```

Create a `.env` file with your Mistral key (and any other provider keys you
need for `llm_model`):

```
MISTRAL_API_KEY=your-key-here
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

## Configuration

API keys live in `.env`. Everything else lives in `config.json` (copy
`config.example.json` to get started). If `config.json` is missing or a key
is absent, the defaults below kick in.

`.env`:

| Env var | Default | Notes |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required when `tts_provider` is `mistral`. Also used for the LLM calls if `llm_model` points at a Mistral model. |
| `XAI_API_KEY` | — | Required when `tts_provider` is `xai`. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. | — | Only needed if `llm_model` points at that provider. |

`config.json`:

| Key | Default | Notes |
|---|---|---|
| `llm_model` | `mistral/mistral-small-latest` | Any LiteLLM-supported model used for the classifier, preamble, summariser, and notification lines. Try `mistral/ministral-3b-latest` for speed, or `anthropic/claude-haiku-4-5-20251001` for quality. |
| `tts_provider` | `mistral` | Either `mistral` or `xai`. See [Switching to xAI](#switching-to-xai) for what changes when you flip this. |
| `voice_base` | `gb_jane` | On Mistral: prefix for the main reading voice (the nine-style suffix is appended automatically). Try `gb_oliver`, `gb_paul`, `fr_marie`. On xAI: a literal voice id like `Eve` — no suffix is appended because xAI uses inline tags for prosody. |
| `voice_monologue` | `<voice_base>_sarcasm` | Full voice id for Marvin's internal-monologue bits (the preamble on Stop, and idle-waiting Notifications). On Mistral, try `fr_marie_sad` for proper Paranoid Android vibes. On xAI, use any voice id — the trick is the language knob below. |
| `tts_language` | `en` | xAI only. ISO language code for Jane's reply. |
| `tts_language_monologue` | `en` | xAI only. Language code for Marvin's lines. **Set this to `fr`** — none of xAI's voices come close to `fr_marie_sad`'s dejection on their own, but speaking English text with a French language hint adds the necessary world-weary drip. The French win, as ever. |
| `gap_file` | `0_75s` | Which silent mp3 in `gaps/` to stitch between the preamble and the main reply. See below. |
| `word_replacements` | `{}` | Phonetic swap map — see [Word replacements](#word-replacements). |

Jane's nine emotional styles (Mistral only): `neutral`, `sarcasm`, `confused`,
`shameful`, `sad`, `jealousy`, `frustrated`, `curious`, `confident`. The
classifier is biased towards `neutral`, so you'll mostly hear that one and
only get the other flavours when Claude's reply genuinely calls for it.

### Switching to xAI

xAI's TTS doesn't have an emotional-style enum. Instead it accepts a small
set of inline prosody tags wrapped around spans of text — `<soft>`,
`<emphasis>`, `<slow>`, `<lower-pitch>`, and so on. When `tts_provider` is
`xai`:

- The classifier call is skipped — `voice_base` is used as a literal voice
  id (e.g. `Eve`), no nine-style suffix.
- The summariser and preamble prompts are swapped for xAI variants that
  list the allowed tags and ask the LLM to wrap a span or two where it
  genuinely aids delivery.
- The summariser runs even on short replies, so a "Done." can still pick
  up a `<slow>` if the model thinks it deserves one.
- Marvin still gets his moment — the trick is `"tts_language_monologue":
  "fr"`, which has xAI speak Marvin's English line with a French inflection
  that's much closer to the right level of resentment than any of xAI's
  default voices manage on their own.

Example xAI config:

```json
{
  "llm_model": "mistral/mistral-small-latest",
  "tts_provider": "xai",
  "voice_base": "Eve",
  "voice_monologue": "Eve",
  "tts_language": "en",
  "tts_language_monologue": "fr"
}
```

Unknown or malformed tags are ignored by xAI rather than rejected, so no
sanitiser is needed — Claude's reply goes through as-is.

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

The shipped gaps are 22050 Hz mono at 56 kbps. Any custom gap should match
that — afplay refuses to cross sample-rate boundaries cleanly when
stitching, so a 44.1 kHz gap between two 22 kHz clips will silently
truncate playback at the boundary.

### Word replacements

Mistral's TTS mispronounces plenty of technical jargon — `vite` comes out
as "vite" (rhymes with "kite") rather than "veet", for example. Add a
`word_replacements` object to `config.json` with a flat map of problem
words to phonetic spellings and they'll be swapped in before the text
hits the TTS. Matching is case-insensitive and on word boundaries, so
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

## Known rough edges

- On Mistral, the nine-style enum assumes Jane's flavour set — other Mistral
  voices may not have all nine `_<style>` variants.
- Markdown stripping is regex-based and unsubtle.
- The hook blocks for roughly 2–3 seconds while the classifier and TTS
  calls complete. Playback itself is detached and non-blocking.
- macOS-only, because of `afplay`.
- No way to interrupt Jane mid-sentence. If she's reading a long response
  and you want her to stop, `killall afplay` does the job.

## Log

Every Stop event and classifier pick gets appended to `stop-hook.log`.
Handy when tuning the classifier prompt or chasing down voice 404s.

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
