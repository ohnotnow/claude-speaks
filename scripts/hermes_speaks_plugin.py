"""Hermes (Nous Research) plugin: forward each turn's final reply to claude-speaks.

Registers a `transform_llm_output` callback that POSTs the assistant's response
to the claude-speaks server on your Mac (see README → "Remote mode"). The
response is never modified — we always return None so Hermes delivers the
original text unchanged.

Drop this file wherever Hermes loads plugins from, and set two env vars where
Hermes can see them:

    CLAUDE_SPEAKS_URL=http://your-mac.local:8765/hook
    CLAUDE_SPEAKS_TOKEN=same-string-as-on-the-mac

If either is unset, the plugin silently no-ops — handy if you sometimes run
Hermes on a box that isn't on the same network as the Mac.

The plugin also sends a per-request config-override block so Hermes doesn't
get confused with Claude Code in your ear. By default it disables the
monologue and notification stages (Hermes isn't Claude — Marvin's preamble
would be a category error). Set CLAUDE_SPEAKS_OVERRIDES to a JSON object to
replace the default — e.g. to give Hermes its own voice:

    CLAUDE_SPEAKS_OVERRIDES='{
      "features": {"monologue": false, "notification": false},
      "voices": {"mistral": {"main": "fr_marie"}}
    }'
"""

import json
import os
import threading
import urllib.error
import urllib.request

POST_TIMEOUT_SECONDS = 5

# Sensible default: Hermes' replies stand on their own — no Marvin sigh, no
# idle nag. Voice stays as whatever the Mac's config.json says, so the user
# only has to override it if they specifically want to.
DEFAULT_OVERRIDES = {
    "features": {"monologue": False, "notification": False},
}


def _post(payload: dict, url: str, token: str) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # The hook docs already say exceptions become warnings rather than
        # breaking agent execution, but we'd rather not even cause a warning
        # when the speaker-machine is just offline.
        pass


def _resolve_overrides() -> dict:
    raw = os.environ.get("CLAUDE_SPEAKS_OVERRIDES")
    if not raw:
        return DEFAULT_OVERRIDES
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_OVERRIDES
    if not isinstance(parsed, dict):
        return DEFAULT_OVERRIDES
    return parsed


def claude_speaks(response_text, **kwargs):
    if not response_text or not response_text.strip():
        return None

    url = os.environ.get("CLAUDE_SPEAKS_URL")
    token = os.environ.get("CLAUDE_SPEAKS_TOKEN")
    if not url or not token:
        return None

    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": response_text,
        "claude_speaks": _resolve_overrides(),
        # Optional metadata the Mac side currently ignores but might find useful later.
        "source": "hermes",
        "session_id": kwargs.get("session_id") or "",
        "model": kwargs.get("model") or "",
        "platform": kwargs.get("platform") or "",
    }

    # Fire-and-forget: don't hold up Hermes' response delivery on the round-trip.
    threading.Thread(target=_post, args=(payload, url, token), daemon=True).start()

    return None  # leave Hermes' text untouched


def register(ctx):
    ctx.register_hook("transform_llm_output", claude_speaks)
