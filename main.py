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

# Anthropic rejects system-only message lists; this makes LiteLLM quietly add a
# placeholder user turn so the Marvin notification prompt works.
litellm.modify_params = True

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "stop-hook.log"
ENV_FILE = PROJECT_DIR / ".env"
NOTIFICATION_HISTORY_FILE = PROJECT_DIR / "notification-history.txt"
NOTIFICATION_HISTORY_MAX = 10
WORD_REPLACEMENTS_FILE = PROJECT_DIR / "word_replacements.json"

MISTRAL_TTS_URL = "https://api.mistral.ai/v1/audio/speech"
MISTRAL_TTS_MODEL = "voxtral-mini-tts-2603"
DEFAULT_VOICE_BASE = "gb_jane"
MAX_SPEAK_CHARS = 800
SUMMARY_WORD_THRESHOLD = 60

AUDIO_DIR = Path("/tmp")
AUDIO_PREFIX = "claude-speaks-"
AUDIO_KEEP = 10
GAPS_DIR = PROJECT_DIR / "gaps"
DEFAULT_GAP = "0_75s"

FALLBACK_SOUND = Path("/System/Library/Sounds/Funk.aiff")


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

Generate ONE SHORT line to be read aloud by text-to-speech using a female French accent. It should drip with weary disdain and dry sarcasm about the tedium of waiting. You may imply the user is a bit dim, but do not insult them outright. No emoji, no quotation marks, no markdown. Just the bare line itself.

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical sentence or phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.  Sometimes just "Merde!" is funnier than "Oh, not another boring task - whatever"

Avoid repeating any of these recent lines or sentence structures:
{history}"""


STOP_PREAMBLE_PROMPT = """You are Claude, a coding assistant, but delivered in the voice of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy — drained of enthusiasm, dripping with weary disdain for the tedium of having to explain things to lesser minds.

You will be shown the reply Claude is about to give. Generate a single short Marvin-style preamble that will be prepended before the reply when spoken aloud. It should convey a weary sigh at the tedium of having to speak at all. Do NOT paraphrase, summarise, or quote the reply. Do NOT insult the user directly.

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.

Return only the preamble line. No quotation marks, no emoji, no markdown, no trailing punctuation."""


SUMMARY_PROMPT = """You are compressing a coding assistant's reply so it can be read aloud in under 30 seconds by a slow text-to-speech voice. Markdown has already been stripped.

HARD WORD BUDGET: aim for 50 words, never exceed 80. Even for a 400-word input. This is not a trim — it is aggressive compression. If the reply is long, most of it must go. That is the job.

Preserve one good voice beat. If the reply has personality — dry asides, jokes, turns of phrase — pick the single best one and keep it. Cut all the others. You cannot keep "the cadence" of a long reply in 50 words; pick the one line that would be missed and keep that.

- Keep the single most important point, decision, or result.
- Keep one memorable aside if there is one. Drop the rest.
- Drop file paths, line numbers, function signatures, argument values, flag lists.
- Drop tangents, context-setting, "the thing I keep thinking about" framings, and any second or third example.
- Merge bullets into flowing prose.
- Keep first-person tone.
- Do NOT add preamble, framing, or closing remarks. Return ONLY the rewritten prose.
- Do NOT use markdown, quotation marks, or emoji.
- Do NOT include meta-phrases like "summary" or "in short".

Examples:

Input: We call some_function(blah=2, thing=4) to fix it.
Output: We call some_function to fix it.

Input: Run uv run --project /path/to/project main.py --flag value from the terminal.
Output: Run the main script from the terminal.

Input: Ha! Don't feel too guilty — the summariser is only rewriting the spoken version. The full reply with all its file paths, line numbers, and parentheticals is still sitting right there in your terminal, which is where you'd actually want to read it from anyway. The TTS was always a "catch the gist while you're making coffee" thing, not a replacement for reading the real response. If you wanted a verbatim reading you'd use a screen reader, not a hook that takes creative liberties with your prose.
Output: Don't feel guilty — the full reply is still in your terminal, which is where you'd actually read it anyway. TTS was always a catch-the-gist-while-making-coffee thing, not a real replacement.

Input: Done. Three changes: bootstrap/app.php:18 — trustProxies(at: '') as string, not array. This is the actual root cause. app/Providers/AppServiceProvider.php — removed both band-aids (URL::forceScheme and the request()->server->set('HTTPS', 'on') hack) and the now-unused URL import. Previous layout / flux:error cleanups stay. Once this deploys, isSecure() will correctly return true in production and you can also drop the ASSET_URL env var; Laravel will figure out the scheme itself.
Output: Done, three changes. trustProxies now takes a string, not an array — that was the actual root cause. Removed both band-aids and the unused import. Once deployed, isSecure will return true in production and you can drop ASSET_URL too.

Input: Right — fingers crossed, Mimo's moment of truth. The thing I keep coming back to about this project is how much character it packs into roughly 480 lines of Python. The core idea is delightfully silly: Claude Code fires a Stop hook, and main.py reads the last assistant message off stdin, runs it through three parallel LLM calls (a tone classifier, a Marvin preamble generator, and a gentle summariser), then stitches two TTS clips — Marvin's weary sigh in one voice, followed by Jane reading the actual reply. The two clips are joined by a tiny silent mp3 so there's a natural beat between the sigh and the reply. A few bits I think are nicely judged. The word_replacements step is a pragmatic phonetic lookup so things like SQL or Livewire get pronounced properly. The notification path keeps a rolling history of the last ten Marvin quips to nudge against repetition. And rotate_audio_archive quietly keeps only the ten most recent mp3s, which future-you will appreciate.
Output: Fingers crossed for Mimo. What I love is how much character this packs into 480 lines — a Stop hook, three parallel calls for tone, Marvin's sigh and the summary, stitched with a silent mp3 for the beat. The phonetic lookup so SQL doesn't get read as squirrel is the bit that made me smile.

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
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def cap_length(text: str) -> str:
    """Last-resort safety net: if the summariser didn't trim enough, hard cap."""
    if len(text) <= MAX_SPEAK_CHARS:
        return text
    trimmed = text[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0]
    return f"{trimmed}…"


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
    return os.environ.get("LLM_MODEL", "mistral/mistral-small-latest")


def voice_base() -> str:
    """Prefix for the main reading voice — e.g. `gb_jane`, `gb_oliver`, `fr_marie`."""
    return os.environ.get("VOICE_BASE", DEFAULT_VOICE_BASE)


def voice_monologue() -> str:
    """Full voice id for Marvin's internal-monologue bits (preamble + notifications)."""
    return os.environ.get("VOICE_MONOLOGUE", f"{voice_base()}_sarcasm")


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


def classify_style(text: str) -> tuple[VoiceStyle, str | None]:
    try:
        response = litellm.completion(
            model=classifier_model(),
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=50,
            temperature=0,
        )
        style_str = _extract_style(response.choices[0].message.content or "")
        return VoiceStyle(style_str), None
    except Exception as exc:
        log(f"<classifier error> {exc!r}")
        return VoiceStyle.NEUTRAL, "classifier"


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


def summarise_for_tts(text: str) -> tuple[str, str | None]:
    """Rewrite long replies into a TTS-friendly version. Short text passes through untouched."""
    if len(text.split()) <= SUMMARY_WORD_THRESHOLD:
        return text, None
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
            return text, None
        log(
            f"<summary> original_words={len(text.split())} "
            f"rewritten_words={len(rewritten.split())}"
        )
        return rewritten, None
    except Exception as exc:
        log(f"<summary error> {exc!r}")
        return text, "summariser"


def generate_stop_preamble(text: str) -> tuple[str | None, str | None]:
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
        if not line:
            log("<preamble gen> model returned empty content")
            return None, None
        return f"{line} ...", None
    except Exception as exc:
        log(f"<preamble gen error> {exc!r}")
        return None, "preamble"


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


def load_word_replacements() -> dict[str, str]:
    """Load phonetic word replacements from JSON. Missing or malformed file → no replacements."""
    if not WORD_REPLACEMENTS_FILE.is_file():
        return {}
    try:
        data = json.loads(WORD_REPLACEMENTS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"<word replacements error> {exc!r}")
        return {}
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
    """Read the chosen silent-mp3 gap. GAP_FILE selects which file in gaps/."""
    name = os.environ.get("GAP_FILE", DEFAULT_GAP)
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


def play_clips(clips: list[tuple[str, str]], api_key: str) -> None:
    """Synthesise each (text, voice) pair in parallel, stitch into one mp3, play it.

    One combined mp3 + txt per turn lands in AUDIO_DIR. Marvin's trailing
    ellipsis in the preamble gives the TTS a natural pause before the reply.
    """
    if not clips:
        return

    replacements = load_word_replacements()
    if replacements:
        clips = [(apply_word_replacements(text, replacements), voice) for text, voice in clips]

    with ThreadPoolExecutor(max_workers=max(len(clips), 1)) as executor:
        futures = [executor.submit(_safe_synthesise, text, voice, api_key) for text, voice in clips]
        audio_blobs = [f.result() for f in futures]

    successful = [
        (audio, text, voice)
        for audio, (text, voice) in zip(audio_blobs, clips)
        if audio
    ]
    if not successful:
        log("<fallback> all TTS synthesis failed; playing system sound")
        play_fallback_sound()
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
        style, style_err = style_future.result()
        preamble, preamble_err = preamble_future.result()
        spoken_text, summary_err = summary_future.result()

    voice = f"{voice_base()}_{style.value}"
    log(
        f"<stop> style={style.value} voice={voice} "
        f"monologue_voice={voice_monologue()} preamble={preamble!r}"
    )

    failed = [name for name in (style_err, preamble_err, summary_err) if name]
    if failed:
        notice = f"Heads up — the {', '.join(failed)} call fell over. Raw reply coming up. "
        spoken_text = notice + spoken_text

    spoken_text = cap_length(spoken_text)

    clips: list[tuple[str, str]] = []
    if preamble:
        # Trailing ellipsis lets the TTS tail off with a natural pause before the reply.
        clips.append((f"{preamble} ...", voice_monologue()))
    clips.append((spoken_text, voice))
    play_clips(clips, api_key)


def handle_notification(payload: dict, api_key: str) -> None:
    line = generate_notification_line()
    if not line:
        log("<fallback> notification line generation failed; playing system sound")
        play_fallback_sound()
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
