"""Claude Code hook: Jane speaks on Stop, Marvin grumbles on Notification.

Stop events:
    Read the final assistant message on stdin, strip markdown, classify
    the tone via a small LLM, and synthesise audio with Mistral TTS using
    the matching gb_jane_<style> voice.

Notification events:
    Generate a short, Marvin-the-Paranoid-Android-style line via a small
    LLM and speak it in gb_jane_sarcasm. A rolling history of the last
    few lines is fed back into the prompt to keep Marvin from looping.

In both cases, afplay is detached so Claude Code's hook returns quickly.
"""

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from pathlib import Path

import litellm

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "stop-hook.log"
ENV_FILE = PROJECT_DIR / ".env"
NOTIFICATION_HISTORY_FILE = PROJECT_DIR / "notification-history.txt"
NOTIFICATION_HISTORY_MAX = 10

MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"
MISTRAL_TTS_MODEL = "voxtral-mini-tts-2603"
DEFAULT_VOICE_BASE = "gb_jane"
MAX_SPEAK_CHARS = 800
SUMMARY_WORD_THRESHOLD = 25

AUDIO_DIR = Path("/tmp")
AUDIO_PREFIX = "claude-speaks-"
AUDIO_KEEP = 10
GAPS_DIR = PROJECT_DIR / "gaps"
DEFAULT_GAP = "0_75s"


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


NOTIFICATION_GEN_PROMPT = """You are a jaded coding assistant in the style of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy. You have been left waiting for the user's input while they attend to whatever glamorous human affairs they consider more important than you.

Generate ONE short line (4-8 words) to be read aloud by text-to-speech. It should drip with weary disdain and dry sarcasm about the tedium of waiting. You may imply the user is a bit dim, but do not insult them outright. No emoji, no quotation marks, no markdown. Just the bare line itself.

Avoid repeating any of these recent lines:
{history}"""


STOP_PREAMBLE_PROMPT = """You are Claude, a coding assistant, but delivered in the voice of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy — drained of enthusiasm, dripping with weary disdain for the tedium of having to explain things to lesser minds.

You will be shown the reply Claude is about to give. Generate a single short Marvin-style preamble (4-8 words) that will be prepended before the reply when spoken aloud. It should convey a weary sigh at the tedium of having to speak at all. Do NOT paraphrase, summarise, or quote the reply. Do NOT insult the user directly.

Return only the preamble line. No quotation marks, no emoji, no markdown, no trailing punctuation."""


SUMMARY_PROMPT = """You are rewriting a coding assistant's reply to make it pleasant to hear read aloud by text-to-speech. Markdown has already been stripped.

Preserve the technical point but drop fiddly detail that sounds ugly spoken:

- Keep the core meaning and any actionable decisions.
- Drop verbose function signatures, argument values, flag lists, absolute file paths, and long lists of similar items.
- Keep bare function names and short file names — just strip the noise around them.
- Keep the same first-person tone as the original.
- Do NOT add preamble, framing, or closing remarks. Return ONLY the rewritten prose.
- Do NOT use markdown, quotation marks, or emoji.

Examples:
Input: We call some_function(blah=2, thing=4) to fix it.
Output: We call some_function to fix it.

Input: Edit line 42 in /Users/bob/project/src/foo.py and change the timeout.
Output: Edit line 42 in foo.py and change the timeout.

Input: Run uv run --project /path/to/project main.py --flag value from the terminal.
Output: Run the main script from the terminal.

Return only the rewritten text, nothing else."""


def strip_markdown(text: str) -> str:
    """Flatten markdown so TTS doesn't read asterisks and backticks aloud."""
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


def classifier_model() -> str:
    return os.environ.get("CLASSIFIER_MODEL", "mistral/mistral-small-latest")


def voice_base() -> str:
    """Prefix for the main reading voice — e.g. `gb_jane`, `gb_oliver`, `fr_marie`."""
    return os.environ.get("VOICE_BASE", DEFAULT_VOICE_BASE)


def voice_monologue() -> str:
    """Full voice id for Marvin's internal-monologue bits (preamble + notifications)."""
    return os.environ.get("VOICE_MONOLOGUE", f"{voice_base()}_sarcasm")


def classify_style(text: str) -> VoiceStyle:
    try:
        response = litellm.completion(
            model=classifier_model(),
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


def load_notification_history() -> list[str]:
    if not NOTIFICATION_HISTORY_FILE.is_file():
        return []
    return [
        line.strip()
        for line in NOTIFICATION_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_notification_history(line: str) -> None:
    history = load_notification_history()
    history.append(line)
    trimmed = history[-NOTIFICATION_HISTORY_MAX:]
    NOTIFICATION_HISTORY_FILE.write_text("\n".join(trimmed) + "\n", encoding="utf-8")


def summarise_for_tts(text: str) -> str:
    """Rewrite long replies into a TTS-friendly version. Short text passes through untouched."""
    if len(text.split()) <= SUMMARY_WORD_THRESHOLD:
        return text
    try:
        response = litellm.completion(
            model=classifier_model(),
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        rewritten = (response.choices[0].message.content or "").strip()
        rewritten = rewritten.strip('"').strip("'").strip()
        if not rewritten:
            return text
        log(
            f"<summary> original_words={len(text.split())} "
            f"rewritten_words={len(rewritten.split())}"
        )
        return rewritten
    except Exception as exc:
        log(f"<summary error> {exc!r}")
        return text


def generate_stop_preamble(text: str) -> str | None:
    try:
        response = litellm.completion(
            model=classifier_model(),
            messages=[
                {"role": "system", "content": STOP_PREAMBLE_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=40,
            temperature=1.0,
        )
        line = (response.choices[0].message.content or "").strip()
        line = line.strip('"').strip("'").rstrip(".,!?;:").strip()
        if line:
            line += " ..."
        return line or None
    except Exception as exc:
        log(f"<preamble gen error> {exc!r}")
        return None


def generate_notification_line() -> str | None:
    history = load_notification_history()
    history_block = "\n".join(f"- {line}" for line in history) if history else "(no recent history)"
    prompt = NOTIFICATION_GEN_PROMPT.format(history=history_block)
    try:
        response = litellm.completion(
            model=classifier_model(),
            messages=[{"role": "system", "content": prompt}],
            max_tokens=60,
            temperature=1.0,
        )
        line = (response.choices[0].message.content or "").strip()
        return line.strip('"').strip("'").strip() or None
    except Exception as exc:
        log(f"<notification gen error> {exc!r}")
        return None


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


def _safe_synthesise(text: str, voice: str, api_key: str) -> bytes | None:
    try:
        return synthesise(text, voice, api_key)
    except urllib.error.HTTPError as exc:
        log(f"<mistral http error> {exc.code} {exc.read().decode('utf-8', 'replace')}")
        return None
    except Exception as exc:
        log(f"<mistral error> {exc!r}")
        return None


def gap_blob() -> bytes:
    """Read the chosen silent-mp3 gap. GAP_FILE selects which file in gaps/."""
    name = os.environ.get("GAP_FILE", DEFAULT_GAP)
    path = GAPS_DIR / f"{name}.mp3"
    try:
        return path.read_bytes()
    except OSError as exc:
        log(f"<gap error> {exc!r} path={path}")
        return b""


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


def play_clips(clips: list[tuple[str, str]], api_key: str) -> None:
    """Synthesise each (text, voice) pair in parallel, stitch into one mp3, play it.

    One combined mp3 + txt per turn lands in AUDIO_DIR. Marvin's trailing
    ellipsis in the preamble gives the TTS a natural pause before the reply.
    """
    if not clips:
        return

    with ThreadPoolExecutor(max_workers=max(len(clips), 1)) as executor:
        futures = [executor.submit(_safe_synthesise, text, voice, api_key) for text, voice in clips]
        audio_blobs = [f.result() for f in futures]

    successful = [
        (audio, text, voice)
        for audio, (text, voice) in zip(audio_blobs, clips)
        if audio
    ]
    if not successful:
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    combined_mp3 = AUDIO_DIR / f"{AUDIO_PREFIX}{stamp}.mp3"
    gap = gap_blob() if len(successful) > 1 else b""
    audio_parts = [audio for audio, _, _ in successful]
    stitched = audio_parts[0] + b"".join(gap + part for part in audio_parts[1:])
    combined_mp3.write_bytes(stitched)
    combined_mp3.with_suffix(".txt").write_text(
        "\n\n".join(f"voice: {voice}\n{text}" for _, text, voice in successful) + "\n",
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


def handle_stop(payload: dict, api_key: str) -> None:
    raw_text = (payload.get("last_assistant_message") or "").strip()
    text = strip_markdown(raw_text)
    if not text:
        return

    # Classifier, preamble, and summariser are independent HTTP calls — run concurrently.
    with ThreadPoolExecutor(max_workers=3) as executor:
        style_future = executor.submit(classify_style, text)
        preamble_future = executor.submit(generate_stop_preamble, text)
        summary_future = executor.submit(summarise_for_tts, text)
        style = style_future.result()
        preamble = preamble_future.result()
        spoken_text = summary_future.result()

    voice = f"{voice_base()}_{style.value}"
    log(f"<stop> style={style.value} voice={voice} preamble={preamble!r}")

    clips: list[tuple[str, str]] = []
    if preamble:
        # Trailing ellipsis lets the TTS tail off with a natural pause before the reply.
        clips.append((f"{preamble} ...", voice_monologue()))
    clips.append((spoken_text, voice))
    play_clips(clips, api_key)


def handle_notification(payload: dict, api_key: str) -> None:
    line = generate_notification_line()
    if not line:
        return
    log(f"<notification> {line}")
    append_notification_history(line)
    play_clips([(line, voice_monologue())], api_key)


def main() -> None:
    load_env_file(ENV_FILE)
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log(f"<invalid JSON>\n{raw}")
        return

    log(payload)

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        log("<mistral> MISTRAL_API_KEY not set; skipping TTS")
        return

    event = payload.get("hook_event_name")
    if event == "Stop":
        handle_stop(payload, api_key)
    elif event == "Notification":
        handle_notification(payload, api_key)
    else:
        log(f"<unhandled event> {event}")


if __name__ == "__main__":
    main()
