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
        max_tokens: int = 400,
        temperature: float = 0.3,
    ) -> str:
        messages = [{"role": "system", "content": system}]
        if user:
            messages.append({"role": "user", "content": user})
        response = litellm.completion(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (response.choices[0].message.content or "").strip()
