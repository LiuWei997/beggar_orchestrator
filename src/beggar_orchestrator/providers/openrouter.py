from __future__ import annotations

from .base import LLMProvider


class OpenRouterProvider(LLMProvider):
    # OpenRouter keeps this router pointed at its current pool of free models.
    # It also filters candidates by capabilities required by the request.
    default_model = "openrouter/free"

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
