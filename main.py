"""Claude Code hook: speaks Claude's final reply on Stop, a Marvin quip on Notification.

Reads stdin, picks the configured TTS provider from `providers/`, asks it for
clips, hands them to `audio.play_clips` to stitch and play. afplay is detached
so Claude Code's hook returns quickly.

Provider behaviour lives in providers/<name>.py. See providers/base.py for the
contract and providers/mistral.py / providers/xai.py for worked examples.
"""

import json
import os
import sys

import litellm

from audio import play_clips, play_fallback_sound
from config import ENV_FILE, classifier_model, load_config, load_env_file, tts_provider
from llm import LLM
from logging_util import log, trim_log
from providers import PROVIDERS
from providers.base import Clip

# Anthropic rejects system-only message lists; this makes LiteLLM quietly add a
# placeholder user turn so the Marvin notification prompt works.
litellm.modify_params = True


def _build_provider(name: str, api_key: str | None):
    cls = PROVIDERS[name]
    config = load_config()
    return cls(
        llm=LLM(model=classifier_model()),
        api_key=api_key,
        settings=(config.get("provider_settings") or {}).get(name) or {},
        voices_config=(config.get("voices") or {}).get(name) or {},
    )


def _synth_fn(provider):
    """Adapter: play_clips' (text, voice, language, api_key) → provider.synthesise(Clip)."""
    def synth(text, voice, language, _api_key):
        return provider.synthesise(Clip(text, voice, language))
    return synth


def handle_stop(payload: dict, provider) -> None:
    from text_util import strip_markdown

    raw_text = (payload.get("last_assistant_message") or "").strip()
    text = strip_markdown(raw_text)
    if not text:
        return

    clips = provider.plan_stop_clips(text)
    play_clips(
        [(c.text, c.voice, c.language) for c in clips],
        provider.api_key,
        _synth_fn(provider),
    )


def handle_notification(provider) -> None:
    clip = provider.plan_notification_clip()
    if not clip:
        log("<fallback> notification line generation failed; playing system sound")
        play_fallback_sound()
        return
    play_clips(
        [(clip.text, clip.voice, clip.language)],
        provider.api_key,
        _synth_fn(provider),
    )


def main() -> None:
    load_env_file(ENV_FILE)
    trim_log()
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log(f"<invalid JSON>\n{raw}")
        return

    log(payload)

    name = tts_provider()
    cls = PROVIDERS.get(name)
    if cls is None:
        log(f"<unknown provider> {name!r}; available: {sorted(PROVIDERS)}")
        play_fallback_sound()
        return

    api_key = os.environ.get(cls.api_key_env) if cls.api_key_env else None
    if cls.api_key_env and not api_key:
        log(f"<{name}> {cls.api_key_env} not set; skipping TTS")
        return

    provider = _build_provider(name, api_key)

    event = payload.get("hook_event_name")
    if event == "Stop":
        handle_stop(payload, provider)
    elif event == "Notification":
        handle_notification(provider)
    else:
        log(f"<unhandled event> {event}")


if __name__ == "__main__":
    main()
