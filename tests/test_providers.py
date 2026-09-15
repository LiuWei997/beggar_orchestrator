from __future__ import annotations

import unittest

from beggar_orchestrator.providers import (
    PROVIDER_TYPES,
    CohereProvider,
    GroqProvider,
    OpenRouterProvider,
)


class ProviderTests(unittest.TestCase):
    def test_only_verified_provider_types_are_registered(self):
        self.assertEqual(set(PROVIDER_TYPES), {"groq", "openrouter", "cohere"})

    def test_provider_defaults(self):
        self.assertEqual(GroqProvider(token=None).model, "openai/gpt-oss-120b")
        self.assertEqual(
            OpenRouterProvider(token=None).model,
            "dots-studio/dots-3-note-preview:free",
        )
        self.assertEqual(CohereProvider(token=None).model, "command-a-plus-05-2026")

    def test_every_provider_request_uses_sse(self):
        provider = GroqProvider(token="secret")
        body = provider._body(
            messages=[{"role": "user", "content": "hello"}],
            model=provider.model,
            temperature=0,
            max_output_tokens=100,
            response_schema=None,
        )
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})


if __name__ == "__main__":
    unittest.main()
