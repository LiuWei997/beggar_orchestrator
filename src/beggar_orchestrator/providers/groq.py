from __future__ import annotations

from .base import LLMProvider


class GroqProvider(LLMProvider):
    default_model = "openai/gpt-oss-120b"

    def __init__(self, *, name: str = "groq", token: str | None, model: str | None = None):
        super().__init__(
            name=name,
            token=token,
            base_url="https://api.groq.com/openai/v1",
            model=model or self.default_model,
        )
