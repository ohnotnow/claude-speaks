"""TTS provider auto-discovery.

Each module in this package that exposes a module-level `PROVIDER` attribute
(a `Provider` subclass) gets registered into `PROVIDERS` keyed by its `name`.
A broken provider file is logged and skipped — it must never kill the hook.
"""

import importlib
import pkgutil

from logging_util import log

from .base import Provider

PROVIDERS: dict[str, type[Provider]] = {}

_SKIP = {"base"}

for _mod_info in pkgutil.iter_modules(__path__):
    _name = _mod_info.name
    if _name in _SKIP or _name.startswith("_"):
        continue
    try:
        _mod = importlib.import_module(f".{_name}", __name__)
    except Exception as exc:
        log(f"<provider load error> {_name}: {exc!r}")
        continue
    _candidate = getattr(_mod, "PROVIDER", None)
    if isinstance(_candidate, type) and issubclass(_candidate, Provider) and _candidate.name:
        PROVIDERS[_candidate.name] = _candidate
