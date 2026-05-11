"""Claude Code hook: speaks Claude's final reply on Stop, a Marvin quip on Notification.

Reads stdin, picks a TTS provider via auto-discovery, asks it for clips, hands
them to audio.play_clips. afplay is detached so the hook returns quickly. See
providers/README.md for the contract and how to add a new backend.
"""

import json
import os
import sys

import litellm

from audio import play_clips, play_fallback_sound
from config import (
    ENV_FILE,
    classifier_model,
    config_overlay,
    features,
    load_config,
    load_env_file,
    tts_provider,
)
from history import append_notification_history
from llm import LLM
from logging_util import log, trim_log
from providers import PROVIDERS
from text_util import strip_markdown

# Anthropic rejects system-only message lists; this makes LiteLLM quietly add a
# placeholder user turn so the Marvin notification prompt works.
litellm.modify_params = True


def handle_stop(payload: dict, provider) -> None:
    text = strip_markdown((payload.get("last_assistant_message") or "").strip())
    if not text:
        return
    play_clips(provider.plan_stop_clips(text), provider)


def handle_notification(provider) -> None:
    if not provider.features.get("notification", True):
        log("<notification disabled> skipping")
        return
    clip = provider.plan_notification_clip()
    if not clip:
        log("<fallback> notification line generation failed; playing system sound")
        play_fallback_sound()
        return
    append_notification_history(clip.text)
    play_clips([clip], provider)


def process_payload(payload: dict) -> None:
    """Dispatch a hook payload to the configured provider. Shared by main() and server.py.

    Honours a ``claude_speaks`` block in the payload as a per-request config
    overlay — see README → "Per-request overrides".
    """
    overrides = None
    if isinstance(payload, dict):
        candidate = payload.pop("claude_speaks", None)
        if isinstance(candidate, dict) and candidate:
            overrides = candidate
        elif candidate is not None:
            log(f"<bad overrides> expected object, got {type(candidate).__name__}; ignoring")

    log(payload)
    if overrides:
        log(f"<config overrides> {overrides}")

    with config_overlay(overrides):
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

        config = load_config()
        provider = cls(
            llm=LLM(model=classifier_model()),
            api_key=api_key,
            settings=(config.get("provider_settings") or {}).get(name) or {},
            voices_config=(config.get("voices") or {}).get(name) or {},
            features=features(),
        )

        event = payload.get("hook_event_name")
        if event == "Stop":
            handle_stop(payload, provider)
        elif event == "Notification":
            handle_notification(provider)
        else:
            log(f"<unhandled event> {event}")


def main() -> None:
    load_env_file(ENV_FILE)
    trim_log()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        log(f"<bad payload> {exc!r}")
        return

    process_payload(payload)


if __name__ == "__main__":
    main()
