"""Tiny wrapper around litellm so providers can call self.llm.complete(...)."""

import litellm

# Anthropic rejects system-only message lists; this makes LiteLLM quietly add a
# placeholder user turn.
litellm.modify_params = True


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
