from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from beggar_orchestrator.providers import (
    PROVIDER_TYPES,
    AgyProvider,
    CohereProvider,
    GroqProvider,
    OpenRouterProvider,
    ProviderError,
)
from beggar_orchestrator.providers.base import LLMProvider, _safe_log_text


class ProviderTests(unittest.TestCase):
    def test_only_verified_provider_types_are_registered(self):
        self.assertEqual(set(PROVIDER_TYPES), {"agy", "groq", "openrouter", "cohere"})

    def test_provider_defaults(self):
        self.assertEqual(GroqProvider(token=None).model, "openai/gpt-oss-120b")
        self.assertEqual(
            OpenRouterProvider(token=None).model,
            "openrouter/free",
        )
        self.assertEqual(CohereProvider(token=None).model, "command-a-plus-05-2026")
        self.assertEqual(AgyProvider().model, "gemini-3.8-flash-low")

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

    def test_agy_translates_system_and_schema(self):
        provider = AgyProvider(executable="/custom/agy")
        system, prompt = "system rules", "question"
        rendered = provider._prompt(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        command = provider._command(
            model=provider.model,
            response_schema=schema,
            timeout=20,
        )

        self.assertIn("<system_instruction>\nsystem rules", rendered)
        self.assertIn("<user>\nquestion", rendered)
        self.assertIn("--json-schema", command)
        self.assertIn("stream-json", command)
        self.assertNotIn("--system", command)

    def test_http_error_details_keep_diagnostics_and_redact_secrets(self):
        details = LLMProvider._error_details(
            {
                "error": {
                    "code": 429,
                    "message": "daily quota exhausted",
                    "metadata": {
                        "limit_source": "free_tier_daily",
                        "provider_name": "upstream-model-host",
                        "remedy_hint": "wait for reset",
                    },
                }
            }
        )

        self.assertEqual(details["provider_code"], "429")
        self.assertEqual(details["limit_source"], "free_tier_daily")
        self.assertEqual(details["upstream_provider"], "upstream-model-host")
        self.assertEqual(details["remedy_hint"], "wait for reset")

        fake_secret = "sk-" + ("x" * 16)
        rendered = _safe_log_text(f"Bearer {fake_secret} api_key={fake_secret}")
        self.assertNotIn(fake_secret, rendered)
        self.assertIn("[REDACTED]", rendered)


class AgyProviderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_agy_stream_normalizes_ndjson_and_structured_output(self):
        script_source = """#!/usr/bin/env python3
import json
import sys

request = json.loads(sys.stdin.readline())
assert request["event"] == "user"
assert "<system_instruction>" in request["message"]["content"]
print(json.dumps({"event": "init", "init": {"model": "fake-model"}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"text_delta": "{\\\"ok\\\":"}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"text_delta": "true}"}}), flush=True)
print(json.dumps({"event": "result", "result": {
    "conversation_id": "conversation-1",
    "status": "SUCCESS",
    "response": "{\\\"ok\\\":true}",
    "structured_output": {"ok": True},
    "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}
}}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake-agy"
            executable.write_text(script_source, encoding="utf-8")
            os.chmod(executable, 0o700)
            provider = AgyProvider(
                name="local-agy",
                model="fake-model",
                executable=str(executable),
            )
            events = []
            with self.assertLogs(
                "beggar_orchestrator.providers.agy", level="INFO"
            ) as captured:
                async for event in provider.stream(
                    messages=[
                        {"role": "system", "content": "system rules"},
                        {"role": "user", "content": "question"},
                    ],
                    response_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                ):
                    events.append(event)

        response = events[-1].response
        self.assertIsNotNone(response)
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(response.request_id, "conversation-1")
        self.assertEqual(response.usage.total_tokens, 14)
        self.assertEqual("".join(event.content for event in events), '{"ok":true}')
        logs = "\n".join(captured.output)
        self.assertIn("provider_request_completed", logs)
        self.assertIn("request_id=conversation-1", logs)
        self.assertNotIn("system rules", logs)
        self.assertNotIn("question", logs)

    async def test_agy_failure_logs_safe_cli_diagnostic(self):
        script_source = """#!/usr/bin/env python3
import sys

sys.stdin.readline()
print("unknown model; authorization=not-a-real-token", file=sys.stderr)
raise SystemExit(2)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake-agy"
            executable.write_text(script_source, encoding="utf-8")
            os.chmod(executable, 0o700)
            provider = AgyProvider(executable=str(executable))

            with self.assertLogs(
                "beggar_orchestrator.providers.agy", level="WARNING"
            ) as captured:
                with self.assertRaises(ProviderError):
                    async for _ in provider.stream(
                        messages=[{"role": "user", "content": "private question"}]
                    ):
                        pass

        logs = "\n".join(captured.output)
        self.assertIn("returncode=2", logs)
        self.assertIn("unknown model", logs)
        self.assertIn("authorization=[REDACTED]", logs)
        self.assertNotIn("not-a-real-token", logs)
        self.assertNotIn("private question", logs)


if __name__ == "__main__":
    unittest.main()
