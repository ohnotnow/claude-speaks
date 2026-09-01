"""Kokoro TTS provider — local synthesis, no API key. Two engines:

- cli (default): shells out to https://github.com/nazdridoy/kokoro-tts,
  a fresh subprocess per clip, each paying the ~7s CPU-heavy onnxruntime
  model load.
- mlx (provider_settings.kokoro.engine: "mlx"): in-process mlx-audio on
  the Apple-Silicon GPU. Same voice pack, ~0.5s model load (fetched from
  HuggingFace into ~/.cache/huggingface on first use), and about a tenth
  of the CPU burn: measured 86 CPU-seconds (cli) vs 7.5 (mlx) for a 90s
  clip on an M1. One model per process, clips generated under a lock;
  generation runs at roughly 6x real time.

Voice ids are literal (`bm_george`, `bf_emma`) and shared by both engines;
blends like "af_sarah:60,am_adam:40" are cli-only. No prosody tags —
the summariser only runs on long replies, like Mistral's. Its summary
prompt is two-mode (routine replies compress hard, technically substantial
ones get room), so the main clip is capped by
provider_settings.kokoro.max_speak_chars (default 3000 chars) rather than
the global 800.

Kokoro's mp3s are 24000 Hz mono, not the 22050 Hz the stock gap files use,
so `gap_variant = "24k"` makes audio.gap_blob stitch with gaps/*_24k.mp3.
The mp3s also carry a Xing header, so afinfo reports a stitched file's
duration as just the first clip — playback is unaffected.
"""

import io
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from history import load_notification_history
from logging_util import PROJECT_DIR, log
from prompts import safe_format
from text_util import MAX_MONOLOGUE_CHARS, SHORT_REPLY_WORD_THRESHOLD, SUMMARY_WORD_THRESHOLD, cap_length, first_line

LONG_LENGTH_GUIDANCE = (
    "Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, "
    "grammatical phrase. Never stop mid-sentence to meet a word count: a finished "
    "thought matters more than brevity."
)
SHORT_LENGTH_GUIDANCE = (
    "The reply you are reacting to is very short, so keep this VERY brief too — "
    "1 to 3 words is ideal, like a single weary mutter (\"Whatever\", \"Finally\", "
    "\"Done — thank god\"). Do not pad it out into a full sentence."
)

from .base import Clip, Provider

DEFAULT_CLI = "kokoro-tts"
DEFAULT_MODEL_PATH = "kokoro-v1.0.onnx"
DEFAULT_VOICES_PATH = "voices-v1.0.bin"
DEFAULT_LANGUAGE = "en-gb"
DEFAULT_SPEED = 1.0
DEFAULT_TIMEOUT = 120
# Cap for the spoken summary clip. Local synthesis costs nothing, so this is
# roomier than the global MAX_SPEAK_CHARS; it must stay above the summary
# prompt's 400-word ceiling or long summaries get truncated mid-sentence.
DEFAULT_MAX_SPEAK_CHARS = 3000

DEFAULT_ENGINE = "cli"
MLX_DEFAULT_MODEL = "mlx-community/Kokoro-82M-bf16"
# Kokoro pipeline language codes, keyed by the CLI-style tags used in config.
# Anything unmapped falls back to British English; non-English codes need the
# matching misaki extra installed (e.g. misaki[ja] for Japanese).
MLX_LANG_CODES = {
    "en-gb": "b",
    "en-us": "a",
    "es": "e",
    "fr": "f",
    "fr-fr": "f",
    "hi": "h",
    "it": "i",
    "ja": "j",
    "pt-br": "p",
    "zh": "z",
}

# One model per process, shared across clips. Generation is serialised
# because mlx-audio's pipeline is not thread-safe and play_clips
# synthesises in parallel threads; at ~6x real time, sequential is cheap.
_MLX_LOCK = threading.Lock()
_MLX_MODEL = None


class KokoroProvider(Provider):
    name = "kokoro"
    api_key_env = None
    default_voices = {
        "main": "bm_george",
        "monologue": "bf_emma",
        "notification": "bf_emma",
    }
    gap_variant = "24k"

    def reformat_text(self, text: str) -> tuple[str, str | None]:
        if len(text.split()) <= SUMMARY_WORD_THRESHOLD:
            return text, None
        try:
            system_prompt = self.prompt("summary")
            persona = self.persona("main")
            if persona:
                system_prompt += (
                    f"\n\nThe reply you are about to compress is written in the voice of: {persona}. "
                    "Preserve a beat that captures that voice."
                )
            rewritten = self.llm.complete(system_prompt, text, max_tokens=16000, temperature=0.3)
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
            guidance = SHORT_LENGTH_GUIDANCE if len(text.split()) <= SHORT_REPLY_WORD_THRESHOLD else LONG_LENGTH_GUIDANCE
            system_prompt = safe_format(
                self.prompt("preamble"),
                length_guidance=guidance,
                persona=self.persona("monologue") or "",
            )
            raw = self.llm.complete(system_prompt, text, max_tokens=4000, temperature=1.0)
            line = first_line(raw)
            if line != raw.strip():
                log(f"<preamble guard> multi-line output; kept first line ({len(line)} of {len(raw.strip())} chars)")
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

        failed = [name for name in (preamble_err, summary_err) if name]
        if failed and want_main:
            summary = f"Heads up — the {', '.join(failed)} call fell over. Raw reply coming up. " + summary

        clips: list[Clip] = []
        if want_monologue and preamble:
            # Single trailing ellipsis gives the TTS a natural pause before the reply.
            clips.append(Clip(cap_length(f"{preamble} ...", MAX_MONOLOGUE_CHARS), monologue_voice, self.language_for("monologue")))
        if want_main:
            limit = self.settings.get("max_speak_chars", DEFAULT_MAX_SPEAK_CHARS)
            clips.append(Clip(cap_length(summary, limit), main_voice, self.language_for("main")))
        return clips

    def plan_notification_clip(self) -> Clip | None:
        history = load_notification_history()
        history_block = "\n".join(f"- {line}" for line in history) if history else "(no recent history)"
        prompt = safe_format(
            self.prompt("notification"),
            history=history_block,
            persona=self.persona("notification") or "",
        )
        try:
            line = self.llm.complete(prompt, "", max_tokens=4000, temperature=1.0)
            line = line.strip('"').strip("'").strip()
        except Exception as exc:
            log(f"<notification gen error> {exc!r}")
            return None
        if not line:
            return None
        log(f"<notification> {line}")
        return Clip(cap_length(line), self.voice_for("notification"), self.language_for("notification"))

    def synthesise(self, clip: Clip) -> bytes | None:
        engine = self.settings.get("engine", DEFAULT_ENGINE)
        if engine == "mlx":
            return self._synthesise_mlx(clip)
        if engine != DEFAULT_ENGINE:
            log(f"<kokoro> unknown engine {engine!r}; using cli")
        return self._synthesise_cli(clip)

    def _clip_language(self, clip: Clip) -> str:
        # The base class defaults language to "en"; kokoro wants a concrete
        # dialect, so bare "en" becomes the configurable default (en-gb).
        language = clip.language or "en"
        if language == "en":
            language = self.settings.get("language", DEFAULT_LANGUAGE)
        return language

    def _synthesise_mlx(self, clip: Clip) -> bytes | None:
        global _MLX_MODEL
        try:
            import numpy as np
            import soundfile as sf
            from mlx_audio.tts import load as load_mlx_model
        except Exception as exc:
            log(f"<kokoro mlx error> import failed: {exc!r} (deps missing? try `uv sync`)")
            return None

        language = self._clip_language(clip)
        lang_code = MLX_LANG_CODES.get(language.lower())
        if lang_code is None:
            log(f"<kokoro mlx> no lang code for {language!r}; using en-gb")
            lang_code = MLX_LANG_CODES[DEFAULT_LANGUAGE]
        model_id = self.settings.get("mlx_model", MLX_DEFAULT_MODEL)
        speed = self.settings.get("speed", DEFAULT_SPEED)

        try:
            with _MLX_LOCK:
                if _MLX_MODEL is None:
                    t0 = time.perf_counter()
                    _MLX_MODEL = load_mlx_model(model_id)
                    log(f"<kokoro mlx> loaded {model_id} in {time.perf_counter() - t0:.1f}s")
                t0 = time.perf_counter()
                chunks = []
                sample_rate = 24000
                for chunk in _MLX_MODEL.generate(
                    text=clip.text, voice=clip.voice, speed=float(speed), lang_code=lang_code
                ):
                    chunks.append(np.asarray(chunk.audio))
                    sample_rate = chunk.sample_rate
                gen_s = time.perf_counter() - t0
        except BaseException as exc:
            # BaseException, not Exception: misaki's G2P setup calls
            # spacy.cli.download when the en_core_web_sm model package is
            # missing, and that raises SystemExit(1) in a pip-less uv venv.
            log(f"<kokoro mlx error> {exc!r} voice={clip.voice}")
            return None

        if not chunks:
            log(f"<kokoro mlx error> no audio produced voice={clip.voice}")
            return None
        if sample_rate != 24000:
            # gap_variant pins stitching to the 24 kHz gap files; a mismatch
            # would silently truncate playback at the first gap.
            log(f"<kokoro mlx> unexpected sample rate {sample_rate}; gaps may truncate")

        audio = np.concatenate(chunks)
        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="MP3")
        result = buf.getvalue()
        log(
            f"<kokoro mlx synth> voice={clip.voice} lang={lang_code} "
            f"text_words={len(clip.text.split())} text_chars={len(clip.text)} "
            f"audio_s={len(audio) / sample_rate:.1f} gen_s={gen_s:.1f} audio_bytes={len(result)}"
        )
        return result

    def _synthesise_cli(self, clip: Clip) -> bytes | None:
        cli = self.settings.get("cli_path", DEFAULT_CLI)
        # Relative paths resolve against the project dir, not the hook's CWD
        # (which is wherever Claude Code was launched). Absolute paths pass through.
        model = str(PROJECT_DIR / self.settings.get("model_path", DEFAULT_MODEL_PATH))
        voices = str(PROJECT_DIR / self.settings.get("voices_path", DEFAULT_VOICES_PATH))
        speed = self.settings.get("speed", DEFAULT_SPEED)
        timeout = self.settings.get("timeout", DEFAULT_TIMEOUT)
        language = self._clip_language(clip)

        with tempfile.TemporaryDirectory(prefix="claude-speaks-kokoro-") as tmp:
            text_path = Path(tmp) / "clip.txt"
            audio_path = Path(tmp) / "clip.mp3"
            text_path.write_text(clip.text + "\n", encoding="utf-8")
            argv = [
                cli, str(text_path), str(audio_path),
                "--format", "mp3",
                "--voice", clip.voice,
                "--lang", language,
                "--speed", str(speed),
                "--model", model,
                "--voices", voices,
            ]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            except FileNotFoundError:
                log(f"<kokoro error> CLI not found: {cli!r} — is kokoro-tts installed and on PATH?")
                return None
            except subprocess.TimeoutExpired:
                log(f"<kokoro error> synthesis timed out after {timeout}s voice={clip.voice}")
                return None
            except Exception as exc:
                log(f"<kokoro error> {exc!r}")
                return None

            if proc.returncode != 0:
                log(f"<kokoro synth error> exit={proc.returncode} stderr={proc.stderr.strip()[-500:]}")
                return None
            if not audio_path.is_file() or audio_path.stat().st_size == 0:
                log(f"<kokoro synth error> exit=0 but no audio written stderr={proc.stderr.strip()[-500:]}")
                return None

            result = audio_path.read_bytes()
            log(
                f"<kokoro synth> voice={clip.voice} lang={language} "
                f"text_words={len(clip.text.split())} text_chars={len(clip.text)} "
                f"audio_bytes={len(result)}"
            )
            return result


PROVIDER = KokoroProvider
