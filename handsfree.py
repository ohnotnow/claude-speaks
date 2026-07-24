"""Hands-free arm path: after Marvin finishes speaking, hand the mic to the recorder.

Active only when ~/.claude-voice/handsfree exists (see claude-listens
bin/handsfree). Looks up the speaking session's channel server in the
registry, writes the one-shot reply target, and toggles the recorder via
config handsfree_arm_command (the ears daemon; declines to arm when
unconfigured). Every failure logs and returns - the spoken reply must never
be disrupted.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from config import handsfree_arm_command
from logging_util import log

BASE = Path.home() / ".claude-voice"
FLAG = BASE / "handsfree"
REGISTRY = BASE / "registry"
REPLY_TARGET = BASE / "reply-target.json"
PLAYBACK_TIMEOUT_S = 120
SETTLE_S = 0.3


def _ancestor_pids(limit: int = 10) -> set[int]:
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(limit):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pid = int(out.stdout.strip())
        except Exception:
            break
        if pid <= 1:
            break
        pids.add(pid)
    return pids


def _lookup_by_ancestry() -> dict | None:
    """Registry entry whose claude_pid is an ancestor of this hook process.

    Resumed sessions have a split identity: hook payloads (and hook/Bash child
    env) keep the ORIGINAL conversation id, but the restarted claude process
    spawns its MCP channel server with a FRESH CLAUDE_CODE_SESSION_ID, so the
    registry key no longer matches the payload id. The claude_pid recorded at
    registration bridges the two unambiguously - unlike cwd matching, which
    breaks with two sessions in one directory.
    """
    ancestors = _ancestor_pids()
    matches = []
    for path in REGISTRY.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if int(entry["claude_pid"]) in ancestors:
                matches.append(entry)
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log(f"<handsfree> {len(matches)} registry entries share our ancestry; not guessing")
    return None


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

    arm_cmd = handsfree_arm_command()
    if arm_cmd is None:
        log("<handsfree> handsfree_arm_command not configured; not arming")
        return

    entry_path = REGISTRY / f"{session_id}.json"
    try:
        entry = json.loads(entry_path.read_text())
    except FileNotFoundError:
        entry = _lookup_by_ancestry()
        if entry is None:
            log(f"<handsfree> no registry entry for {session_id}; not arming")
            return
        log(f"<handsfree> payload id {session_id} not registered; ancestry matched {entry['session_id']}")
    except Exception as exc:
        log(f"<handsfree> bad registry entry for {session_id}: {exc!r}; not arming")
        return
    try:
        port = int(entry["port"])
    except Exception as exc:
        log(f"<handsfree> registry entry missing port: {exc!r}; not arming")
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
            arm_cmd,
            check=True,
            capture_output=True,
            timeout=10,
        )
        log(f"<handsfree> mic armed for {session_id} -> port {port}")
    except Exception as exc:
        log(f"<handsfree> arming failed: {exc!r}")
        # Never leave a stale one-shot target behind a failed arm.
        REPLY_TARGET.unlink(missing_ok=True)
