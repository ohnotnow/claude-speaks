"""Run the audio player while holding an exclusive cross-process playback lock.

Spawned detached by audio.play_audio_file so concurrent hook invocations
(a second Claude session, a sub-agent finishing mid-speech) queue their
audio instead of talking over each other. The flock is kernel-owned and
tied to the open file descriptor: however this process or the player
dies (pkill, crash, kill -9), the lock is released at exit, so there is
no stale-lock state to clean up.

Usage: play_locked.py <lock-file> <player-argv...>

Stdlib plus logging_util only. It runs as a bare script, not part of the
uv project, so it must stay importable under any python.
"""

import fcntl
import subprocess
import sys

from logging_util import log

# A wedged player holding the lock would silence every later clip on the
# machine, so cap playback. Generous: Kokoro main clips can run ~4 minutes.
PLAYER_TIMEOUT_S = 600


def main() -> int:
    if len(sys.argv) < 3:
        log(f"<play locked> bad argv {sys.argv[1:]!r}; expected <lock-file> <player-argv...>")
        return 2
    lock_path, *player_argv = sys.argv[1:]
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            subprocess.run(player_argv, timeout=PLAYER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log(f"<play locked> player exceeded {PLAYER_TIMEOUT_S}s and was killed; argv={player_argv}")
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # the hook has already returned; the log is the only surface left
        log(f"<play locked error> {exc!r} argv={sys.argv[1:]}")
        sys.exit(1)
