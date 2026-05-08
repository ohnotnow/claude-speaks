"""OpenAI TTS provider.

Voice ids are literal — one of the named voices listed in OpenAI's docs (alloy,
ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin,
cedar). Model defaults to gpt-4o-mini-tts.

Tone/style is controlled via the `instructions` parameter on gpt-4o-mini-tts,
not via inline tags. Per-role defaults live in DEFAULT_INSTRUCTIONS; override
by adding "instructions": "..." alongside "voice" in voices.openai.<role> in
config.json. The summariser only runs on long replies (Mistral-style threshold)
since there's no inline markup to add to short ones.

Sample-rate landmine: OpenAI's mp3 output is fixed at 24 kHz / 128 kbps and
the API doesn't expose sample-rate knobs. The shipped gaps in `gaps/` are
22050 Hz, so synthesise asks OpenAI for `wav` and pipes it through ffmpeg to
produce mp3 at the matching rate. Without ffmpeg on PATH this provider can't
work — install it or replace the gap mp3s and document the new rate.
Override target rate via `provider_settings.openai.sample_rate` /
`.bit_rate` if you swap the gaps.
"""

import json
import random
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from config import notification_languages
from history import load_notification_history
from logging_util import log
from prompts import safe_format
from text_util import SUMMARY_WORD_THRESHOLD, cap_length

from .base import Clip, Provider

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_BIT_RATE = 56000

DEFAULT_INSTRUCTIONS = {
    "main": "Speak clearly and naturally, in a calm measured tone — like a helpful colleague summarising over coffee.",
    "monologue": "Speak in the voice of Marvin the Paranoid Android: weary, deadpan, drained of enthusiasm, dry and faintly disdainful. Slow and slightly resigned.",
    "notification": "Speak in the voice of Marvin the Paranoid Android: weary, deadpan, drained of enthusiasm, dry and faintly disdainful. Slow and slightly resigned.",
}


class OpenAIProvider(Provider):
    name = "openai"
    api_key_env = "OPENAI_API_KEY"
    default_voices = {
        "main": "coral",
        "monologue": "ash",
        "notification": "ash",
    }

    def instructions_for(self, role: str) -> str:
        configured = self.voices_config.get(role)
        if isinstance(configured, dict):
            instr = configured.get("instructions")
            if isinstance(instr, str) and instr:
                return instr
        return DEFAULT_INSTRUCTIONS.get(role, "")

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
            clips.append(Clip(
                f"{preamble} ...",
                monologue_voice,
                instructions=self.instructions_for("monologue"),
            ))
        if want_main:
            clips.append(Clip(
                cap_length(summary),
                main_voice,
                instructions=self.instructions_for("main"),
            ))
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
        return Clip(
            line,
            self.voice_for("notification"),
            instructions=self.instructions_for("notification"),
        )

    def synthesise(self, clip: Clip) -> bytes | None:
        sample_rate = self.settings.get("sample_rate", DEFAULT_SAMPLE_RATE)
        bit_rate = self.settings.get("bit_rate", DEFAULT_BIT_RATE)
        model = self.settings.get("model", DEFAULT_MODEL)

        body = {
            "model": model,
            "voice": clip.voice,
            "input": clip.text,
            "response_format": "wav",
        }
        if clip.instructions:
            body["instructions"] = clip.instructions
        payload = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(
            OPENAI_TTS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                wav_bytes = response.read()
        except urllib.error.HTTPError as exc:
            log(f"<openai http error> {exc.code} {exc.read().decode('utf-8', 'replace')}")
            return None
        except Exception as exc:
            log(f"<openai error> {exc!r}")
            return None
        if not wav_bytes:
            return None

        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-y",
                    "-f", "wav", "-i", "pipe:0",
                    "-ar", str(sample_rate),
                    "-ac", "1",
                    "-b:a", str(bit_rate),
                    "-f", "mp3", "pipe:1",
                ],
                input=wav_bytes,
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError:
            log("<openai ffmpeg missing> ffmpeg not on PATH; install ffmpeg or replace the gap mp3s")
            return None
        except Exception as exc:
            log(f"<openai ffmpeg exception> {exc!r}")
            return None

        if result.returncode != 0:
            log(
                f"<openai ffmpeg error> rc={result.returncode} "
                f"stderr={result.stderr.decode('utf-8', 'replace')[:300]}"
            )
            return None
        mp3_bytes = result.stdout or None
        log(
            f"<openai synth> voice={clip.voice} model={model} "
            f"text_words={len(clip.text.split())} text_chars={len(clip.text)} "
            f"audio_bytes={len(mp3_bytes) if mp3_bytes else 0}"
        )
        return mp3_bytes


PROVIDER = OpenAIProvider
