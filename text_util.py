"""Project-level text helpers used by main.py and provider modules."""

import re

MAX_SPEAK_CHARS = 800
SUMMARY_WORD_THRESHOLD = 60


def strip_markdown(text: str) -> str:
    """Flatten markdown so TTS doesn't read asterisks and backticks aloud."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = text.replace("*", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def cap_length(text: str) -> str:
    """Last-resort safety net: if the summariser didn't trim enough, hard cap."""
    if len(text) <= MAX_SPEAK_CHARS:
        return text
    trimmed = text[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0]
    return f"{trimmed}…"
