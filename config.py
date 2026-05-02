"""Config and dotenv loading. Read fresh on every call — the hook runs once per turn."""

import json
import os
from pathlib import Path

from logging_util import PROJECT_DIR, log

CONFIG_FILE = PROJECT_DIR / "config.json"
ENV_FILE = PROJECT_DIR / ".env"

DEFAULT_LLM_MODEL = "mistral/mistral-small-latest"
DEFAULT_TTS_PROVIDER = "mistral"
DEFAULT_FEATURES = {"monologue": True, "main": True, "notification": True}


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"<config error> {exc!r}")
        return {}
    if not isinstance(data, dict):
        log(f"<config> expected object, got {type(data).__name__}")
        return {}
    return data


def classifier_model() -> str:
    return load_config().get("llm_model") or DEFAULT_LLM_MODEL


def tts_provider() -> str:
    return (load_config().get("tts_provider") or DEFAULT_TTS_PROVIDER).lower()


def features() -> dict[str, bool]:
    raw = load_config().get("features") or {}
    if not isinstance(raw, dict):
        log(f"<features> expected object, got {type(raw).__name__}")
        raw = {}
    return {k: bool(raw.get(k, default)) for k, default in DEFAULT_FEATURES.items()}
