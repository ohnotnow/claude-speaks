"""Stitch synthesised mp3 clips together with a silent gap, archive, and play."""

import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from config import load_config
from logging_util import PROJECT_DIR, log
from providers.base import Clip, Provider

AUDIO_DIR = Path("/tmp")
AUDIO_PREFIX = "claude-speaks-"
AUDIO_KEEP = 10

GAPS_DIR = PROJECT_DIR / "gaps"
DEFAULT_GAP = "0_75s"

FALLBACK_SOUND = Path("/System/Library/Sounds/Funk.aiff")


def load_word_replacements() -> dict[str, str]:
    """Phonetic replacements pulled from config.json. Missing section → no replacements."""
    data = load_config().get("word_replacements") or {}
    if not isinstance(data, dict):
        log(f"<word replacements> expected object, got {type(data).__name__}")
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k).strip()}


def apply_word_replacements(text: str, replacements: dict[str, str]) -> str:
    """Swap technical/obscure words for phonetic versions so the TTS pronounces them right."""
    for word, replacement in replacements.items():
        pattern = r"\b" + re.escape(word) + r"\b"
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def gap_blob() -> bytes:
    """Read the chosen silent-mp3 gap. `gap_file` in config picks which one."""
    name = load_config().get("gap_file") or DEFAULT_GAP
    path = GAPS_DIR / f"{name}.mp3"
    try:
        return path.read_bytes()
    except OSError as exc:
        log(f"<gap error> {exc!r} path={path}")
        return b""


def play_fallback_sound() -> None:
    """Last-resort audible heads-up when every TTS path has fallen over."""
    if not FALLBACK_SOUND.is_file():
        log(f"<fallback sound missing> {FALLBACK_SOUND}")
        return
    try:
        subprocess.Popen(
            ["afplay", str(FALLBACK_SOUND)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log(f"<fallback sound error> {exc!r}")


def rotate_audio_archive() -> None:
    """Keep only the AUDIO_KEEP most recent mp3s (plus their .txt companions)."""
    try:
        mp3s = sorted(
            AUDIO_DIR.glob(f"{AUDIO_PREFIX}*.mp3"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in mp3s[AUDIO_KEEP:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".txt").unlink(missing_ok=True)
    except Exception as exc:
        log(f"<rotate error> {exc!r}")


def _safe_synthesise(provider: Provider, clip: Clip) -> bytes | None:
    """Call provider.synthesise but never propagate — providers should log their own errors."""
    try:
        return provider.synthesise(clip)
    except Exception as exc:
        log(f"<{provider.name} error> {exc!r}")
        return None


def play_clips(clips: list[Clip], provider: Provider) -> None:
    """Synthesise each clip in parallel, stitch with a silent gap, play via afplay."""
    if not clips:
        return

    replacements = load_word_replacements()
    if replacements:
        for clip in clips:
            clip.text = apply_word_replacements(clip.text, replacements)

    with ThreadPoolExecutor(max_workers=max(len(clips), 1)) as executor:
        futures = [executor.submit(_safe_synthesise, provider, clip) for clip in clips]
        audio_blobs = [f.result() for f in futures]

    successful = [(audio, clip) for audio, clip in zip(audio_blobs, clips) if audio]
    if not successful:
        log("<fallback> all TTS synthesis failed; playing system sound")
        play_fallback_sound()
        return
    if len(successful) < len(clips):
        log(f"<partial> {len(successful)}/{len(clips)} clips synthesised; playing what we have")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    combined_mp3 = AUDIO_DIR / f"{AUDIO_PREFIX}{stamp}.mp3"
    gap = gap_blob() if len(successful) > 1 else b""
    audio_parts = [audio for audio, _ in successful]
    stitched = audio_parts[0] + b"".join(gap + part for part in audio_parts[1:])
    combined_mp3.write_bytes(stitched)
    combined_mp3.with_suffix(".txt").write_text(
        "\n\n".join(f"voice: {clip.voice}\n{clip.text}" for _, clip in successful) + "\n",
        encoding="utf-8",
    )

    rotate_audio_archive()

    subprocess.Popen(
        ["sh", "-c", f"afplay {shlex.quote(str(combined_mp3))}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
