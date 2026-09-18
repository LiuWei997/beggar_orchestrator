from __future__ import annotations

import asyncio
import unittest

from beggar_orchestrator import (
    Agent,
    AgentAlreadyStartedError as RunAlreadyStartedError,
    AgentClearedError as RunClearedError,
    AgentError,
    AgentStatus as RunStatus,
    InvalidAgentTransitionError as InvalidRunTransitionError,
    Message,
)
from beggar_orchestrator.runtime import Route, RouteTarget
from beggar_orchestrator.providers import (
    AuthenticationError,
    ProviderError,
    Response,
    StreamEvent,
    StreamEventType,
    Usage,
)


class FakeProvider:
    def __init__(self, name: str, outcomes):
        self.name = name
        self.model = "test-model"
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_kwargs = None

    async def stream(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield StreamEvent(StreamEventType.CONNECTED, self.name, self.model)
        yield StreamEvent(StreamEventType.CONTENT_DELTA, self.name, self.model, content=outcome)
        yield StreamEvent(
            StreamEventType.COMPLETED,
            self.name,
            self.model,
            response=Response(
                outcome,
                self.name,
                self.model,
                "upstream-1",
                1,
                Usage(2, 3, 5),
                "stop",
            ),
        )


class SlowProvider:
    name = "slow"
    model = "test-model"

    async def stream(self, **kwargs):
        yield StreamEvent(StreamEventType.CONNECTED, self.name, self.model)
        for part in ("partial ", "output ", "that should not complete"):
            yield StreamEvent(
                StreamEventType.CONTENT_DELTA,
                self.name,
                self.model,
                content=part,
            )
            await asyncio.sleep(1)


def make_agent(providers, targets, *, on_event=None) -> Agent:
    return Agent._for_testing(
        system="system rules",
        providers=providers,
        routes={"chat": Route(targets)},
        on_event=on_event,
    )


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_accepts_controlled_roles_and_injects_system(self):
        provider = FakeProvider("primary", ["ok"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        result = await agent.start(
            route="chat",
            provider="primary",
            messages=[
                Message.user("question"),
                Message.assistant("working"),
                Message.tool('{"ok":true}', tool_call_id="call-1"),
            ],
        )

        self.assertEqual(result.content, "ok")
        self.assertEqual(
            provider.last_kwargs["messages"][0],
            {"role": "system", "content": "system rules"},
        )

    async def test_logs_attempt_and_state_without_message_content(self):
        provider = FakeProvider("primary", ["ok"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        with self.assertLogs("beggar_orchestrator.execution", level="INFO") as captured:
            await agent.start(
                route="chat",
                provider="primary",
                messages=[Message.user("private transcript fragment")],
                request_id="job-123",
            )

        logs = "\n".join(captured.output)
        self.assertIn("agent_attempt_started agent_id=job-123", logs)
        self.assertIn("agent_attempt_succeeded agent_id=job-123", logs)
        self.assertIn("agent_state_changed agent_id=job-123", logs)
        self.assertNotIn("private transcript fragment", logs)

    async def test_failure_does_not_fall_back(self):
        first = FakeProvider("first", [ProviderError("busy", retryable=True)])
        second = FakeProvider("second", ["ok"])
        agent = make_agent(
            {"first": first, "second": second},
            [RouteTarget("first"), RouteTarget("second")],
        )

        with self.assertRaises(ProviderError):
            await agent.start(
                route="chat", provider="first", messages=[Message.user("hello")]
            )

        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    async def test_authentication_failure_does_not_fall_back(self):
        first = FakeProvider("first", [AuthenticationError()])
        second = FakeProvider("second", ["ok"])
        agent = make_agent(
            {"first": first, "second": second},
            [RouteTarget("first"), RouteTarget("second")],
        )

        with self.assertRaises(AuthenticationError):
            await agent.start(
                route="chat", provider="first", messages=[Message.user("hello")]
            )
        self.assertEqual(second.calls, 0)

    async def test_stream_exposes_connection_delta_and_completion(self):
        provider = FakeProvider("primary", ["hello"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        events = [
            event
            async for event in agent.stream(
                route="chat", provider="primary", messages=[Message.user("hello")]
            )
        ]

        self.assertEqual(events[0].type, StreamEventType.CONNECTED)
        self.assertEqual(events[1].content, "hello")
        self.assertEqual(events[-1].response.content, "hello")

    async def test_agent_is_one_shot_and_exposes_lifecycle(self):
        provider = FakeProvider("primary", ["hello"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        self.assertEqual(agent.status, RunStatus.RUN_CREATED)
        response = await agent.start(
            route="chat", provider="primary", messages=[Message.user("hello")]
        )

        self.assertEqual(response.content, "hello")
        self.assertEqual(agent.status, RunStatus.RUN_COMPLETED)
        self.assertEqual(agent.snapshot().content, "hello")
        self.assertEqual(
            [transition.current for transition in agent.snapshot().transitions],
            [
                RunStatus.RUN_CREATED,
                RunStatus.ATTEMPT_STARTED,
                RunStatus.CONNECTED,
                RunStatus.RUN_COMPLETED,
            ],
        )
        with self.assertRaises(RunAlreadyStartedError):
            await agent.start(
                route="chat", provider="primary", messages=[Message.user("again")]
            )
        with self.assertRaises(InvalidRunTransitionError):
            await agent._execution.transition_to(RunStatus.ATTEMPT_STARTED)

    async def test_lifecycle_changes_are_emitted(self):
        provider = FakeProvider("primary", ["hello"])
        lifecycle_events = []

        async def capture(event):
            lifecycle_events.append(event["event"])

        agent = make_agent(
            {"primary": provider}, [RouteTarget("primary")], on_event=capture
        )
        await agent.start(
            route="chat", provider="primary", messages=[Message.user("hello")]
        )

        self.assertEqual(
            lifecycle_events,
            ["attempt_started", "connected", "run_completed"],
        )

    async def test_selected_provider_failure_becomes_out_of_usage(self):
        first = FakeProvider("first", [ProviderError("busy", retryable=True)])
        second = FakeProvider("second", [AuthenticationError()])
        agent = make_agent(
            {"first": first, "second": second},
            [RouteTarget("first"), RouteTarget("second")],
        )

        with self.assertRaises(AuthenticationError):
            await agent.start(
                route="chat", provider="second", messages=[Message.user("hello")]
            )

        self.assertEqual(agent.status, RunStatus.OUT_OF_USAGE)
        self.assertEqual(first.calls, 0)
        self.assertEqual(second.calls, 1)

    async def test_provider_not_allowed_by_route_fails_immediately(self):
        primary = FakeProvider("primary", ["unused"])
        agent = make_agent({"primary": primary}, [RouteTarget("other")])

        with self.assertRaisesRegex(AgentError, "not allowed"):
            await agent.start(
                route="chat", provider="primary", messages=[Message.user("hello")]
            )

        self.assertEqual(primary.calls, 0)
        self.assertEqual(agent.status, RunStatus.OUT_OF_USAGE)

    async def test_missing_provider_fails_immediately(self):
        agent = make_agent(
            {"primary": FakeProvider("primary", ["unused"])},
            [RouteTarget("missing")],
        )

        with self.assertRaisesRegex(AgentError, "not configured or is disabled"):
            await agent.start(
                route="chat", provider="missing", messages=[Message.user("hello")]
            )

        self.assertEqual(agent.status, RunStatus.OUT_OF_USAGE)

    async def test_cancel_before_start_and_clear(self):
        provider = FakeProvider("primary", ["unused"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        await agent.cancel("not needed")
        self.assertEqual(agent.status, RunStatus.RUN_CANCELLED)
        self.assertEqual(agent.snapshot().cancel_reason, "not needed")

        await agent.clear()
        self.assertTrue(agent.is_cleared)
        with self.assertRaises(RunClearedError):
            agent.snapshot()

    async def test_no_providers_is_init_failed(self):
        agent = make_agent({}, [RouteTarget("missing")])

        self.assertEqual(agent.status, RunStatus.INIT_FAILED)
        self.assertTrue(agent.is_terminal)
        with self.assertRaises(AgentError):
            await agent.start(
                route="chat", provider="missing", messages=[Message.user("hello")]
            )

    async def test_missing_config_is_init_failed_instead_of_constructor_error(self):
        agent = Agent(system="system rules", config_path="does-not-exist.toml")

        self.assertEqual(agent.status, RunStatus.INIT_FAILED)
        self.assertTrue(agent.snapshot().errors)

    async def test_running_stream_can_be_cancelled_and_keeps_partial_output(self):
        agent = make_agent({"slow": SlowProvider()}, [RouteTarget("slow")])
        received_output = asyncio.Event()

        async def consume():
            async for event in agent.stream(
                route="chat", provider="slow", messages=[Message.user("hello")]
            ):
                if event.type is StreamEventType.CONTENT_DELTA:
                    received_output.set()

        stream_task = asyncio.create_task(consume())
        await received_output.wait()
        await agent.cancel("monitor stopped the agent")
        await stream_task

        snapshot = agent.snapshot()
        self.assertEqual(snapshot.status, RunStatus.RUN_CANCELLED)
        self.assertEqual(snapshot.content, "partial ")

    async def test_async_context_completes_normally(self):
        provider = FakeProvider("primary", ["hello"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        async with agent as active_agent:
            self.assertIs(active_agent, agent)
            events = [
                event
                async for event in agent.stream(
                    route="chat", provider="primary", messages=[Message.user("hello")]
                )
            ]

        self.assertEqual(events[-1].type, StreamEventType.COMPLETED)
        self.assertEqual(agent.status, RunStatus.RUN_COMPLETED)

    async def test_breaking_context_stream_cancels_and_keeps_partial_output(self):
        agent = make_agent({"slow": SlowProvider()}, [RouteTarget("slow")])

        async with agent:
            async for event in agent.stream(
                route="chat", provider="slow", messages=[Message.user("hello")]
            ):
                if event.type is StreamEventType.CONTENT_DELTA:
                    break

        self.assertEqual(agent.status, RunStatus.RUN_CANCELLED)
        self.assertEqual(agent.snapshot().content, "partial ")

    async def test_context_exception_cancels_and_propagates(self):
        agent = make_agent({"slow": SlowProvider()}, [RouteTarget("slow")])

        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            async with agent:
                async for event in agent.stream(
                    route="chat", provider="slow", messages=[Message.user("hello")]
                ):
                    if event.type is StreamEventType.CONTENT_DELTA:
                        raise RuntimeError("consumer failed")

        self.assertEqual(agent.status, RunStatus.RUN_CANCELLED)

    async def test_agent_cannot_enter_context_twice(self):
        provider = FakeProvider("primary", ["hello"])
        agent = make_agent({"primary": provider}, [RouteTarget("primary")])

        async with agent:
            events = [
                event
                async for event in agent.stream(
                    route="chat", provider="primary", messages=[Message.user("hello")]
                )
            ]
        self.assertTrue(events)

        with self.assertRaises(RunAlreadyStartedError):
            async with agent:
                pass


if __name__ == "__main__":
    unittest.main()
