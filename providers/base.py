"""Provider contract + Clip dataclass.

Each TTS backend lives in providers/<name>.py and subclasses Provider. See
providers/README.md for the full guide.
"""

from dataclasses import dataclass


@dataclass
class Clip:
    text: str
    voice: str
    language: str = "en"


class Provider:
    name: str = ""
    api_key_env: str | None = None
    default_voices: dict[str, str] = {}

    def __init__(self, llm, api_key=None, settings=None, voices_config=None, features=None):
        self.llm = llm
        self.api_key = api_key
        self.settings = settings or {}
        self.voices_config = voices_config or {}
        self.features = features if features is not None else {"monologue": True, "main": True, "notification": True}

    def voice_for(self, role: str, *, style: str | None = None) -> str:
        configured = self.voices_config.get(role)
        if isinstance(configured, dict):
            configured = configured.get("voice")
        if isinstance(configured, str) and configured:
            return configured
        if role in self.default_voices:
            return self.default_voices[role]
        if role == "notification" and "monologue" in self.default_voices:
            return self.default_voices["monologue"]
        if role == "monologue" and "main" in self.default_voices:
            return self.default_voices["main"]
        return self.default_voices.get("main", "")

    def language_for(self, role: str) -> str:
        configured = self.voices_config.get(role)
        if isinstance(configured, dict):
            lang = configured.get("language")
            if isinstance(lang, str) and lang:
                return lang
        return "en"

    def plan_stop_clips(self, text: str) -> list[Clip]:
        raise NotImplementedError

    def plan_notification_clip(self) -> Clip | None:
        raise NotImplementedError

    def synthesise(self, clip: Clip) -> bytes | None:
        raise NotImplementedError
