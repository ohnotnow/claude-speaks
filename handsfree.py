"""Hands-free arm path: after Marvin finishes speaking, hand the mic to Handy.

Active only when ~/.claude-voice/handsfree exists (see claude-listens
bin/handsfree). Looks up the speaking session's channel server in the
registry, writes the one-shot reply target, and toggles Handy recording.
Every failure logs and returns - the spoken reply must never be disrupted.
"""

import json
import subprocess
import time
from pathlib import Path

from logging_util import log

BASE = Path.home() / ".claude-voice"
FLAG = BASE / "handsfree"
REGISTRY = BASE / "registry"
REPLY_TARGET = BASE / "reply-target.json"
# Verified invocation 2026-07-23 (ant note on ait handy-UkLWZ.1.3): forwards
# to the running Handy instance via single-instance arg passing; same command
# starts and stops (toggle semantics).
HANDY_BIN = "/Applications/Handy.app/Contents/MacOS/handy"
PLAYBACK_TIMEOUT_S = 120
SETTLE_S = 0.3


def maybe_arm(session_id: str | None, player_proc) -> None:
    """Arm the mic for `session_id` once playback ends.

    No-op unless the hands-free flag is set and a reply was actually played.
    `player_proc` is the Popen returned by audio.play_clips (None when
    nothing was played).
    """
    if not FLAG.exists():
        return
    if not session_id:
        log("<handsfree> flag on but payload has no session_id; not arming")
        return
    if player_proc is None:
        log("<handsfree> flag on but nothing was played; not arming")
        return

    entry_path = REGISTRY / f"{session_id}.json"
    try:
        entry = json.loads(entry_path.read_text())
        port = int(entry["port"])
    except FileNotFoundError:
        log(f"<handsfree> no registry entry for {session_id}; not arming")
        return
    except Exception as exc:
        log(f"<handsfree> bad registry entry for {session_id}: {exc!r}; not arming")
        return

    try:
        player_proc.wait(timeout=PLAYBACK_TIMEOUT_S)
    except Exception as exc:
        # A stuck player means audio may still be coming out of the speakers;
        # arming now would record Marvin. Skip this round.
        log(f"<handsfree> playback wait failed: {exc!r}; not arming")
        return
    time.sleep(SETTLE_S)

    try:
        REPLY_TARGET.write_text(
            json.dumps(
                {
                    "port": port,
                    "session_id": session_id,
                    "armed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            + "\n"
        )
        subprocess.run(
            [HANDY_BIN, "--toggle-transcription"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        log(f"<handsfree> mic armed for {session_id} -> port {port}")
    except Exception as exc:
        log(f"<handsfree> arming failed: {exc!r}")
        # Never leave a stale one-shot target behind a failed arm.
        REPLY_TARGET.unlink(missing_ok=True)
