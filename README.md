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
- A Mistral API key

## Setup

```bash
git clone git@github.com:ohnotnow/claude-speaks.git
cd claude-speaks
uv sync
```

Create a `.env` file with your Mistral key:

```
MISTRAL_API_KEY=your-key-here
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

| Env var | Default | Notes |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required. Used for TTS synthesis. Also used for the LLM calls if `LLM_MODEL` points at a Mistral model. |
| `LLM_MODEL` | `mistral/mistral-small-latest` | Any LiteLLM-supported model used for the classifier, preamble, summariser, and notification lines. Try `mistral/ministral-3b-latest` for speed, or `anthropic/claude-haiku-4-5-20251001` for quality. Set the matching provider API key in `.env` (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). |
| `VOICE_BASE` | `gb_jane` | Prefix for the main reading voice. Swap to `gb_oliver`, `gb_paul`, `fr_marie`, etc. |
| `VOICE_MONOLOGUE` | `<VOICE_BASE>_sarcasm` | Full voice id for Marvin's internal-monologue bits (the preamble on Stop, and idle-waiting Notifications). Try `fr_marie_sad` for proper Paranoid Android vibes. |
| `GAP_FILE` | `0_75s` | Which silent mp3 in `gaps/` to stitch between the preamble and the main reply. See below. |

Jane's nine emotional styles: `neutral`, `sarcasm`, `confused`, `shameful`,
`sad`, `jealousy`, `frustrated`, `curious`, `confident`. The classifier is
biased towards `neutral`, so you'll mostly hear that one and only get the
other flavours when Claude's reply genuinely calls for it.

### Gaps between clips

When a Stop event triggers both a Marvin preamble and a main reply, the two
synthesised mp3s are stitched into a single file and played back-to-back.
To stop them running into each other, a short chunk of silence gets spliced
in between.

The `gaps/` directory holds a few pre-rendered silent mp3s:

- `0_5s.mp3` — half a second, snappy
- `0_75s.mp3` — three-quarters of a second (default)
- `1_0s.mp3` — a full second, more theatrical

Pick one with the `GAP_FILE` env var (no extension — e.g. `GAP_FILE=1_0s`).
To add your own, drop another silent mp3 into `gaps/` and reference it by
filename. Any mp3-encoding tool will do; `ffmpeg` is the usual suspect:

```bash
ffmpeg -f lavfi -i anullsrc=r=24000:cl=mono -t 1.5 -q:a 9 gaps/1_5s.mp3
```

### Word replacements

Mistral's TTS mispronounces plenty of technical jargon — `vite` comes out
as "vite" (rhymes with "kite") rather than "veet", for example. Drop a
`word_replacements.json` in the project directory with a flat map of
problem words to phonetic spellings and they'll be swapped in before the
text hits the TTS. Matching is case-insensitive and on word boundaries,
so `Vite` and `vite` both get caught but `invitation` doesn't.

```json
{
  "vite": "veet",
  "nginx": "engine-ex",
  "kubectl": "koob-control"
}
```

A `word_replacements.example.json` ships with the repo — copy it to
`word_replacements.json` and edit to taste. If the file's missing or
malformed, the hook just skips the step.

## Known rough edges

- Voices are configured via env vars (see Configuration), but the nine-style
  enum still assumes Jane's flavour set — other voices may not have all nine.
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
