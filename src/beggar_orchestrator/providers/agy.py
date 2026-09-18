from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from typing import Any

from .base import (
    AuthenticationError,
    ProviderError,
    RateLimitError,
    Response,
    StreamEvent,
    StreamEventType,
    Usage,
    _safe_log_text,
)
from .cli import CLIProvider


logger = logging.getLogger(__name__)


class AgyProvider(CLIProvider):
    """Local provider adapter for Google Antigravity's ``agy`` CLI."""

    default_model = "gemini-3.8-flash-low"

    def __init__(
        self,
        *,
        name: str = "agy",
        token: str | None = None,
        model: str | None = None,
        executable: str = "agy",
        working_directory: str | None = None,
        extra_args: tuple[str, ...] = (),
    ):
        # agy authenticates with its own cached local credentials. ``token`` is
        # accepted only to keep provider construction transport-neutral.
        del token
        super().__init__(
            name=name,
            model=model or self.default_model,
            executable=executable,
            working_directory=working_directory,
            extra_args=extra_args,
        )

    @classmethod
    def from_config(
        cls,
        *,
        name: str,
        values: dict[str, Any],
        token: str | None,
    ) -> AgyProvider:
        extra_args = values.get("extra_args", ())
        if not isinstance(extra_args, (list, tuple)) or not all(
            isinstance(value, str) for value in extra_args
        ):
            raise ValueError("agy extra_args must be an array of strings")
        reserved = {
            "-p",
            "--print",
            "--prompt",
            "--input-format",
            "--output-format",
            "--json-schema",
            "--model",
            "--print-timeout",
        }
        if any(value.split("=", 1)[0] in reserved for value in extra_args):
            raise ValueError("agy extra_args cannot override transport-managed flags")
        return cls(
            name=name,
            token=token,
            model=values.get("model"),
            executable=str(values.get("executable", "agy")),
            working_directory=values.get("working_directory"),
            extra_args=tuple(extra_args),
        )

    @staticmethod
    def _prompt(messages: list[dict[str, Any]]) -> str:
        system_parts: list[str] = []
        conversation: list[tuple[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system":
                system_parts.append(content)
            else:
                conversation.append((role, content))

        sections: list[str] = []
        if system_parts:
            sections.append(
                "<system_instruction>\n"
                + "\n\n".join(system_parts)
                + "\n</system_instruction>"
            )
        sections.append(
            "<conversation>\n"
            + "\n\n".join(
                f"<{role}>\n{content}\n</{role}>" for role, content in conversation
            )
            + "\n</conversation>"
        )
        return "\n\n".join(sections)

    def _command(
        self,
        *,
        model: str,
        response_schema: dict[str, Any] | None,
        timeout: float,
    ) -> list[str]:
        command = [
            self.executable,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--model",
            model,
            "--print-timeout",
            f"{max(1, math.ceil(timeout))}s",
        ]
        if response_schema is not None:
            command.extend(
                [
                    "--json-schema",
                    json.dumps(
                        response_schema,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ]
            )
        command.extend(self.extra_args)
        return command

    @staticmethod
    def _usage(payload: dict[str, Any]) -> Usage:
        raw = payload.get("usage") or {}
        return Usage(
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            total_tokens=int(raw.get("total_tokens", 0) or 0),
        )

    @staticmethod
    def _failure(error: str, returncode: int | None = None) -> ProviderError:
        normalized = error.lower()
        safe_error = _safe_log_text(error)
        if any(value in normalized for value in ("authentication required", "unauthenticated")):
            return AuthenticationError(
                "agy authentication is required",
                provider_message=safe_error or None,
            )
        if any(value in normalized for value in ("rate limit", "quota", "resource_exhausted")):
            return RateLimitError(
                "agy quota or rate limit exceeded",
                provider_message=safe_error or None,
            )
        suffix = f" (exit status {returncode})" if returncode is not None else ""
        return ProviderError(
            f"agy request failed{suffix}",
            retryable=returncode != 2,
            provider_message=safe_error or None,
        )

    async def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> AsyncIterator[StreamEvent]:
        # agy 1.2.6 has no temperature/max-output-token flags. Keeping these
        # arguments in the provider contract lets API and CLI targets share a
        # route without transport-specific branching in AgentExecution.
        del temperature, max_output_tokens
        selected_model = model or self.model
        command = self._command(
            model=selected_model,
            response_schema=response_schema,
            timeout=timeout,
        )
        process = await self.start_process(command)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self.stop_process(process)
            raise ProviderError("agy process pipes were not created", retryable=True)

        started = time.monotonic()
        stderr_task = asyncio.create_task(process.stderr.read())
        parts: list[str] = []
        result_payload: dict[str, Any] | None = None

        prompt_event = {
            "event": "user",
            "message": {"content": self._prompt(messages)},
        }
        prompt_chars = len(prompt_event["message"]["content"])
        logger.info(
            "provider_request_started transport=cli provider=%s model=%s executable=%s "
            "timeout_seconds=%s structured=%s message_count=%s prompt_chars=%s",
            self.name,
            selected_model,
            self.executable,
            timeout,
            response_schema is not None,
            len(messages),
            prompt_chars,
        )

        try:
            process.stdin.write(
                (json.dumps(prompt_event, ensure_ascii=False) + "\n").encode("utf-8")
            )
            await process.stdin.drain()
            process.stdin.close()

            yield StreamEvent(StreamEventType.CONNECTED, self.name, selected_model)
            try:
                async with asyncio.timeout(timeout):
                    while line := await process.stdout.readline():
                        try:
                            payload = json.loads(line)
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                "provider_cli_output_invalid provider=%s model=%s line_bytes=%s",
                                self.name,
                                selected_model,
                                len(line),
                            )
                            raise ProviderError(
                                "agy returned invalid NDJSON output",
                                retryable=False,
                            ) from exc
                        event_type = payload.get("event")
                        if event_type == "step_update":
                            update = payload.get("step_update") or {}
                            content = update.get("text_delta")
                            if isinstance(content, str) and content:
                                parts.append(content)
                                yield StreamEvent(
                                    StreamEventType.CONTENT_DELTA,
                                    self.name,
                                    selected_model,
                                    content=content,
                                )
                        elif event_type == "result":
                            candidate = payload.get("result")
                            if isinstance(candidate, dict):
                                result_payload = candidate
                                logger.info(
                                    "provider_cli_result_received provider=%s model=%s "
                                    "status=%s request_id=%s duration_ms=%s",
                                    self.name,
                                    selected_model,
                                    candidate.get("status"),
                                    candidate.get("conversation_id"),
                                    int((time.monotonic() - started) * 1000),
                                )
                    returncode = await process.wait()
            except TimeoutError as exc:
                logger.warning(
                    "provider_request_failed transport=cli provider=%s model=%s "
                    "error_type=timeout duration_ms=%s",
                    self.name,
                    selected_model,
                    int((time.monotonic() - started) * 1000),
                )
                raise ProviderError("agy request timed out", retryable=True) from exc

            stderr = (await stderr_task).decode("utf-8", "replace")
            if returncode != 0:
                classified = self._failure(stderr, returncode)
                logger.warning(
                    "provider_request_failed transport=cli provider=%s model=%s "
                    "error_type=%s returncode=%s retryable=%s duration_ms=%s "
                    "provider_message=%s",
                    self.name,
                    selected_model,
                    type(classified).__name__,
                    returncode,
                    classified.retryable,
                    int((time.monotonic() - started) * 1000),
                    classified.provider_message,
                )
                raise classified
            if result_payload is None:
                logger.warning(
                    "provider_request_failed transport=cli provider=%s model=%s "
                    "error_type=missing_result returncode=%s duration_ms=%s",
                    self.name,
                    selected_model,
                    returncode,
                    int((time.monotonic() - started) * 1000),
                )
                raise ProviderError("agy ended without a result event", retryable=True)

            status = str(result_payload.get("status") or "").upper()
            if status != "SUCCESS":
                classified = self._failure(str(result_payload.get("error") or status))
                logger.warning(
                    "provider_request_failed transport=cli provider=%s model=%s status=%s "
                    "request_id=%s error_type=%s retryable=%s duration_ms=%s "
                    "provider_message=%s",
                    self.name,
                    selected_model,
                    status,
                    result_payload.get("conversation_id"),
                    type(classified).__name__,
                    classified.retryable,
                    int((time.monotonic() - started) * 1000),
                    classified.provider_message,
                )
                raise classified

            structured = result_payload.get("structured_output")
            if response_schema is not None and structured is not None:
                content = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
            else:
                content = str(result_payload.get("response") or "")

            if not parts and content:
                parts.append(content)
                yield StreamEvent(
                    StreamEventType.CONTENT_DELTA,
                    self.name,
                    selected_model,
                    content=content,
                )

            response = Response(
                content=content,
                provider=self.name,
                model=selected_model,
                request_id=result_payload.get("conversation_id"),
                latency_ms=int((time.monotonic() - started) * 1000),
                usage=self._usage(result_payload),
                finish_reason=status.lower(),
            )
            logger.info(
                "provider_request_completed transport=cli provider=%s model=%s request_id=%s "
                "duration_ms=%s input_tokens=%s output_tokens=%s total_tokens=%s "
                "finish_reason=%s response_chars=%s structured=%s",
                self.name,
                selected_model,
                response.request_id,
                response.latency_ms,
                response.usage.input_tokens,
                response.usage.output_tokens,
                response.usage.total_tokens,
                response.finish_reason,
                len(response.content),
                response_schema is not None,
            )
            yield StreamEvent(
                StreamEventType.COMPLETED,
                self.name,
                selected_model,
                response=response,
            )
        finally:
            await self.stop_process(process)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
