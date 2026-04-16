"""Claude Stop hook: speaks the final assistant message via Mistral TTS.

Pipeline:
1. Read the Stop hook JSON payload on stdin; log it for inspection.
2. Ask a small LLM (via LiteLLM) to classify the message's tone into one
   of Jane's supported emotional styles.
3. Synthesise audio via Mistral /v1/audio/speech using the matching voice
   (e.g. en_uk_jane_curious) and play it with afplay.

Forks before the LLM + TTS work so Claude Code's hook returns immediately.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from enum import Enum
from pathlib import Path

import litellm

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "stop-hook.log"
ENV_FILE = PROJECT_DIR / ".env"

MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"
MISTRAL_TTS_MODEL = "voxtral-mini-tts-2603"
VOICE_BASE = "gb_jane"
MAX_SPEAK_CHARS = 800


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


def strip_markdown(text: str) -> str:
    """Flatten markdown so TTS doesn't read asterisks and backticks aloud.

    Code blocks are dropped entirely (saying 'def main colon' is not fun).
    Inline code, links, headers, and list markers are replaced with their
    content. Stray asterisks (bold/italic markers) are removed last.
    Finally a soft character cap stops runaway monologues.
    """
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = text.replace("*", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > MAX_SPEAK_CHARS:
        trimmed = text[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0]
        text = f"{trimmed}… (trimmed for audio)"
    return text


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(entry: object) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    separator = "=" * 72
    body = json.dumps(entry, indent=2) if isinstance(entry, dict) else str(entry)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n{separator}\n{timestamp}\n{separator}\n{body}\n")


def classify_style(text: str) -> VoiceStyle:
    model = os.environ.get("CLASSIFIER_MODEL", "mistral/mistral-small-latest")
    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            max_tokens=50,
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        style_str = (json.loads(content).get("style") or "neutral").strip().lower()
        return VoiceStyle(style_str)
    except Exception as exc:
        log(f"<classifier error> {exc!r}")
        return VoiceStyle.NEUTRAL


def synthesise(text: str, voice: str, api_key: str) -> bytes | None:
    payload = json.dumps({
        "input": text,
        "model": MISTRAL_TTS_MODEL,
        "response_format": "mp3",
        "voice_id": voice,
    }).encode("utf-8")

    request = urllib.request.Request(
        MISTRAL_TTS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    audio_b64 = body.get("audio_data")
    return base64.b64decode(audio_b64) if audio_b64 else None


def speak(text: str, api_key: str) -> None:
    style = classify_style(text)
    voice = f"{VOICE_BASE}_{style.value}"
    log(f"<classifier> style={style.value} voice={voice}")

    try:
        audio = synthesise(text, voice, api_key)
    except urllib.error.HTTPError as exc:
        log(f"<mistral http error> {exc.code} {exc.read().decode('utf-8', 'replace')}")
        return
    except Exception as exc:
        log(f"<mistral error> {exc!r}")
        return

    if not audio:
        log("<mistral> no audio_data in response")
        return

    filepath = Path(f"/tmp/claude-speaks-{datetime.now():%Y%m%d-%H%M%S}.mp3")
    filepath.write_bytes(audio)
    # Detach afplay so the hook returns without waiting for playback to finish.
    subprocess.Popen(
        ["afplay", str(filepath)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> None:
    load_env_file(ENV_FILE)
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log(f"<invalid JSON>\n{raw}")
        return

    log(payload)

    raw_text = (payload.get("last_assistant_message") or "").strip()
    text = strip_markdown(raw_text)
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not text:
        return
    if not api_key:
        log("<mistral> MISTRAL_API_KEY not set; skipping TTS")
        return

    speak(text, api_key)


if __name__ == "__main__":
    main()
