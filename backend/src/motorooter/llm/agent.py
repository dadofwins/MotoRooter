"""The tool-calling loop.

Send the conversation, run whatever tools the model asks for, feed the results back, repeat
until it stops asking. Events are yielded as they happen so the map fills in while the
assistant works rather than after — the whole point of streaming here is that discovery is
slow and a spinner is a worse answer than partial results.

**The model is untrusted input.** It will invent tool names, send malformed arguments, send
well-formed arguments of the wrong shape, and keep calling tools forever. None of those may
reach the caller as an exception, because none of them is a server fault: each is reported
back to the model, which can usually correct itself, and the conversation continues.

The one thing the loop enforces unilaterally is a turn ceiling. Cost is explicitly not the
design driver on this project, but a model looping on a tool is a *bug* that happens to
spend money, and this is the only place with the standing to stop it.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

from motorooter.llm.errors import ToolCallFailed
from motorooter.llm.messages import AssistantMessage, Message, ToolMessage
from motorooter.llm.protocol import LlmClient
from motorooter.llm.tools import ToolOutcome, ToolRegistry

DEFAULT_MAX_TURNS = 8
"""Turns before the loop stops asking. Enough for search, resolve, judge and a summary."""

EventKind = Literal["message", "tool_started", "tool_finished", "tool_failed", "truncated", "done"]


@dataclass(frozen=True)
class AgentEvent:
    """One thing that happened, in the order it happened."""

    kind: EventKind
    message: str = ""
    tool: str | None = None
    outcome: ToolOutcome | None = None


class Agent:
    """Runs a conversation to completion, yielding progress as it goes."""

    def __init__(
        self,
        client: LlmClient,
        registry: ToolRegistry,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if max_turns < 1:
            msg = f"max_turns must be at least 1, got {max_turns}"
            raise ValueError(msg)
        self._client = client
        self._registry = registry
        self._max_turns = max_turns

    async def run(self, messages: Sequence[Message]) -> AsyncIterator[AgentEvent]:
        """Drive the conversation, yielding events until the model stops calling tools."""
        conversation: list[Message] = list(messages)
        specs = self._registry.specs()

        for _ in range(self._max_turns):
            reply = await self._client.complete(conversation, specs)
            conversation.append(reply)

            if reply.content:
                yield AgentEvent(kind="message", message=reply.content)

            if not reply.tool_calls:
                yield AgentEvent(kind="done")
                return

            for call in reply.tool_calls:
                yield AgentEvent(kind="tool_started", tool=call.name)
                event, result = await self._invoke(call.id, call.name, call.arguments)
                conversation.append(result)
                yield event
        else:
            # The ceiling, not a natural end. Said out loud: a truncated conversation and a
            # finished one look identical from the outside, and the difference matters —
            # one means the assistant is done, the other means it was cut off mid-task.
            yield AgentEvent(
                kind="truncated",
                message=f"stopped after {self._max_turns} turns without a final answer",
            )
        yield AgentEvent(kind="done")

    async def _invoke(
        self, call_id: str, name: str, arguments: str
    ) -> tuple[AgentEvent, ToolMessage]:
        """Run one tool call, converting any failure into something the model can read."""
        try:
            tool = self._registry.get(name)
            parsed = tool.parse(arguments)
        except ToolCallFailed as exc:
            return self._failure(call_id, name, str(exc))

        try:
            outcome = await tool.run(parsed)
        except ToolCallFailed as exc:
            return self._failure(call_id, name, str(exc))
        except Exception as exc:  # noqa: BLE001 -- see below
            # Any tool failure, not just the declared kind. A tool wrapping a live service
            # can fail in ways its author did not enumerate, and ending the conversation on
            # an unexpected exception would lose every result already produced in this run.
            # `BaseException` is deliberately not caught, so cancellation still cancels.
            return self._failure(call_id, name, f"{type(exc).__name__}: {exc}")

        return (
            AgentEvent(kind="tool_finished", tool=name, outcome=outcome),
            ToolMessage(call_id=call_id, content=outcome.content),
        )

    @staticmethod
    def _failure(call_id: str, name: str, detail: str) -> tuple[AgentEvent, ToolMessage]:
        """A failed call still owes the model a reply addressed to that call.

        Omitting it leaves a tool call with no result in the conversation, which providers
        reject outright — so a tool failure would surface as a malformed-request error two
        turns later, nowhere near its cause.
        """
        return (
            AgentEvent(kind="tool_failed", tool=name, message=detail),
            ToolMessage(call_id=call_id, content=f"Error: {detail}"),
        )


__all__ = ["Agent", "AgentEvent", "AssistantMessage", "EventKind", "ToolCallFailed"]
