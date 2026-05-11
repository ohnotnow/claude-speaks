"""HTTP server that accepts hook payloads from another machine and plays them locally.

Use case: Claude Code is running on a headless box (e.g. a Raspberry Pi) and
you want the audio to come out of your Mac. The Pi POSTs the hook JSON to
this server with a Bearer token; this Mac runs it through the normal pipeline.

Run with `uv run server.py`. Config:
  - host/port: config.json -> server.host / server.port (defaults 127.0.0.1:8765)
  - token:    env var CLAUDE_SPEAKS_TOKEN (load_env_file picks it up from .env)

The endpoint is POST /hook with the same JSON body the local Stop hook would
receive on stdin. Work happens in a background thread, so the client gets a
prompt 202 back without waiting for TTS to finish.
"""

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm

from audio import play_fallback_sound
from config import ENV_FILE, load_config, load_env_file
from logging_util import log, trim_log
from main import process_payload

# Match main.py: litellm needs a placeholder user turn for system-only prompts.
litellm.modify_params = True

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB — hook payloads are tiny; bigger is suspicious


def _server_config() -> tuple[str, int]:
    raw = load_config().get("server") or {}
    if not isinstance(raw, dict):
        log(f"<server config> expected object, got {type(raw).__name__}; using defaults")
        raw = {}
    host = raw.get("host") or DEFAULT_HOST
    port = raw.get("port") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        log(f"<server config> bad port {port!r}; using {DEFAULT_PORT}")
        port = DEFAULT_PORT
    return str(host), port


def _expected_token() -> str | None:
    import os

    token = os.environ.get("CLAUDE_SPEAKS_TOKEN")
    return token.strip() if token else None


class Handler(BaseHTTPRequestHandler):
    # Quiet the default per-request stderr line; we log what we care about ourselves.
    def log_message(self, format: str, *args) -> None:  # noqa: A002 — signature is fixed
        return

    def _send(self, status: int, body: str = "") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _check_auth(self) -> bool:
        expected = _expected_token()
        if not expected:
            log("<server auth> CLAUDE_SPEAKS_TOKEN not set; refusing request")
            self._send(503, "server token not configured\n")
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix) or not hmac.compare_digest(
            header[len(prefix) :].strip(), expected
        ):
            log(f"<server auth> rejected request from {self.client_address[0]}")
            self._send(401, "unauthorized\n")
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, "ok\n")
            return
        self._send(404, "not found\n")

    def do_POST(self) -> None:
        if self.path != "/hook":
            self._send(404, "not found\n")
            return
        if not self._check_auth():
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(400, "bad content-length\n")
            return
        if length <= 0:
            self._send(400, "empty body\n")
            return
        if length > MAX_BODY_BYTES:
            self._send(413, "payload too large\n")
            return

        try:
            raw = self.rfile.read(length)
        except Exception as exc:
            log(f"<server read error> {exc!r}")
            self._send(400, "could not read body\n")
            return

        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            log(f"<server bad payload> {exc!r}")
            self._send(400, "invalid json\n")
            return
        if not isinstance(payload, dict):
            self._send(400, "json object required\n")
            return

        # Fire-and-forget: don't make the Pi wait for TTS to finish.
        threading.Thread(target=_run_safely, args=(payload,), daemon=True).start()
        self._send(202, "accepted\n")


def _run_safely(payload: dict) -> None:
    try:
        process_payload(payload)
    except Exception as exc:
        log(f"<server worker error> {exc!r}")
        try:
            play_fallback_sound()
        except Exception:
            pass


def serve() -> None:
    load_env_file(ENV_FILE)
    trim_log()
    if not _expected_token():
        raise SystemExit(
            "CLAUDE_SPEAKS_TOKEN is not set. Add it to .env (see dotenv.example) "
            "before starting the server."
        )
    host, port = _server_config()
    log(f"<server start> listening on {host}:{port}")
    print(f"claude-speaks server listening on http://{host}:{port}  (POST /hook)")
    with ThreadingHTTPServer((host, port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log("<server stop> keyboard interrupt")
            print("\nstopping.")


if __name__ == "__main__":
    serve()
