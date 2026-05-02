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

from config import notification_languages
from history import load_notification_history
from logging_util import log
from prompts import safe_format
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
            content = self.llm.complete(self.prompt("classifier"), text, max_tokens=50, temperature=0)
            return VoiceStyle(_extract_style(content)), None
        except Exception as exc:
            log(f"<classifier error> {exc!r}")
            return VoiceStyle.NEUTRAL, "classifier"

    def reformat_text(self, text: str) -> tuple[str, str | None]:
        if len(text.split()) <= SUMMARY_WORD_THRESHOLD:
            return text, None
        try:
            rewritten = self.llm.complete(self.prompt("summary"), text, max_tokens=400, temperature=0.3)
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
            line = self.llm.complete(self.prompt("preamble"), text, max_tokens=40, temperature=1.0)
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
        languages, weights = zip(*notification_languages())
        language = random.choices(languages, weights=weights, k=1)[0]
        log(f"<notification language> {language}")
        prompt = safe_format(self.prompt("notification"), history=history_block, language=language)
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
