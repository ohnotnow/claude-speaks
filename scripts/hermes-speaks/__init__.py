"""Hermes (Nous Research) plugin: forward each turn's final reply to claude-speaks.

Registers a `post_llm_call` callback that POSTs the assistant's final response
to the claude-speaks server on your Mac (see README → "Remote mode"). This is
an observer hook: its return value is ignored, so Hermes delivers the original
text unchanged.

Drop this file wherever Hermes loads plugins from, and set two env vars where
Hermes can see them:

    HERMES_SPEAKS_URL=http://your-mac.local:8765/hook
    HERMES_SPEAKS_TOKEN=same-string-as-on-the-mac

For backwards compatibility, CLAUDE_SPEAKS_URL and CLAUDE_SPEAKS_TOKEN are
also accepted as fallbacks. If neither URL/token pair is set, the plugin
silently no-ops — handy if you sometimes run Hermes on a box that isn't on the
same network as the Mac.

The plugin also sends a per-request config-override block so Hermes doesn't
get confused with Claude Code in your ear. By default it disables the
monologue and notification stages (Hermes isn't Claude — Marvin's preamble
would be a category error). Set HERMES_SPEAKS_OVERRIDES to a JSON object to
replace the default — e.g. to give Hermes its own voice. For backwards
compatibility, CLAUDE_SPEAKS_OVERRIDES is also accepted as a fallback:

    HERMES_SPEAKS_OVERRIDES='{
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


def _get_env(primary: str, fallback: str) -> str | None:
    """Read the Hermes-specific env var, falling back to the legacy Claude one."""
    return os.environ.get(primary) or os.environ.get(fallback)


def _resolve_overrides() -> dict:
    raw = _get_env("HERMES_SPEAKS_OVERRIDES", "CLAUDE_SPEAKS_OVERRIDES")
    if not raw:
        return DEFAULT_OVERRIDES
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_OVERRIDES
    if not isinstance(parsed, dict):
        return DEFAULT_OVERRIDES
    return parsed


def claude_speaks(assistant_response=None, response_text=None, **kwargs):
    """Forward Hermes' final reply to claude-speaks.

    Hermes' current final-response hook is `post_llm_call`, which passes the
    text as `assistant_response`. Keep `response_text` as a compatibility alias
    in case this helper is called directly or by an older local experiment.
    """
    text = assistant_response if assistant_response is not None else response_text
    if not text or not str(text).strip():
        return None

    url = _get_env("HERMES_SPEAKS_URL", "CLAUDE_SPEAKS_URL")
    token = _get_env("HERMES_SPEAKS_TOKEN", "CLAUDE_SPEAKS_TOKEN")
    if not url or not token:
        return None

    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": str(text),
        "claude_speaks": _resolve_overrides(),
        # Optional metadata the Mac side currently ignores but might find useful later.
        "source": "hermes",
        "session_id": kwargs.get("session_id") or "",
        "model": kwargs.get("model") or "",
        "platform": kwargs.get("platform") or "",
    }

    # Fire-and-forget: don't hold up Hermes' response delivery on the round-trip.
    threading.Thread(target=_post, args=(payload, url, token), daemon=True).start()

    return None  # observer hook; leave Hermes' text untouched


def register(ctx):
    ctx.register_hook("post_llm_call", claude_speaks)

