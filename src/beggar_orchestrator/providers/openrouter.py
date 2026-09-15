from __future__ import annotations

from .base import LLMProvider


class OpenRouterProvider(LLMProvider):
    default_model = "dots-studio/dots-3-note-preview:free"

    def __init__(
        self, *, name: str = "openrouter", token: str | None, model: str | None = None
    ):
        super().__init__(
            name=name,
            token=token,
            base_url="https://openrouter.ai/api/v1",
            model=model or self.default_model,
            headers={"X-OpenRouter-Title": "beggar-orchestrator"},
        )
