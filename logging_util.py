"""Shared logging used by main.py, config.py, audio.py, and provider modules."""

import json
import os
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "stop-hook.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_BYTES = 500_000


def log(entry: object) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    separator = "=" * 72
    body = json.dumps(entry, indent=2) if isinstance(entry, dict) else str(entry)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n{separator}\n{timestamp}\n{separator}\n{body}\n")


def trim_log() -> None:
    """If the log has grown past LOG_MAX_BYTES, keep only the most recent tail."""
    try:
        if not LOG_FILE.is_file() or LOG_FILE.stat().st_size <= LOG_MAX_BYTES:
            return
        with LOG_FILE.open("rb") as f:
            f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
        _, newline, rest = tail.partition(b"\n")
        kept = rest if newline else tail
        LOG_FILE.write_bytes(b"<log truncated>\n" + kept)
    except Exception as exc:
        try:
            log(f"<trim log error> {exc!r}")
        except Exception:
            pass
