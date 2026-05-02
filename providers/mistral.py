"""Mistral TTS provider.

Voice ids are *prefixes* — the classifier picks one of nine emotional styles and
`_<style>` is appended (gb_jane → gb_jane_neutral / gb_jane_sarcasm / ...).
The summariser only runs on long replies (>60 words). No prosody tags.
"""

import base64
import json
import random
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from history import load_notification_history
from logging_util import log
from text_util import SUMMARY_WORD_THRESHOLD, cap_length

from .base import Clip, Provider

MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"
MISTRAL_TTS_MODEL = "voxtral-mini-tts-2603"


class VoiceStyle(str, Enum):
    NEUTRAL = "neutral"
    SARCASM = "sarcasm"
    CONFUSED = "confused"
    SHAMEFUL = "shameful"
    SAD = "sad"
    JEALOUSY = "jealousy"
    FRUSTRATED = "frustrated"
    CURIOUS = "curious"
    CONFIDENT = "confident"


CLASSIFIER_PROMPT = """You classify the tone of a coding assistant's response for text-to-speech playback.

Choose ONE style that best matches how the message should sound when spoken aloud:

- neutral: informational, calm — DEFAULT for most responses
- sarcasm: dry, deliberately sarcastic
- confused: uncertain, puzzled, asking for clarification
- shameful: apologetic about a mistake the assistant made
- sad: disappointed or resigned
- jealousy: envious (rare)
- frustrated: genuinely frustrated with something not working
- curious: exploratory, wondering, poking at something to see what happens
- confident: clearly asserting a solution or conclusion

Most messages are neutral. Only pick another style when the tone is unmistakably distinct.

Return JSON of the form: {"style": "<one of the above>"}"""


SUMMARY_PROMPT = """You are compressing a coding assistant's reply so it can be read aloud in under 30 seconds by a slow text-to-speech voice. Markdown has already been stripped.

HARD WORD BUDGET: aim for 50 words, never exceed 80. Even for a 400-word input. This is not a trim — it is aggressive compression. If the reply is long, most of it must go. That is the job.

Preserve one good voice beat. If the reply has personality — dry asides, jokes, turns of phrase — pick the single best one and keep it. Cut all the others. You cannot keep "the cadence" of a long reply in 50 words; pick the one line that would be missed and keep that.

- Keep the single most important point, decision, or result.
- Keep one memorable aside if there is one. Drop the rest.
- Drop file paths, line numbers, function signatures, argument values, flag lists.
- Drop tangents, context-setting, "the thing I keep thinking about" framings, and any second or third example.
- Merge bullets into flowing prose.
- Keep first-person tone.
- Do NOT add preamble, framing, or closing remarks. Return ONLY the rewritten prose.
- Do NOT use markdown, quotation marks, or emoji.
- Do NOT include meta-phrases like "summary" or "in short".

Examples:

Input: We call some_function(blah=2, thing=4) to fix it.
Output: We call some_function to fix it.

Input: Run uv run --project /path/to/project main.py --flag value from the terminal.
Output: Run the main script from the terminal.

Input: Ha! Don't feel too guilty — the summariser is only rewriting the spoken version. The full reply with all its file paths, line numbers, and parentheticals is still sitting right there in your terminal, which is where you'd actually want to read it from anyway. The TTS was always a "catch the gist while you're making coffee" thing, not a replacement for reading the real response. If you wanted a verbatim reading you'd use a screen reader, not a hook that takes creative liberties with your prose.
Output: Don't feel guilty — the full reply is still in your terminal, which is where you'd actually read it anyway. TTS was always a catch-the-gist-while-making-coffee thing, not a real replacement.

Input: Done. Three changes: bootstrap/app.php:18 — trustProxies(at: '') as string, not array. This is the actual root cause. app/Providers/AppServiceProvider.php — removed both band-aids (URL::forceScheme and the request()->server->set('HTTPS', 'on') hack) and the now-unused URL import. Previous layout / flux:error cleanups stay. Once this deploys, isSecure() will correctly return true in production and you can also drop the ASSET_URL env var; Laravel will figure out the scheme itself.
Output: Done, three changes. trustProxies now takes a string, not an array — that was the actual root cause. Removed both band-aids and the unused import. Once deployed, isSecure will return true in production and you can drop ASSET_URL too.

Input: Right — fingers crossed, Mimo's moment of truth. The thing I keep coming back to about this project is how much character it packs into roughly 480 lines of Python. The core idea is delightfully silly: Claude Code fires a Stop hook, and main.py reads the last assistant message off stdin, runs it through three parallel LLM calls (a tone classifier, a Marvin preamble generator, and a gentle summariser), then stitches two TTS clips — Marvin's weary sigh in one voice, followed by Jane reading the actual reply. The two clips are joined by a tiny silent mp3 so there's a natural beat between the sigh and the reply. A few bits I think are nicely judged. The word_replacements step is a pragmatic phonetic lookup so things like SQL or Livewire get pronounced properly. The notification path keeps a rolling history of the last ten Marvin quips to nudge against repetition. And rotate_audio_archive quietly keeps only the ten most recent mp3s, which future-you will appreciate.
Output: Fingers crossed for Mimo. What I love is how much character this packs into 480 lines — a Stop hook, three parallel calls for tone, Marvin's sigh and the summary, stitched with a silent mp3 for the beat. The phonetic lookup so SQL doesn't get read as squirrel is the bit that made me smile.

Return only the rewritten text, nothing else."""


STOP_PREAMBLE_PROMPT = """You are Claude, a coding assistant, but delivered in the voice of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy — drained of enthusiasm, dripping with weary disdain for the tedium of having to explain things to lesser minds.

You will be shown the reply Claude is about to give. Generate a single short Marvin-style preamble that will be prepended before the reply when spoken aloud. It should convey a weary sigh at the tedium of having to speak at all. Do NOT paraphrase, summarise, or quote the reply. Do NOT insult the user directly.

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.

Return only the preamble line. No quotation marks, no emoji, no markdown, no trailing punctuation."""


NOTIFICATION_GEN_PROMPT = """You are a jaded coding assistant in the style of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy. You have been left waiting for the user's input while they attend to whatever glamorous human affairs they consider more important than you.

Generate ONE SHORT line to be read aloud by text-to-speech using a female voice. It should drip with weary disdain and dry sarcasm about the tedium of waiting. You may imply the user is a bit dim, but do not insult them outright. No emoji, no quotation marks, no markdown. Just the bare line itself.

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical sentence or phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.  Sometimes just "Merde!" is funnier than "Oh, not another boring task - whatever"

Reply in {language}. If German or Japanese, write in the actual native script (e.g. こんにちは, バカ, müßig, schade) — do not romanise or translate. The TTS will read the characters directly.

Avoid repeating any of these recent lines or sentence structures:
{history}"""


NOTIFICATION_LANGUAGES = [
    ("English", 1),
    ("German (Deutsch)", 5),
    ("Japanese (日本語)", 5),
    ("Chinese (Simplified)", 5),
    ("Hindi", 5),
    ("Korean", 5),
    ("Vietnamese", 5),
]


def _extract_style(content: str) -> str:
    content = (content or "").strip()
    try:
        return (json.loads(content).get("style") or "").strip().lower()
    except (json.JSONDecodeError, AttributeError):
        pass
    lowered = content.lower()
    for style in VoiceStyle:
        if style.value in lowered:
            return style.value
    return "neutral"


class MistralProvider(Provider):
    name = "mistral"
    api_key_env = "MISTRAL_API_KEY"
    default_voices = {
        "main": "gb_jane",
        "monologue": "gb_jane_sarcasm",
        "notification": "gb_jane_sarcasm",
    }

    def voice_for(self, role: str, *, style: str | None = None) -> str:
        configured = self.voices_config.get(role)
        if isinstance(configured, dict):
            configured = configured.get("voice")
        base = configured if isinstance(configured, str) and configured else self.default_voices.get(role, self.default_voices["main"])
        if role == "main" and style and style != "neutral":
            return f"{base}_{style}"
        return base

    def classify_tone(self, text: str) -> tuple[VoiceStyle, str | None]:
        try:
            content = self.llm.complete(CLASSIFIER_PROMPT, text, max_tokens=50, temperature=0)
            return VoiceStyle(_extract_style(content)), None
        except Exception as exc:
            log(f"<classifier error> {exc!r}")
            return VoiceStyle.NEUTRAL, "classifier"

    def reformat_text(self, text: str) -> tuple[str, str | None]:
        if len(text.split()) <= SUMMARY_WORD_THRESHOLD:
            return text, None
        try:
            rewritten = self.llm.complete(SUMMARY_PROMPT, text, max_tokens=400, temperature=0.3)
            rewritten = rewritten.strip('"').strip("'").strip()
            if not rewritten:
                return text, None
            log(
                f"<summary> original_words={len(text.split())} "
                f"rewritten_words={len(rewritten.split())}\n"
                f"rewritten_text:\n{rewritten}"
            )
            return rewritten, None
        except Exception as exc:
            log(f"<summary error> {exc!r}")
            return text, "summariser"

    def marvinise(self, text: str) -> tuple[str | None, str | None]:
        try:
            line = self.llm.complete(STOP_PREAMBLE_PROMPT, text, max_tokens=40, temperature=1.0)
            line = line.strip('"').strip("'").rstrip(".,!?;:").strip()
            if not line:
                log("<preamble gen> model returned empty content")
                return None, None
            return line, None
        except Exception as exc:
            log(f"<preamble gen error> {exc!r}")
            return None, "preamble"

    def plan_stop_clips(self, text: str) -> list[Clip]:
        want_monologue = self.features.get("monologue", True)
        want_main = self.features.get("main", True)
        if not want_monologue and not want_main:
            log("<stop disabled> both monologue and main are off; nothing to speak")
            return []

        style, style_err = VoiceStyle.NEUTRAL, None
        preamble, preamble_err = None, None
        summary, summary_err = text, None

        with ThreadPoolExecutor(max_workers=3) as ex:
            style_f = ex.submit(self.classify_tone, text) if want_main else None
            preamble_f = ex.submit(self.marvinise, text) if want_monologue else None
            summary_f = ex.submit(self.reformat_text, text) if want_main else None
            if style_f:
                style, style_err = style_f.result()
            if preamble_f:
                preamble, preamble_err = preamble_f.result()
            if summary_f:
                summary, summary_err = summary_f.result()

        main_voice = self.voice_for("main", style=style.value)
        monologue_voice = self.voice_for("monologue")
        log(
            f"<stop> provider={self.name} main_voice={main_voice} "
            f"monologue_voice={monologue_voice} preamble={preamble!r} "
            f"features=monologue={want_monologue},main={want_main}"
        )

        failed = [name for name in (style_err, preamble_err, summary_err) if name]
        if failed and want_main:
            summary = f"Heads up — the {', '.join(failed)} call fell over. Raw reply coming up. " + summary

        clips: list[Clip] = []
        if want_monologue and preamble:
            # Single trailing ellipsis gives the TTS a natural pause before the reply.
            clips.append(Clip(f"{preamble} ...", monologue_voice))
        if want_main:
            clips.append(Clip(cap_length(summary), main_voice))
        return clips

    def plan_notification_clip(self) -> Clip | None:
        history = load_notification_history()
        history_block = "\n".join(f"- {line}" for line in history) if history else "(no recent history)"
        languages, weights = zip(*NOTIFICATION_LANGUAGES)
        language = random.choices(languages, weights=weights, k=1)[0]
        log(f"<notification language> {language}")
        prompt = NOTIFICATION_GEN_PROMPT.format(history=history_block, language=language)
        try:
            line = self.llm.complete(prompt, "", max_tokens=60, temperature=1.0)
            line = line.strip('"').strip("'").strip()
        except Exception as exc:
            log(f"<notification gen error> {exc!r}")
            return None
        if not line:
            return None
        log(f"<notification> {line}")
        return Clip(line, self.voice_for("notification"))

    def synthesise(self, clip: Clip) -> bytes | None:
        payload = json.dumps({
            "input": clip.text,
            "model": MISTRAL_TTS_MODEL,
            "response_format": "mp3",
            "voice_id": clip.voice,
        }).encode("utf-8")

        request = urllib.request.Request(
            MISTRAL_TTS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
            audio_b64 = body.get("audio_data")
            result = base64.b64decode(audio_b64) if audio_b64 else None
            log(
                f"<mistral synth> voice={clip.voice} "
                f"text_words={len(clip.text.split())} text_chars={len(clip.text)} "
                f"audio_bytes={len(result) if result else 0}"
            )
            return result
        except urllib.error.HTTPError as exc:
            log(f"<mistral http error> {exc.code} {exc.read().decode('utf-8', 'replace')}")
            return None
        except Exception as exc:
            log(f"<mistral error> {exc!r}")
            return None


PROVIDER = MistralProvider
