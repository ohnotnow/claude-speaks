"""ElevenLabs TTS provider.

Voice ids are literal (the long opaque ElevenLabs voice id strings). No tone
classifier — the summariser embeds inline ElevenLabs audio tags ([laughs],
[whispers], [sighs], …) directly in the rewritten text so it always runs,
even on short replies. Same shape as the xAI provider; only the markup
vocabulary differs.

Sample-rate landmine: the synth call pins output_format to mp3_22050_32 to
match the shipped gap mp3s. Override via `provider_settings.elevenlabs.output_format`
if you swap the gap files; mismatched rates cause the second clip to silently
truncate during playback.

Uses the ElevenLabs Python SDK (already a dep). The SDK's text_to_speech.convert
returns an iterator of byte chunks — we collect them into a single bytes object
so the rest of the pipeline can byte-concat with the gap mp3.
"""

import random
from concurrent.futures import ThreadPoolExecutor

from elevenlabs.client import ElevenLabs

from config import notification_languages
from history import load_notification_history
from logging_util import log
from prompts import safe_format
from text_util import cap_length

from .base import Clip, Provider

DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_OUTPUT_FORMAT = "mp3_22050_32"


class ElevenLabsProvider(Provider):
    name = "elevenlabs"
    api_key_env = "ELEVENLABS_API_KEY"
    default_voices = {
        "main": "6qpxBH5KUSDb40bij36w",
        "monologue": "6qpxBH5KUSDb40bij36w",
        "notification": "6qpxBH5KUSDb40bij36w",
    }

    def reformat_text(self, text: str) -> tuple[str, str | None]:
        try:
            system_prompt = self.prompt("summary")
            persona = self.persona("main")
            if persona:
                system_prompt += (
                    f"\n\nThe reply you are about to compress is written in the voice of: {persona}. "
                    "Preserve a beat that captures that voice."
                )
            rewritten = self.llm.complete(system_prompt, text, max_tokens=400, temperature=0.3)
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
            system_prompt = safe_format(
                self.prompt("preamble"),
                persona=self.persona("monologue") or "",
            )
            line = self.llm.complete(system_prompt, text, max_tokens=40, temperature=1.0)
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
        prompt = safe_format(
            self.prompt("notification"),
            history=history_block,
            language=language,
            persona=self.persona("notification") or "",
        )
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
        model_id = self.settings.get("model_id", DEFAULT_MODEL_ID)
        output_format = self.settings.get("output_format", DEFAULT_OUTPUT_FORMAT)
        try:
            client = ElevenLabs(api_key=self.api_key)
            audio_iter = client.text_to_speech.convert(
                text=clip.text,
                voice_id=clip.voice,
                model_id=model_id,
                output_format=output_format,
            )
            result = b"".join(audio_iter) or None
            log(
                f"<elevenlabs synth> voice={clip.voice} model={model_id} "
                f"format={output_format} text_words={len(clip.text.split())} "
                f"text_chars={len(clip.text)} audio_bytes={len(result) if result else 0}"
            )
            return result
        except Exception as exc:
            log(f"<elevenlabs error> {exc!r}")
            return None


PROVIDER = ElevenLabsProvider
