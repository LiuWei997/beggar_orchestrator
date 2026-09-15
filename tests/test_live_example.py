"""A real, directly executable LLM streaming example.

Run it manually with:

    .venv/bin/python tests/test_live_example.py

This file deliberately has no pytest test function, so a normal ``pytest`` run does
not call an external API or consume provider quota.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


def _restart_with_project_python() -> None:
    """Use the project environment when an IDE launches this with system Python."""
    if VENV_PYTHON.is_file() and Path(sys.executable) != VENV_PYTHON:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve())])


if __name__ == "__main__":
    _restart_with_project_python()


from beggar_orchestrator import Agent, Message, StreamEventType


async def main() -> None:
    """Ask one real question through the configured routing and retry policy."""
    agent = Agent(
        config_path=PROJECT_ROOT / "providers.toml",
        system=(
            "You are a senior DevOps engineer. Answer in Traditional Chinese, "
            "be concise, and provide actionable steps."
        ),
    )

    question = "Kubernetes Pod 一直出現 CrashLoopBackOff，應該先檢查哪些項目？"
    print(f"Initial lifecycle: {agent.status.value}")
    print(f"Question: {question}")
    print("Answer: ", end="", flush=True)

    final_response = None
    async with agent:
        async for event in agent.stream(
            route="general-chat",
            messages=[Message.user(question)],
            temperature=0.2,
            max_output_tokens=800,
            request_id="manual-live-example",
        ):
            if event.type is StreamEventType.CONTENT_DELTA:
                print(event.content, end="", flush=True)
            elif event.type is StreamEventType.COMPLETED:
                final_response = event.response

    if final_response is None or not final_response.content:
        raise RuntimeError("The LLM stream ended without a response")

    print("\n")
    print(f"Final lifecycle: {agent.status.value}")
    print(
        f"provider={final_response.provider} model={final_response.model} "
        f"latency_ms={final_response.latency_ms} "
        f"tokens={final_response.usage.total_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
