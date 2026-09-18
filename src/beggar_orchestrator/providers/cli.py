from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from .base import ProviderError, Response, StreamEvent, StreamEventType


logger = logging.getLogger(__name__)


class CLIProvider(ABC):
    """Base implementation for local command-line provider adapters."""

    requires_credential = False

    def __init__(
        self,
        *,
        name: str,
        model: str,
        executable: str,
        working_directory: str | None = None,
        extra_args: tuple[str, ...] = (),
    ):
        self.name = name
        self.model = model
        self.executable = executable
        self.working_directory = working_directory
        self.extra_args = extra_args

    async def start_process(self, argv: list[str]) -> asyncio.subprocess.Process:
        logger.info(
            "provider_cli_process_starting provider=%s model=%s executable=%s cwd=%s",
            self.name,
            self.model,
            self.executable,
            self.working_directory,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=os.environ.copy(),
            )
            logger.info(
                "provider_cli_process_started provider=%s model=%s executable=%s pid=%s",
                self.name,
                self.model,
                self.executable,
                process.pid,
            )
            return process
        except FileNotFoundError as exc:
            logger.error(
                "provider_cli_process_failed provider=%s executable=%s error_type=not_found",
                self.name,
                self.executable,
            )
            raise ProviderError(
                f"CLI executable {self.executable!r} was not found",
                retryable=False,
            ) from exc
        except OSError as exc:
            logger.warning(
                "provider_cli_process_failed provider=%s executable=%s error_type=%s",
                self.name,
                self.executable,
                type(exc).__name__,
            )
            raise ProviderError(
                f"CLI provider {self.name!r} could not start",
                retryable=True,
            ) from exc

    @staticmethod
    async def stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        logger.info("provider_cli_process_terminating pid=%s", process.pid)
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            logger.warning("provider_cli_process_killing pid=%s reason=terminate_timeout", process.pid)
            process.kill()
            await process.wait()

    @abstractmethod
    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

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
