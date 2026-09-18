from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from .lifecycle import (
    AgentAlreadyStartedError,
    AgentClearedError,
    AgentError,
    AgentSnapshot,
    AgentStateMachine,
    AgentStatus,
    TERMINAL_AGENT_STATUSES,
)
from .messages import Message
from .providers import ProviderError, Response, StreamEvent, StreamEventType
from .runtime import ProviderRuntime


logger = logging.getLogger(__name__)


class AgentExecution:
    """Execute exactly one routed LLM request and retain its observable state."""

    def __init__(self, runtime: ProviderRuntime, *, agent_id: str | None = None):
        self._runtime: ProviderRuntime | None = runtime
        self.agent_id = agent_id or str(uuid.uuid4())
        self.route = ""
        self.requested_provider = ""
        self.temperature: float | None = None
        self.max_output_tokens: int | None = None
        self.response_schema: dict[str, Any] | None = None
        self._messages: list[Message | Mapping[str, Any]] = []

        self._state = AgentStateMachine()
        self._provider: str | None = None
        self._model: str | None = None
        self._attempt = 0
        self._created_at = time.time()
        self._started_at: float | None = None
        self._connected_at: float | None = None
        self._ended_at: float | None = None
        self._content_parts: list[str] = []
        self._response: Response | None = None
        self._errors: list[tuple[str, Exception]] = []
        self._cancel_reason: str | None = None
        self._request_configured = False
        self._started = False
        self._cleared = False
        self._context_entered = False
        self._running_task: asyncio.Task[Any] | None = None
        self._active_stream: AsyncIterator[StreamEvent] | None = None

        if not runtime.providers:
            transition = self._state.transition(AgentStatus.INIT_FAILED)
            self._ended_at = transition.occurred_at

    @property
    def system(self) -> str:
        return self._require_runtime().system

    @property
    def status(self) -> AgentStatus:
        self.ensure_available()
        return self._state.status

    @property
    def response(self) -> Response | None:
        self.ensure_available()
        return self._response

    @property
    def is_terminal(self) -> bool:
        return not self._cleared and self._state.is_terminal

    @property
    def is_cleared(self) -> bool:
        return self._cleared

    def snapshot(self) -> AgentSnapshot:
        self.ensure_available()
        now = self._ended_at or time.time()
        return AgentSnapshot(
            agent_id=self.agent_id,
            status=self._state.status,
            route=self.route,
            provider=self._provider,
            model=self._model,
            attempt=self._attempt,
            created_at=self._created_at,
            started_at=self._started_at,
            connected_at=self._connected_at,
            ended_at=self._ended_at,
            elapsed_seconds=0.0 if self._started_at is None else now - self._started_at,
            content="".join(self._content_parts),
            response=self._response,
            errors=tuple(self._errors),
            cancel_reason=self._cancel_reason,
            transitions=self._state.history,
        )

    def record_initialization_error(self, error: Exception) -> None:
        self._errors.append(("initialization", error))

    def configure_request(
        self,
        *,
        route: str,
        provider: str,
        messages: Iterable[Message | Mapping[str, Any]],
        temperature: float | None,
        max_output_tokens: int | None,
        response_schema: dict[str, Any] | None,
        request_id: str | None,
    ) -> None:
        self.ensure_available()
        if self._started or self._request_configured:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been configured"
            )
        self.route = route
        if not isinstance(provider, str) or not provider.strip():
            raise AgentError("Agent requires an explicit provider")
        self.requested_provider = provider.strip()
        self._messages = list(messages)
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.response_schema = response_schema
        if request_id is not None:
            self.agent_id = request_id
        self._request_configured = True
        logger.info(
            "agent_request_configured agent_id=%s route=%s provider=%s message_count=%s "
            "structured=%s max_output_tokens=%s",
            self.agent_id,
            self.route,
            self.requested_provider,
            len(self._messages),
            response_schema is not None,
            max_output_tokens,
        )

    async def start(self) -> Response:
        response: Response | None = None
        async for event in self.stream():
            if event.type is StreamEventType.COMPLETED:
                response = event.response
        if response is None:
            raise AgentError(f"Agent {self.agent_id!r} ended without a response")
        return response

    async def stream(self) -> AsyncIterator[StreamEvent]:
        self._claim_start()
        self._running_task = asyncio.current_task()
        try:
            runtime = self._require_runtime()
            route_policy = runtime.routes.get(self.route)
            if route_policy is None:
                logger.error(
                    "agent_route_missing agent_id=%s route=%s configured_routes=%s",
                    self.agent_id,
                    self.route,
                    sorted(runtime.routes),
                )
                await self.transition_to(AgentStatus.OUT_OF_USAGE)
                raise AgentError(f"Unknown route {self.route!r}")

            target = route_policy.target_for(self.requested_provider)
            if target is None:
                error = AgentError(
                    f"Provider {self.requested_provider!r} is not allowed for route "
                    f"{self.route!r}"
                )
                self._errors.append((self.requested_provider, error))
                logger.error(
                    "agent_provider_not_allowed agent_id=%s route=%s provider=%s "
                    "allowed_providers=%s",
                    self.agent_id,
                    self.route,
                    self.requested_provider,
                    [item.provider for item in route_policy.targets],
                )
                await self.transition_to(
                    AgentStatus.OUT_OF_USAGE,
                    provider=self.requested_provider,
                )
                raise error

            provider = runtime.providers.get(self.requested_provider)
            if provider is None:
                error = AgentError(
                    f"Provider {self.requested_provider!r} is not configured or is disabled"
                )
                self._errors.append((self.requested_provider, error))
                logger.error(
                    "agent_provider_missing agent_id=%s route=%s provider=%s",
                    self.agent_id,
                    self.route,
                    self.requested_provider,
                )
                await self.transition_to(
                    AgentStatus.OUT_OF_USAGE,
                    provider=self.requested_provider,
                )
                raise error

            prepared_messages = runtime.prepare_messages(self._messages)
            deadline = time.monotonic() + route_policy.deadline_seconds
            selected_model = target.model or provider.model
            logger.info(
                "agent_run_started agent_id=%s route=%s provider=%s model=%s "
                "deadline_seconds=%s structured=%s",
                self.agent_id,
                self.route,
                self.requested_provider,
                selected_model,
                route_policy.deadline_seconds,
                self.response_schema is not None,
            )

            await self.transition_to(
                AgentStatus.ATTEMPT_STARTED,
                provider=self.requested_provider,
                model=selected_model,
                attempt=1,
            )
            attempt_timeout = min(
                target.timeout_seconds,
                max(0.001, deadline - time.monotonic()),
            )
            attempt_started = time.monotonic()
            emitted_content = False
            logger.info(
                "agent_attempt_started agent_id=%s route=%s attempt=1 provider=%s "
                "model=%s timeout_seconds=%.3f deadline_remaining_seconds=%.3f",
                self.agent_id,
                self.route,
                self.requested_provider,
                selected_model,
                attempt_timeout,
                max(0.0, deadline - time.monotonic()),
            )

            timed_out = False
            try:
                final_response: Response | None = None
                async with asyncio.timeout(attempt_timeout):
                    async for event in provider.stream(
                        messages=prepared_messages,
                        model=target.model,
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                        response_schema=self.response_schema,
                        timeout=attempt_timeout,
                    ):
                        if event.type is StreamEventType.CONNECTED:
                            logger.info(
                                "agent_attempt_connected agent_id=%s route=%s attempt=1 "
                                "provider=%s model=%s connect_ms=%s",
                                self.agent_id,
                                self.route,
                                event.provider,
                                event.model,
                                int((time.monotonic() - attempt_started) * 1000),
                            )
                            await self.transition_to(
                                AgentStatus.CONNECTED,
                                provider=event.provider,
                                model=event.model,
                            )
                            yield event
                        elif event.type is StreamEventType.CONTENT_DELTA:
                            emitted_content = True
                            self._content_parts.append(event.content)
                            yield event
                        else:
                            final_response = event.response
                if final_response is None:
                    raise ProviderError(
                        "Provider stream ended without a result", retryable=True
                    )
            except TimeoutError as exc:
                timed_out = True
                attempt_error = ProviderError("Agent attempt timed out", retryable=True)
                attempt_error.__cause__ = exc
            except ProviderError as exc:
                attempt_error = exc
            except asyncio.CancelledError:
                logger.info(
                    "agent_attempt_cancelled agent_id=%s route=%s attempt=1 provider=%s "
                    "model=%s duration_ms=%s",
                    self.agent_id,
                    self.route,
                    self.requested_provider,
                    selected_model,
                    int((time.monotonic() - attempt_started) * 1000),
                )
                raise
            except Exception as exc:
                attempt_error = ProviderError(
                    "Unexpected provider failure", retryable=False
                )
                attempt_error.__cause__ = exc
            else:
                self._response = final_response
                logger.info(
                    "agent_attempt_succeeded agent_id=%s route=%s attempt=1 provider=%s "
                    "model=%s duration_ms=%s request_id=%s response_chars=%s "
                    "input_tokens=%s output_tokens=%s total_tokens=%s finish_reason=%s",
                    self.agent_id,
                    self.route,
                    final_response.provider,
                    final_response.model,
                    int((time.monotonic() - attempt_started) * 1000),
                    final_response.request_id,
                    len(final_response.content),
                    final_response.usage.input_tokens,
                    final_response.usage.output_tokens,
                    final_response.usage.total_tokens,
                    final_response.finish_reason,
                )
                await self.transition_to(
                    AgentStatus.RUN_COMPLETED,
                    provider=final_response.provider,
                    model=final_response.model,
                )
                yield StreamEvent(
                    StreamEventType.COMPLETED,
                    self.requested_provider,
                    final_response.model,
                    response=final_response,
                )
                return

            self._errors.append((self.requested_provider, attempt_error))
            logger.warning(
                "agent_attempt_failed agent_id=%s route=%s attempt=1 provider=%s "
                "model=%s duration_ms=%s emitted_content=%s retryable=%s "
                "status_code=%s provider_code=%s request_id=%s retry_after=%s "
                "limit_source=%s upstream_provider=%s remedy_hint=%s error_type=%s "
                "error=%s cause_type=%s",
                self.agent_id,
                self.route,
                self.requested_provider,
                selected_model,
                int((time.monotonic() - attempt_started) * 1000),
                emitted_content,
                attempt_error.retryable,
                attempt_error.status_code,
                getattr(attempt_error, "provider_code", None),
                getattr(attempt_error, "request_id", None),
                getattr(attempt_error, "retry_after", None),
                getattr(attempt_error, "limit_source", None),
                getattr(attempt_error, "upstream_provider", None),
                getattr(attempt_error, "remedy_hint", None),
                type(attempt_error).__name__,
                str(attempt_error).replace("\n", " ")[:300],
                type(attempt_error.__cause__).__name__
                if attempt_error.__cause__ is not None
                else None,
            )
            await self.transition_to(
                AgentStatus.RUN_TIMED_OUT if timed_out else AgentStatus.OUT_OF_USAGE
            )
            logger.error(
                "agent_run_failed agent_id=%s route=%s provider=%s status=%s "
                "attempts=1 error_type=%s",
                self.agent_id,
                self.route,
                self.requested_provider,
                self._state.status,
                type(attempt_error).__name__,
            )
            raise attempt_error
        except asyncio.CancelledError:
            await self._mark_cancelled()
            return
        finally:
            self._running_task = None

    async def cancel(self, reason: str = "Cancelled by caller") -> None:
        self.ensure_available()
        if self._state.is_terminal:
            return
        self._cancel_reason = reason
        logger.info(
            "agent_cancel_requested agent_id=%s route=%s status=%s reason=%s",
            self.agent_id,
            self.route,
            self._state.status,
            reason.replace("\n", " ")[:200],
        )
        if not self._started:
            self._started = True
            await self.transition_to(AgentStatus.RUN_CANCELLED)
            return
        running_task = self._running_task
        if running_task is not None and running_task is not asyncio.current_task():
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)
        await self._mark_cancelled()

    async def clear(self) -> None:
        """Cancel unfinished work and release retained request and result data."""
        self.ensure_available()
        if not self._state.is_terminal:
            await self.cancel(reason="Agent cleared by caller")
        await self.close_active_stream()
        self._messages.clear()
        self._content_parts.clear()
        self._errors.clear()
        self._response = None
        self.response_schema = None
        self._running_task = None
        self._runtime = None
        self._cleared = True
        logger.info("agent_cleared agent_id=%s route=%s", self.agent_id, self.route)

    def register_active_stream(self, stream: AsyncIterator[StreamEvent]) -> None:
        self._active_stream = stream

    def enter_context(self) -> None:
        self.ensure_available()
        if self._context_entered or self._started:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been entered or started"
            )
        self._context_entered = True

    async def close_active_stream(self) -> None:
        stream = self._active_stream
        self._active_stream = None
        if stream is not None:
            await stream.aclose()

    def unregister_active_stream(self, stream: AsyncIterator[StreamEvent]) -> None:
        if self._active_stream is stream:
            self._active_stream = None

    async def transition_to(
        self,
        status: AgentStatus,
        *,
        provider: str | None = None,
        model: str | None = None,
        attempt: int | None = None,
    ) -> None:
        transition = self._state.transition(status)
        if provider is not None:
            self._provider = provider
        if model is not None:
            self._model = model
        if attempt is not None:
            self._attempt = attempt
        if status is AgentStatus.ATTEMPT_STARTED and self._started_at is None:
            self._started_at = transition.occurred_at
        if status is AgentStatus.CONNECTED:
            self._connected_at = transition.occurred_at
        if status in TERMINAL_AGENT_STATUSES:
            self._ended_at = transition.occurred_at

        logger.info(
            "agent_state_changed agent_id=%s route=%s previous=%s current=%s "
            "provider=%s model=%s attempt=%s elapsed_ms=%s",
            self.agent_id,
            self.route,
            transition.previous,
            transition.current,
            self._provider,
            self._model,
            self._attempt,
            0
            if self._started_at is None
            else int((transition.occurred_at - self._started_at) * 1000),
        )

        runtime = self._runtime
        if runtime is not None:
            await runtime.emit_lifecycle_event(
                status.value,
                agent_id=self.agent_id,
                route=self.route,
                provider=self._provider,
                model=self._model,
                attempt=self._attempt,
            )

    def ensure_available(self) -> None:
        if self._cleared:
            raise AgentClearedError(f"Agent {self.agent_id!r} has been cleared")

    def _claim_start(self) -> None:
        self.ensure_available()
        if self._started:
            raise AgentAlreadyStartedError(
                f"Agent {self.agent_id!r} has already been started"
            )
        if not self._request_configured:
            raise AgentError("Agent request has not been configured")
        self._started = True
        if self._state.status is AgentStatus.INIT_FAILED:
            raise AgentError("Agent cannot start because no providers are configured")

    def _require_runtime(self) -> ProviderRuntime:
        self.ensure_available()
        if self._runtime is None:
            raise AgentClearedError(f"Agent {self.agent_id!r} has been cleared")
        return self._runtime

    async def _mark_cancelled(self) -> None:
        if not self._state.is_terminal:
            await self.transition_to(AgentStatus.RUN_CANCELLED)
