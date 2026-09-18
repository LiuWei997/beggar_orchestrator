from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


def _safe_log_text(value: Any, limit: int = 300) -> str:
    """Keep useful provider diagnostics without logging credential-shaped data."""
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(r"\bAIza[A-Za-z0-9_-]{20,}\b", "[REDACTED_KEY]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization)([\"'=:\s]+)"
        r"[A-Za-z0-9._~+/=-]{8,}",
        r"\1\2[REDACTED]",
        text,
    )
    return text[:limit]


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
        limit_source: str | None = None,
        upstream_provider: str | None = None,
        remedy_hint: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.request_id = request_id
        self.retry_after = retry_after
        self.limit_source = limit_source
        self.upstream_provider = upstream_provider
        self.remedy_hint = remedy_hint


class AuthenticationError(ProviderError):
    def __init__(self, message: str = "Provider authentication failed", **details: Any):
        super().__init__(message, retryable=False, status_code=401, **details)


class RateLimitError(ProviderError):
    def __init__(self, message: str = "Provider rate limit exceeded", **details: Any):
        super().__init__(message, retryable=True, status_code=429, **details)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Response:
    content: str
    provider: str
    model: str
    request_id: str | None
    latency_ms: int
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


class StreamEventType(StrEnum):
    CONNECTED = "connected"
    CONTENT_DELTA = "content_delta"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    type: StreamEventType
    provider: str
    model: str
    content: str = ""
    response: Response | None = None


@runtime_checkable
class Provider(Protocol):
    """Transport-neutral contract implemented by API and CLI adapters."""

    name: str
    model: str

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> AsyncIterator[StreamEvent]: ...

    async def verify(self, *, timeout: float = 20) -> Response: ...


class LLMProvider:
    """Base implementation for OpenAI-compatible HTTP API adapters."""

    requires_credential = True

    def __init__(
        self,
        *,
        name: str,
        token: str | None,
        base_url: str,
        model: str,
        headers: dict[str, str] | None = None,
        schema_format: str = "openai_json_schema",
    ):
        self.name = name
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = dict(headers or {})
        self.schema_format = schema_format

    @classmethod
    def from_config(
        cls,
        *,
        name: str,
        values: dict[str, Any],
        token: str | None,
    ) -> LLMProvider:
        return cls(name=name, token=token, model=values.get("model"))

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise AuthenticationError(f"Credential is missing for provider {self.name!r}")
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            **self.headers,
        }

    def _body(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        max_output_tokens: int | None,
        response_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        if response_schema is not None:
            if self.schema_format == "cohere_json_object":
                body["response_format"] = {"type": "json_object", "schema": response_schema}
            else:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
        return body

    @staticmethod
    def _error_details(payload: Any) -> dict[str, str | None]:
        if not isinstance(payload, dict):
            return {}
        error = payload.get("error")
        if not isinstance(error, dict):
            error = payload
        metadata = error.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "provider_code": _safe_log_text(error.get("code"), 80) or None,
            "provider_message": _safe_log_text(error.get("message")) or None,
            "limit_source": _safe_log_text(metadata.get("limit_source"), 120) or None,
            "upstream_provider": _safe_log_text(metadata.get("provider_name"), 120)
            or None,
            "remedy_hint": _safe_log_text(metadata.get("remedy_hint")) or None,
        }

    async def _raise_for_response(
        self,
        response: Any,
        *,
        model: str,
        started: float,
    ) -> None:
        status_code = response.status_code
        if status_code < 400:
            return
        request_id = response.headers.get("x-request-id")
        retry_after = response.headers.get("retry-after")
        details: dict[str, str | None] = {}
        try:
            raw = await response.aread()
            details = self._error_details(json.loads(raw))
        except (TypeError, ValueError):
            pass
        common = {
            **details,
            "request_id": request_id,
            "retry_after": retry_after,
        }
        logger.warning(
            "provider_http_error provider=%s model=%s status=%s duration_ms=%s "
            "request_id=%s provider_code=%s limit_source=%s upstream_provider=%s "
            "retry_after=%s provider_message=%s remedy_hint=%s",
            self.name,
            model,
            status_code,
            int((time.monotonic() - started) * 1000),
            request_id,
            details.get("provider_code"),
            details.get("limit_source"),
            details.get("upstream_provider"),
            retry_after,
            details.get("provider_message"),
            details.get("remedy_hint"),
        )
        if status_code in {401, 403}:
            raise AuthenticationError(
                f"Provider rejected credential with HTTP {status_code}", **common
            )
        if status_code == 429:
            raise RateLimitError(**common)
        if status_code >= 500:
            raise ProviderError(
                f"Provider returned HTTP {status_code}",
                retryable=True,
                status_code=status_code,
                **common,
            )
        if status_code >= 400:
            raise ProviderError(
                f"Provider returned HTTP {status_code}",
                retryable=False,
                status_code=status_code,
                **common,
            )

    @staticmethod
    def _usage(payload: dict[str, Any]) -> Usage:
        usage = payload.get("usage") or {}
        return Usage(
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
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
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("Install package dependencies before calling a provider") from exc

        selected_model = model or self.model
        started = time.monotonic()
        parts: list[str] = []
        request_id: str | None = None
        finish_reason: str | None = None
        usage = Usage()

        logger.info(
            "provider_request_started transport=http provider=%s model=%s timeout_seconds=%s "
            "structured=%s message_count=%s max_output_tokens=%s",
            self.name,
            selected_model,
            timeout,
            response_schema is not None,
            len(messages),
            max_output_tokens,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._body(
                        messages=messages,
                        model=selected_model,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        response_schema=response_schema,
                    ),
                ) as response:
                    request_id = response.headers.get("x-request-id")
                    logger.info(
                        "provider_response_received transport=http provider=%s model=%s "
                        "status=%s request_id=%s duration_ms=%s",
                        self.name,
                        selected_model,
                        response.status_code,
                        request_id,
                        int((time.monotonic() - started) * 1000),
                    )
                    await self._raise_for_response(
                        response,
                        model=selected_model,
                        started=started,
                    )
                    yield StreamEvent(
                        StreamEventType.CONNECTED,
                        self.name,
                        selected_model,
                    )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            payload = json.loads(data)
                        except ValueError as exc:
                            raise ProviderError(
                                "Provider returned an invalid SSE event", retryable=False
                            ) from exc
                        if payload.get("error"):
                            details = self._error_details(payload)
                            logger.warning(
                                "provider_sse_error provider=%s model=%s request_id=%s "
                                "provider_code=%s limit_source=%s upstream_provider=%s "
                                "provider_message=%s remedy_hint=%s",
                                self.name,
                                selected_model,
                                request_id,
                                details.get("provider_code"),
                                details.get("limit_source"),
                                details.get("upstream_provider"),
                                details.get("provider_message"),
                                details.get("remedy_hint"),
                            )
                            raise ProviderError(
                                "Provider returned an error SSE event",
                                retryable=False,
                                request_id=request_id,
                                **details,
                            )
                        request_id = payload.get("id") or request_id
                        selected_model = payload.get("model") or selected_model
                        if payload.get("usage"):
                            usage = self._usage(payload)
                        choices = payload.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            if not isinstance(content, str):
                                raise ProviderError(
                                    "Provider returned unsupported streamed content",
                                    retryable=False,
                                )
                            parts.append(content)
                            yield StreamEvent(
                                StreamEventType.CONTENT_DELTA,
                                self.name,
                                selected_model,
                                content=content,
                            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "provider_request_failed transport=http provider=%s model=%s "
                "error_type=timeout duration_ms=%s",
                self.name,
                selected_model,
                int((time.monotonic() - started) * 1000),
            )
            raise ProviderError("Provider stream timed out", retryable=True) from exc
        except httpx.NetworkError as exc:
            logger.warning(
                "provider_request_failed transport=http provider=%s model=%s "
                "error_type=network duration_ms=%s",
                self.name,
                selected_model,
                int((time.monotonic() - started) * 1000),
            )
            raise ProviderError("Provider network request failed", retryable=True) from exc

        result = Response(
            content="".join(parts),
            provider=self.name,
            model=selected_model,
            request_id=request_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=usage,
            finish_reason=finish_reason,
        )
        logger.info(
            "provider_request_completed transport=http provider=%s model=%s request_id=%s "
            "duration_ms=%s input_tokens=%s output_tokens=%s total_tokens=%s "
            "finish_reason=%s response_chars=%s",
            self.name,
            selected_model,
            request_id,
            result.latency_ms,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            finish_reason,
            len(result.content),
        )
        yield StreamEvent(
            StreamEventType.COMPLETED,
            self.name,
            selected_model,
            response=result,
        )

    async def verify(self, *, timeout: float = 20) -> Response:
        result: Response | None = None
        async for event in self.stream(
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            max_output_tokens=128,
            timeout=timeout,
        ):
            if event.type is StreamEventType.COMPLETED:
                result = event.response
        if result is None:
            raise ProviderError("Provider stream ended without a result", retryable=True)
        return result
