"""Rolling notification-history.txt — last few Marvin quips, fed back to suppress repetition."""

from logging_util import PROJECT_DIR

NOTIFICATION_HISTORY_FILE = PROJECT_DIR / "notification-history.txt"
NOTIFICATION_HISTORY_MAX = 10


def load_notification_history() -> list[str]:
    if not NOTIFICATION_HISTORY_FILE.is_file():
        return []
    return [
        line.strip()
        for line in NOTIFICATION_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_notification_history(line: str) -> None:
    history = load_notification_history()
    history.append(line)
    trimmed = history[-NOTIFICATION_HISTORY_MAX:]
    NOTIFICATION_HISTORY_FILE.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
