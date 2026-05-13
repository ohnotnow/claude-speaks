"""Load provider prompts from disk.

Resolution order: ``prompts.local/<provider>/<name>.md`` (gitignored user
override) falls back to ``prompts/<provider>/<name>.md`` (shipped default).
Either path missing is fine; both missing means the install is broken.

A leading ``<!-- ... -->`` HTML-comment block is stripped at load time, so
shipped prompts can carry usage notes (placeholder reference, what the
prompt does) that survive a copy-paste into the user's override copy
without ending up read aloud by the LLM.
"""

import re

from logging_util import PROJECT_DIR, log

PROMPTS_DIR = PROJECT_DIR / "prompts"
PROMPTS_LOCAL_DIR = PROJECT_DIR / "prompts.local"
PERSONAS_DIR = PROJECT_DIR / "personas"
PERSONAS_LOCAL_DIR = PROJECT_DIR / "personas.local"

LEADING_COMMENT_RE = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)


def _strip_leading_comment(text: str) -> str:
    return LEADING_COMMENT_RE.sub("", text, count=1)


def load_prompt(provider: str, name: str) -> str:
    rel = f"{provider}/{name}.md"
    local = PROMPTS_LOCAL_DIR / rel
    shipped = PROMPTS_DIR / rel

    if local.is_file():
        log(f"<prompt> {rel} loaded from prompts.local/")
        return _strip_leading_comment(local.read_text(encoding="utf-8"))
    if shipped.is_file():
        log(f"<prompt> {rel} loaded from prompts/")
        return _strip_leading_comment(shipped.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"Prompt not found: tried {local} and {shipped}. "
        f"Install may be broken — check that prompts/{rel} exists."
    )


def load_persona(value: str) -> str:
    """Resolve a persona config value to its character description.

    If ``personas.local/<value>.md`` or ``personas/<value>.md`` exists, the
    file contents are returned (with any leading HTML-comment block
    stripped). Otherwise ``value`` is returned verbatim — so a user writing
    ``"personas": {"monologue": "a wildly excited pantomime dame"}`` in
    config.json gets that string slotted straight into the prompt.
    """
    rel = f"{value}.md"
    local = PERSONAS_LOCAL_DIR / rel
    shipped = PERSONAS_DIR / rel
    if local.is_file():
        log(f"<persona> {value} loaded from personas.local/")
        return _strip_leading_comment(local.read_text(encoding="utf-8")).strip()
    if shipped.is_file():
        log(f"<persona> {value} loaded from personas/")
        return _strip_leading_comment(shipped.read_text(encoding="utf-8")).strip()
    log(f"<persona> {value!r} treated as freeform description")
    return value


def safe_format(template: str, **kwargs) -> str:
    """``str.format`` that falls back to the raw template on a brace error.

    A user's custom prompt may contain stray ``{`` characters that aren't
    intended as placeholders. Returning the unformatted template keeps the
    hook running rather than crashing the whole event.
    """
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        log(f"<prompt format error> {exc!r}; using template unformatted")
        return template
