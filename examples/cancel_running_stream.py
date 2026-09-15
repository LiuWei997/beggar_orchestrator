"""Cancel a one-shot Agent after it has started producing SSE output.

Run from the project root:

    .venv/bin/python examples/cancel_running_stream.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from beggar_orchestrator import Agent, Message, StreamEventType


PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def print_stream(agent, enough_output: asyncio.Event) -> None:
    received_characters = 0

    try:
        async for event in agent.stream(
            route="general-chat",
            messages=[Message.user("詳細說明 Kubernetes 高可用架構")],
            max_output_tokens=1_500,
        ):
            if event.type is StreamEventType.CONTENT_DELTA:
                print(event.content, end="", flush=True)
                received_characters += len(event.content)

                # Replace this condition with your own interruption policy.
                if received_characters >= 1:
                    enough_output.set()
                    await asyncio.sleep(0)  # Let the monitor task run immediately.
    except asyncio.CancelledError:
        # Expected when agent.cancel() interrupts this consumer task between chunks.
        pass


async def stop_agent(agent, enough_output: asyncio.Event) -> None:
    await enough_output.wait()
    await agent.cancel("Stopped after receiving partial output")


async def main() -> None:
    agent = Agent(
        config_path=PROJECT_ROOT / "providers.toml",
        system="You are a DevOps engineer. Give a detailed answer in Traditional Chinese.",
    )

    enough_output = asyncio.Event()
    stream_task = asyncio.create_task(print_stream(agent, enough_output))
    monitor_task = asyncio.create_task(stop_agent(agent, enough_output))
    await asyncio.gather(stream_task, monitor_task)

    # Cancellation does not erase data. Partial output remains readable.
    snapshot = agent.snapshot()
    print(f"\n\nstatus={snapshot.status.value}")
    print(f"partial_characters={len(snapshot.content)}")
    print(f"reason={snapshot.cancel_reason}")

    # Only clear after the caller no longer needs the partial output or history.
    await agent.clear()


if __name__ == "__main__":
    asyncio.run(main())
