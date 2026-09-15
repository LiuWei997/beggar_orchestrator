from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class AuthenticationError(ProviderError):
    def __init__(self, message: str = "Provider authentication failed"):
        super().__init__(message, retryable=False, status_code=401)


class RateLimitError(ProviderError):
    def __init__(self, message: str = "Provider rate limit exceeded"):
        super().__init__(message, retryable=True, status_code=429)


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


class LLMProvider:
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
    def _raise_for_status(status_code: int) -> None:
        if status_code in {401, 403}:
            raise AuthenticationError(f"Provider rejected credential with HTTP {status_code}")
        if status_code == 429:
            raise RateLimitError()
        if status_code >= 500:
            raise ProviderError(
                f"Provider returned HTTP {status_code}", retryable=True, status_code=status_code
            )
        if status_code >= 400:
            raise ProviderError(
                f"Provider returned HTTP {status_code}", retryable=False, status_code=status_code
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
                    self._raise_for_status(response.status_code)
                    request_id = response.headers.get("x-request-id")
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
                            raise ProviderError(
                                "Provider returned an error SSE event", retryable=False
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
            raise ProviderError("Provider stream timed out", retryable=True) from exc
        except httpx.NetworkError as exc:
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
