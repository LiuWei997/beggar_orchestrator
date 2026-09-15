from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from beggar_orchestrator.config import Settings, _build_runtime, load_config, read_token


class ConfigTests(unittest.TestCase):
    def test_environment_token_and_agent_build(self):
        settings = Settings(
            providers={
                "primary": {
                    "type": "groq",
                    "credential": "env:TEST_GROQ_KEY",
                    "model": "openai/gpt-oss-20b",
                }
            },
            routes={
                "chat": {
                    "targets": [{"provider": "primary", "timeout_seconds": 10}]
                }
            },
        )

        with patch.dict(os.environ, {"TEST_GROQ_KEY": "secret"}):
            runtime = _build_runtime(settings, system="system rules")

        self.assertEqual(runtime.providers["primary"].model, "openai/gpt-oss-20b")
        self.assertEqual(runtime.routes["chat"].targets[0].provider, "primary")

    def test_environment_token_reference(self):
        with patch.dict(os.environ, {"TEST_TOKEN": "secret"}):
            self.assertEqual(read_token("env:TEST_TOKEN"), "secret")

    def test_config_file_is_cached_after_first_read(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "providers.toml"
            config_path.write_text(
                """
[providers.groq]
type = "groq"

[routes.chat]
[[routes.chat.targets]]
provider = "groq"
""".strip(),
                encoding="utf-8",
            )

            first = load_config(config_path)
            config_path.unlink()
            second = load_config(config_path)

        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
