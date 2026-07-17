"""Project-level text helpers used by main.py and provider modules."""

import re

MAX_SPEAK_CHARS = 800
MAX_MONOLOGUE_CHARS = 200
SUMMARY_WORD_THRESHOLD = 60
SHORT_REPLY_WORD_THRESHOLD = 8


def first_line(text: str) -> str:
    """Keep only the first non-empty line of a should-be-one-line output.

    Some models (gpt-5.x, notably) occasionally answer the reply they were
    shown instead of writing the asked-for one-line quip — especially when
    the reply ends with a question. The babble is always multi-line and its
    first line is usually a usable quip, so keep that and drop the rest.
    """
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


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


def cap_length(text: str, limit: int = MAX_SPEAK_CHARS) -> str:
    """Last-resort safety net: if the summariser didn't trim enough, hard cap."""
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rsplit(" ", 1)[0]
    return f"{trimmed}…"
