"""xAI TTS provider.

Voice ids are literal. No tone classifier — the summariser embeds inline
prosody tags (<soft>, <emphasis>, <slow>, …) directly in the rewritten text
so it always runs, even on short replies.

Sample-rate landmine: the synth payload pins output at 22050 Hz / 64 kbps to
match the shipped gap mp3s. Override via `provider_settings.xai.sample_rate`
and `.bit_rate` if you swap the gap files for something else; mismatched
rates cause the second clip to silently truncate during playback.
"""

import json
import random
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from config import notification_languages
from history import load_notification_history
from logging_util import log
from prompts import safe_format
from text_util import cap_length

from .base import Clip, Provider

XAI_TTS_URL = "https://api.x.ai/v1/tts"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_BIT_RATE = 64000


class XAIProvider(Provider):
    name = "xai"
    api_key_env = "XAI_API_KEY"
    default_voices = {
        "main": "Eve",
        "monologue": "Eve",
        "notification": "Eve",
    }

    def reformat_text(self, text: str) -> tuple[str, str | None]:
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

        preamble, preamble_err = None, None
        summary, summary_err = text, None

        with ThreadPoolExecutor(max_workers=2) as ex:
            preamble_f = ex.submit(self.marvinise, text) if want_monologue else None
            summary_f = ex.submit(self.reformat_text, text) if want_main else None
            if preamble_f:
                preamble, preamble_err = preamble_f.result()
            if summary_f:
                summary, summary_err = summary_f.result()

        main_voice = self.voice_for("main")
        monologue_voice = self.voice_for("monologue")
        main_lang = self.language_for("main")
        monologue_lang = self.language_for("monologue")
        log(
            f"<stop> provider={self.name} main_voice={main_voice} "
            f"monologue_voice={monologue_voice} preamble={preamble!r} "
            f"features=monologue={want_monologue},main={want_main}"
        )

        failed = [n for n in (preamble_err, summary_err) if n]
        if failed and want_main:
            summary = f"Heads up — the {', '.join(failed)} call fell over. Raw reply coming up. " + summary

        clips: list[Clip] = []
        if want_monologue and preamble:
            # Single trailing ellipsis gives the TTS a natural pause before the reply.
            clips.append(Clip(f"{preamble} ...", monologue_voice, monologue_lang))
        if want_main:
            clips.append(Clip(cap_length(summary), main_voice, main_lang))
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
        return Clip(line, self.voice_for("notification"), self.language_for("notification"))

    def synthesise(self, clip: Clip) -> bytes | None:
        sample_rate = self.settings.get("sample_rate", DEFAULT_SAMPLE_RATE)
        bit_rate = self.settings.get("bit_rate", DEFAULT_BIT_RATE)
        payload = json.dumps({
            "text": clip.text,
            "voice_id": clip.voice,
            "output_format": {"codec": "mp3", "sample_rate": sample_rate, "bit_rate": bit_rate},
            "language": clip.language,
        }).encode("utf-8")

        request = urllib.request.Request(
            XAI_TTS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = response.read() or None
            log(
                f"<xai synth> voice={clip.voice} lang={clip.language} "
                f"text_words={len(clip.text.split())} text_chars={len(clip.text)} "
                f"audio_bytes={len(result) if result else 0}"
            )
            return result
        except urllib.error.HTTPError as exc:
            log(f"<xai http error> {exc.code} {exc.read().decode('utf-8', 'replace')}")
            return None
        except Exception as exc:
            log(f"<xai error> {exc!r}")
            return None


PROVIDER = XAIProvider
