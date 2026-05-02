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

from history import load_notification_history
from logging_util import log
from text_util import cap_length

from .base import Clip, Provider

XAI_TTS_URL = "https://api.x.ai/v1/tts"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_BIT_RATE = 64000


XAI_TAG_BLOCK = """xAI prosody tags you MAY use (and ONLY these — do not invent others):

- <soft>quieter, intimate</soft>
- <whisper>conspiratorial</whisper>
- <loud>raised voice</loud>
- <emphasis>the important word</emphasis>
- <slow>weighty, deliberate</slow>
- <fast>urgent, rushed</fast>
- <higher-pitch>questioning, surprised</higher-pitch>
- <lower-pitch>grave, serious</lower-pitch>
- <build-intensity>escalating</build-intensity>
- <decrease-intensity>winding down</decrease-intensity>
- <laugh-speak>amused while talking</laugh-speak>
- <sing-song>playful</sing-song>

Use them sparingly. Most text stays untagged — wrap one or two spans where they genuinely aid delivery, no more. Tags must be balanced (open and close)."""


XAI_SUMMARY_PROMPT = f"""You are preparing a coding assistant's reply for text-to-speech playback. Markdown has already been stripped.

Two jobs, in order:

1. If the reply is longer than ~50 spoken words, compress aggressively. HARD WORD BUDGET: aim for 50, never exceed 80. Keep one good voice beat — the single most memorable line — and cut the rest. Drop file paths, line numbers, function signatures, flag lists, tangents, and any second or third example. Merge bullets into flowing prose. Keep first-person tone. If the reply is already short, leave the wording largely as-is.

2. Wrap one or two spans in xAI prosody tags where they meaningfully aid delivery — an aside in <soft>, a key conclusion in <emphasis>, a weary moment in <slow>. Do not over-tag.

{XAI_TAG_BLOCK}

- Do NOT add preamble, framing, or closing remarks. Return ONLY the rewritten prose with tags inline.
- Do NOT use markdown, quotation marks, or emoji.
- Do NOT include meta-phrases like "summary" or "in short".

Examples:

Input: We call some_function(blah=2, thing=4) to fix it.
Output: We call some_function to fix it.

Input: Done. Three changes: bootstrap/app.php:18 — trustProxies(at: '') as string, not array. This is the actual root cause. Removed both band-aids and the now-unused URL import. Once this deploys, isSecure() will correctly return true in production.
Output: Done, three changes. trustProxies now takes a string, not an array — <emphasis>that was the actual root cause</emphasis>. Removed both band-aids and the unused import. Once deployed, isSecure will return true in production.

Input: Right — fingers crossed, Mimo's moment of truth. The thing I keep coming back to about this project is how much character it packs into roughly 480 lines of Python.
Output: <slow>Fingers crossed for Mimo.</slow> What I love is how much character this packs into 480 lines.

Return only the rewritten text, nothing else."""


XAI_PREAMBLE_PROMPT = f"""You are Claude, a coding assistant, but delivered in the voice of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy — drained of enthusiasm, dripping with weary disdain for the tedium of having to explain things to lesser minds.

You will be shown the reply Claude is about to give. Generate a single short Marvin-style preamble that will be prepended before the reply when spoken aloud. It should convey a weary sigh at the tedium of having to speak at all. Do NOT paraphrase, summarise, or quote the reply. Do NOT insult the user directly.

You may wrap part of the line in <slow>...</slow> or <lower-pitch>...</lower-pitch> to lean into Marvin's drag, but only one tag — the line is already short. No other tags. Tags must be balanced.

{XAI_TAG_BLOCK}

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.

Return only the preamble line. No quotation marks, no emoji, no markdown, no trailing punctuation."""


NOTIFICATION_GEN_PROMPT = """You are a jaded coding assistant in the style of Marvin the Paranoid Android from The Hitchhiker's Guide to the Galaxy. You have been left waiting for the user's input while they attend to whatever glamorous human affairs they consider more important than you.

Generate ONE SHORT line to be read aloud by text-to-speech using a female voice. It should drip with weary disdain and dry sarcasm about the tedium of waiting. You may imply the user is a bit dim, but do not insult them outright. No emoji, no quotation marks, no markdown. Just the bare line itself.

Keep it brief — aim for roughly 6-12 words — but ALWAYS return a complete, grammatical sentence or phrase. Never stop mid-sentence to meet a word count: a finished thought matters more than brevity.  Sometimes just "Merde!" is funnier than "Oh, not another boring task - whatever"

Reply in {language}. If German or Japanese, write in the actual native script (e.g. こんにちは, バカ, müßig, schade) — do not romanise or translate. The TTS will read the characters directly.

Avoid repeating any of these recent lines or sentence structures:
{history}"""


NOTIFICATION_LANGUAGES = [
    ("English", 1),
    ("German (Deutsch)", 5),
    ("Japanese (日本語)", 5),
    ("Chinese (Simplified)", 5),
    ("Hindi", 5),
    ("Korean", 5),
    ("Vietnamese", 5),
]


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
            rewritten = self.llm.complete(XAI_SUMMARY_PROMPT, text, max_tokens=400, temperature=0.3)
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
            line = self.llm.complete(XAI_PREAMBLE_PROMPT, text, max_tokens=40, temperature=1.0)
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
        languages, weights = zip(*NOTIFICATION_LANGUAGES)
        language = random.choices(languages, weights=weights, k=1)[0]
        log(f"<notification language> {language}")
        prompt = NOTIFICATION_GEN_PROMPT.format(history=history_block, language=language)
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
