from __future__ import annotations

from .base import LLMProvider


class CohereProvider(LLMProvider):
    default_model = "command-a-plus-05-2026"

    def __init__(self, *, name: str = "cohere", token: str | None, model: str | None = None):
        super().__init__(
            name=name,
            token=token,
            base_url="https://api.cohere.ai/compatibility/v1",
            model=model or self.default_model,
            schema_format="cohere_json_object",
        )
