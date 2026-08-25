"""The conversation, in provider-neutral shapes.

Modelled on the OpenAI chat vocabulary because that is the first adapter, but nothing here
is OpenAI-specific: adapters translate to and from their own wire format, exactly as routing
providers do. Nothing provider-shaped escapes an adapter module.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class SystemMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system"] = "system"
    content: str


class UserMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user"] = "user"
    content: str


class ToolCall(BaseModel):
    """A tool the model wants run.

    `arguments` stays a raw string. It is the model's output, so it may be malformed JSON or
    well-formed JSON of the wrong shape; parsing it is the tool layer's job and either
    failure has to be reportable back to the model rather than fatal here.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: str


class AssistantMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class ToolMessage(BaseModel):
    """The result of one tool call, addressed to the call that asked for it."""

    model_config = ConfigDict(frozen=True)

    role: Literal["tool"] = "tool"
    call_id: str
    content: str


Message = SystemMessage | UserMessage | AssistantMessage | ToolMessage
