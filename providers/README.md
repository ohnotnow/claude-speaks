# Adding a TTS provider

Each TTS backend lives in one file under `providers/`. Drop in
`providers/<your_name>.py`, point `config.json` at it, set the env var, done.
The file is auto-discovered — no central registry to edit, no import in
`main.py`.

If a provider file fails to import, it gets logged as
`<provider load error>` and skipped. The hook keeps working with whatever
else loaded.

## The contract

Your file needs:

- A subclass of `Provider` (from `providers.base`).
- A `name` class attribute — the string a user sets as `tts_provider` in
  `config.json`.
- An `api_key_env` class attribute — the name of the env var holding the
  API key, or `None` for a local provider that doesn't need one.
- A `default_voices` dict mapping role to voice id, with the three roles
  `main`, `monologue`, and `notification`.
- Implementations of `plan_stop_clips`, `plan_notification_clip`, and
  `synthesise`.
- A module-level `PROVIDER = YourProviderClass` so auto-discovery picks it up.

The base class gives you a sensible `voice_for(role)` and `language_for(role)`
that read from `self.voices_config` (the user's per-provider voice block in
`config.json`) and fall back to your `default_voices`. Override `voice_for`
only if your provider does something unusual — see Mistral's `_<style>`
suffix trick in `providers/mistral.py`.

## What each method does

`plan_stop_clips(text) -> list[Clip]` — Claude finished a turn. The
markdown-stripped reply is in `text`. Return a list of `Clip`s to be spoken
in order. Run whatever LLM calls you need (classifier, preamble generator,
summariser) via `self.llm.complete(system_prompt, user_text)`. Run them in
parallel with a `ThreadPoolExecutor` if there's more than one — Claude Code
blocks on the hook returning, so latency matters.

Respect `self.features` — a `{"monologue": bool, "main": bool, "notification": bool}`
dict from `config.json`. If `monologue` is off, skip the preamble LLM call and
don't emit the preamble clip; if `main` is off, skip the summariser (and any
classifier) and don't emit the main clip. Don't pay for an LLM call whose
output you'll throw away.

`plan_notification_clip() -> Clip | None` — Claude has gone idle or is
waiting for permission. Return a single `Clip` for the spoken nag, or
`None` to play the system fallback sound. The project-level
`history.load_notification_history()` gives you the last 10 lines so you
can feed them back into the prompt and stop the LLM repeating itself.
`main.py` handles the *appending* side of history — you don't need to.
The `features.notification` toggle is enforced in `main.py` before this
method is called, so you can assume the user wants a clip.

`synthesise(clip) -> bytes | None` — Turn one `Clip` into mp3 bytes. The
HTTP call lives here. Catch your own exceptions, log with a tag like
`<yourname synth>` or `<yourname error>`, return `None` on failure.

## The sample-rate landmine

`audio.play_clips` byte-concatenates synthesised mp3s with a silent gap mp3
between them. No demuxing, no ffmpeg. The shipped gaps in `gaps/` are
**22050 Hz mono / 64 kbps**. If your provider emits anything else,
`afplay` will silently truncate playback at the boundary — the second clip
just doesn't play, no error.

Either match the rate when you call your TTS API (`xai.py` does this
explicitly via the `sample_rate` and `bit_rate` payload fields), or
replace the gap mp3s and document the new rate.

## Provider-specific settings

If your provider has knobs the user might want to override — model id,
output format, speaker similarity, voice stability, whatever — accept them
via `self.settings`. The user puts them under
`provider_settings.<your_name>` in `config.json` and `main.py` passes that
slice to your constructor.

Provide sensible defaults so an empty or missing `provider_settings` block
still works. See `xai.py` for `sample_rate` / `bit_rate` overrides as a
worked example.

## Errors are audible

The user is making coffee. Silence is worse than a slightly-wrong line.

- Catch exceptions in every LLM-call wrapper. Return `(fallback_value, "tag")`
  where `tag` names what fell over. `plan_stop_clips` then prepends a spoken
  "Heads up — the classifier call fell over" before the reply.
- Catch exceptions in `synthesise` and return `None`. `audio.play_clips` will
  log a `<partial>` or `<fallback>` and play whichever clips made it.
- Don't let one bad LLM response kill the whole turn.

See `providers/mistral.py` for the canonical error-prepend pattern.

## Two worked examples

- **`providers/mistral.py`** — suffix-style. Voice ids are prefixes; a
  tone classifier picks one of nine emotional styles and `_<style>` is
  appended at synthesis time (`gb_jane` → `gb_jane_sarcasm`). Three
  parallel LLM calls. No inline markup. Look here if your provider
  ships per-emotion voice variants.
- **`providers/xai.py`** — inline-tag style. Voice ids are literal. No
  classifier — the summariser embeds prosody tags (`<soft>`,
  `<emphasis>`, `<slow>`) directly in the rewritten text. Two parallel
  LLM calls. Look here if your provider accepts SSML-ish or audio-tag
  markup (ElevenLabs' `[whispers]` / `[laughs]`, OpenAI's instructions,
  etc.).

## Skeleton

The minimum file that auto-discovers, registers, and works:

```python
"""My TTS provider."""

from providers.base import Clip, Provider


SUMMARY_PROMPT = """Compress this assistant reply for TTS playback. ..."""


class MyTTSProvider(Provider):
    name = "mytts"
    api_key_env = "MYTTS_API_KEY"
    default_voices = {
        "main": "alice",
        "monologue": "bob",
        "notification": "bob",
    }

    def plan_stop_clips(self, text):
        summary = self.llm.complete(SUMMARY_PROMPT, text)
        return [Clip(summary, self.voice_for("main"))]

    def plan_notification_clip(self):
        line = self.llm.complete("You are Marvin. One short line.", "")
        if not line:
            return None
        return Clip(line, self.voice_for("notification"))

    def synthesise(self, clip):
        # POST to your TTS API. Return mp3 bytes (22050 Hz mono, ideally) or None on failure.
        ...


PROVIDER = MyTTSProvider
```

Drop that into `providers/mytts.py`, set `MYTTS_API_KEY` in your env, set
`tts_provider: "mytts"` in `config.json`, and you're live.
