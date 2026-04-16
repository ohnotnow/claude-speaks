# claude-speaks

**Work in progress.** A small experiment: have Claude Code read its final
reply out loud, so you can wander off and make a coffee while it works and
still catch what it said.

It's a [Claude Code Stop hook](https://docs.claude.com/en/docs/claude-code/hooks)
that:

1. Catches the `last_assistant_message` when Claude finishes a turn.
2. Strips markdown and drops fenced code blocks
3. If the reply runs longer than about 25 words, asks a small LLM to rewrite
   it into a TTS-friendly version — keeping the technical point but dropping
   verbose function signatures, argument values, absolute file paths, and
   other detail that sounds ugly spoken.
4. Asks a small LLM (Mistral Small, via LiteLLM) to classify the tone as
   one of the nine emotional styles Mistral's TTS supports for the 'Jane' voice.
5. Synthesises audio via Mistral's `/v1/audio/speech` using the matching
   voice (e.g. `gb_jane_curious`, `gb_jane_confident`).
6. Plays it through `afplay` as a detached subprocess.

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
| `MISTRAL_API_KEY` | — | Required. Used for both classifier and TTS. |
| `CLASSIFIER_MODEL` | `mistral/mistral-small-latest` | Any LiteLLM-supported model. Try `mistral/ministral-3b-latest` for speed. |
| `VOICE_BASE` | `gb_jane` | Prefix for the main reading voice. Swap to `gb_oliver`, `gb_paul`, `fr_marie`, etc. |
| `VOICE_MONOLOGUE` | `<VOICE_BASE>_sarcasm` | Full voice id for Marvin's internal-monologue bits (the preamble on Stop, and idle-waiting Notifications). Try `fr_marie_sad` for proper Paranoid Android vibes. |

Jane's nine emotional styles: `neutral`, `sarcasm`, `confused`, `shameful`,
`sad`, `jealousy`, `frustrated`, `curious`, `confident`. The classifier is
biased towards `neutral`, so you'll mostly hear that one and only get the
other flavours when Claude's reply genuinely calls for it.

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

## Licence

MIT. See [LICENSE](LICENSE).
