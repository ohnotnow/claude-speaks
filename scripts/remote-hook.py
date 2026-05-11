#!/usr/bin/env python3
"""Claude Code hook for a headless box (e.g. a Raspberry Pi).

Reads the hook payload from stdin and POSTs it to a claude-speaks server
running on another machine that has speakers attached (see README →
"Remote mode"). Stdlib-only, so the Pi doesn't need uv or any dependencies.

Env vars:
    CLAUDE_SPEAKS_URL        Server endpoint. Default http://localhost:8765/hook
    CLAUDE_SPEAKS_TOKEN      Shared secret. Required — script no-ops if unset.
    CLAUDE_SPEAKS_OVERRIDES  Optional JSON object merged into the payload as
                             the ``claude_speaks`` overrides block. Lets this
                             client have its own voice / features without
                             touching config.json on the Mac. Example:
                                 export CLAUDE_SPEAKS_OVERRIDES='{
                                   "voices": {"mistral": {"main": "fr_marie"}}
                                 }'

Wire it up in ~/.claude/settings.json on the Pi as the hook command, e.g.
    "command": "/usr/bin/env python3 /path/to/claude-speaks/scripts/remote-hook.py"
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:8765/hook"
POST_TIMEOUT_SECONDS = 5


def _warn(message: str) -> None:
    # Stdout belongs to Claude Code; chat to stderr instead.
    print(f"remote-hook: {message}", file=sys.stderr)


def _load_overrides() -> dict | None:
    raw = os.environ.get("CLAUDE_SPEAKS_OVERRIDES")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"CLAUDE_SPEAKS_OVERRIDES is not valid JSON ({exc}); ignoring")
        return None
    if not isinstance(parsed, dict):
        _warn("CLAUDE_SPEAKS_OVERRIDES must be a JSON object; ignoring")
        return None
    return parsed


def main() -> int:
    token = os.environ.get("CLAUDE_SPEAKS_TOKEN")
    if not token:
        _warn("CLAUDE_SPEAKS_TOKEN not set; not forwarding")
        return 0

    url = os.environ.get("CLAUDE_SPEAKS_URL") or DEFAULT_URL

    raw = sys.stdin.read()
    if not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"stdin is not valid JSON ({exc}); not forwarding")
        return 0
    if not isinstance(payload, dict):
        _warn("stdin payload must be a JSON object; not forwarding")
        return 0

    overrides = _load_overrides()
    if overrides is not None:
        # Don't clobber: if the hook already set claude_speaks, our env-var
        # overrides layer on top of it.
        existing = payload.get("claude_speaks")
        if isinstance(existing, dict):
            payload["claude_speaks"] = {**existing, **overrides}
        else:
            payload["claude_speaks"] = overrides

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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _warn(f"POST to {url} failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
