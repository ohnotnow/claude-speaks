#!/usr/bin/env python3
"""Fire a test payload at a locally-running claude-speaks server.

Saves typing the curl incantation while debugging. Reads the bearer token
from the project .env (or $CLAUDE_SPEAKS_TOKEN if set), POSTs to /hook, and
prints the server's response.

Examples:
    uv run scripts/poke-server.py
    uv run scripts/poke-server.py -m "Hello there, can you hear me?"
    uv run scripts/poke-server.py --event Notification
    uv run scripts/poke-server.py --provider xai --no-monologue
    uv run scripts/poke-server.py --health
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://localhost:8765"
DEFAULT_MESSAGE = "Right then. If you can hear this, the test worked."
TIMEOUT_SECONDS = 5


def load_token() -> str | None:
    token = os.environ.get("CLAUDE_SPEAKS_TOKEN")
    if token:
        return token.strip()
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "CLAUDE_SPEAKS_TOKEN":
            return value.strip().strip('"').strip("'")
    return None


def build_payload(args: argparse.Namespace) -> dict:
    payload: dict = {
        "hook_event_name": args.event,
        "source": "poke-server",
        "session_id": "poke-server",
    }
    if args.event == "Stop":
        payload["last_assistant_message"] = args.message

    overrides: dict = {}
    if args.provider:
        overrides["tts_provider"] = args.provider

    features: dict = {}
    if args.no_monologue:
        features["monologue"] = False
    if args.no_main:
        features["main"] = False
    if args.no_notification:
        features["notification"] = False
    if features:
        overrides["features"] = features

    if overrides:
        payload["claude_speaks"] = overrides

    return payload


def post(url: str, token: str, payload: dict) -> tuple[int, str]:
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
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Server base URL (default {DEFAULT_URL})")
    parser.add_argument("-m", "--message", default=DEFAULT_MESSAGE, help="last_assistant_message body for Stop events")
    parser.add_argument("--event", default="Stop", choices=["Stop", "Notification"], help="hook_event_name to send")
    parser.add_argument("--provider", help="Override tts_provider (e.g. xai, mistral, openai, elevenlabs)")
    parser.add_argument("--no-monologue", action="store_true", help="Disable the monologue clip via overrides")
    parser.add_argument("--no-main", action="store_true", help="Disable the main clip via overrides")
    parser.add_argument("--no-notification", action="store_true", help="Disable notification clip via overrides")
    parser.add_argument("--health", action="store_true", help="Hit /health instead of /hook and exit")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    if args.health:
        status, body = get(f"{base}/health")
        print(f"GET /health -> {status}")
        sys.stdout.write(body)
        return 0 if status == 200 else 1

    token = load_token()
    if not token:
        print("CLAUDE_SPEAKS_TOKEN not found in env or .env", file=sys.stderr)
        return 1

    payload = build_payload(args)
    print("POST /hook payload:")
    print(json.dumps(payload, indent=2))
    status, body = post(f"{base}/hook", token, payload)
    print(f"\n-> HTTP {status}")
    sys.stdout.write(body)
    if not body.endswith("\n"):
        print()
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
