"""Tiny wrapper around litellm so providers can call self.llm.complete(...).

One twist: when the model is an ``anthropic/...`` one and the dotfile has
``CLAUDE_CODE_OAUTH_TOKEN`` set (from ``claude setup-token``), calls route
through the Claude Agent SDK instead of litellm, so they bill the user's
Claude subscription rather than API credit. Claude Code's credential
precedence puts ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` *above*
the OAuth token, so having either set alongside it would silently bill the
API account — ``auth_conflict()`` detects that, and ``main.process_payload``
refuses to run the event when it does.
"""

import asyncio
import os
import time

import litellm

from logging_util import log

# Anthropic rejects system-only message lists; this makes LiteLLM quietly add a
# placeholder user turn.
litellm.modify_params = True

# Env vars that outrank CLAUDE_CODE_OAUTH_TOKEN in Claude Code's credential
# precedence (https://code.claude.com/docs/en/authentication).
_OUTRANKING_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def wants_agent_sdk(model: str) -> bool:
    return model.startswith("anthropic/") and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def auth_conflict(model: str) -> str | None:
    """The env var that would silently outbill the OAuth token, or None if safe."""
    if not wants_agent_sdk(model):
        return None
    for key in _OUTRANKING_KEYS:
        if os.environ.get(key):
            return key
    return None


class LLM:
    def __init__(self, model: str):
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        *,
        # Reasoning models (e.g. gpt-5.x) spend from this budget on hidden
        # reasoning before emitting any text — too small and the call errors
        # with "max_tokens ... reached". Spoken length is bounded by the
        # prompts and cap_length, not by this.
        max_tokens: int = 16000,
        temperature: float = 0.3,
    ) -> str:
        if wants_agent_sdk(self.model):
            return self._complete_agent_sdk(system, user)
        messages = [{"role": "system", "content": system}]
        if user:
            messages.append({"role": "user", "content": user})
        response = litellm.completion(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            # temperature=temperature, # commented out as some providers (and some specific models for a provider) don't support this
        )
        return (response.choices[0].message.content or "").strip()

    def _complete_agent_sdk(self, system: str, user: str) -> str:
        # Import here, not at module top — only subscription users pay the cost,
        # and the hook's startup latency matters for everyone else.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            system_prompt=system,
            model=self.model.removeprefix("anthropic/"),
            tools=[],
            max_turns=1,
            # [] means "load no filesystem settings". The default (None) loads
            # ~/.claude/settings.json — which contains the Stop hook that runs
            # this very code, so anything else recurses.
            setting_sources=[],
            # The SDK defaults to adaptive thinking at high effort, which had
            # Haiku spending 20-55s of hidden reasoning per 40-word quip.
            # These are quick formatting jobs; litellm sends no thinking either.
            thinking={"type": "disabled"},
        )

        started = time.monotonic()

        async def run() -> str:
            parts: list[str] = []
            # "." matches the placeholder litellm inserts for system-only calls.
            async for message in query(prompt=user or ".", options=options):
                if isinstance(message, AssistantMessage):
                    parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage):
                    # api_ms = time inside Anthropic's API; cli_ms = the CLI's
                    # whole query; wall_ms - cli_ms ≈ CLI boot overhead.
                    wall_ms = int((time.monotonic() - started) * 1000)
                    log(
                        f"<agent sdk> model={options.model} api_ms={message.duration_api_ms} "
                        f"cli_ms={message.duration_ms} wall_ms={wall_ms}"
                    )
            return "".join(parts)

        # Providers call complete() from ThreadPoolExecutor threads; asyncio.run
        # gives each thread its own short-lived event loop.
        return asyncio.run(run()).strip()
