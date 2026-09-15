from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .lifecycle import AgentError

Role = Literal["system", "developer", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls("assistant", content)

    @classmethod
    def tool(
        cls, content: str, *, tool_call_id: str, name: str | None = None
    ) -> Message:
        return cls("tool", content, name=name, tool_call_id=tool_call_id)

    def as_dict(self) -> dict[str, str]:
        value = {"role": self.role, "content": self.content}
        if self.name:
            value["name"] = self.name
        if self.tool_call_id:
            value["tool_call_id"] = self.tool_call_id
        return value


def normalize_messages(
    system: str,
    messages: Iterable[Message | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate caller messages and prepend the Agent's fixed system message."""
    normalized: list[dict[str, Any]] = [Message.system(system).as_dict()]
    for message in messages:
        value = message.as_dict() if isinstance(message, Message) else dict(message)
        if value.get("role") == "system":
            raise AgentError("System messages belong to Agent initialization")
        if value.get("role") not in {"developer", "user", "assistant", "tool"}:
            raise AgentError(f"Unsupported message role: {value.get('role')!r}")
        if not isinstance(value.get("content"), str):
            raise AgentError("Every message requires string content")
        normalized.append(value)
    if len(normalized) == 1:
        raise AgentError("At least one conversation message is required")
    return normalized
