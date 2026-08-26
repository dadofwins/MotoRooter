"""The tool-calling loop.

Send the conversation, run whatever tools the model asks for, feed the results back, repeat
until it stops asking. Events are yielded as they happen so the map fills in while the
assistant works rather than after — the whole point of streaming here is that discovery is
slow and a spinner is a worse answer than partial results.

**The model is untrusted input.** It will invent tool names, send malformed arguments, send
well-formed arguments of the wrong shape, nest them 60,000 deep, and keep calling tools
forever. None of those is a server fault and none may reach the caller as an exception: each
is reported back to the model, which can usually correct itself, and the conversation
continues.

Two things the loop enforces unilaterally.

**Nothing internal is ever told to the model.** Trips are unauthenticated, and every string
handed to the model is also streamed to a browser, so an anonymous caller who can induce a
tool failure can read back whatever the exception happened to carry: a filesystem path, a
bucket name, an upstream URL with a key in its query string. Domain failures are forwarded
in full — we author those messages, so their content is a decision. Anything else is logged
server-side and described to the model as "the <tool> tool failed", which is enough for it
to route around and nothing at all for an attacker.

**Work is bounded on every axis that can run away, not just turns.** A turn is a model
round-trip, which is not a unit of work: one reply carrying five thousand tool calls is five
thousand executions inside a single turn, and a tool returning large JSON exhausts the
context window long before the turn budget. Cost is explicitly not the design driver on this
project, but a runaway loop is a *bug* that happens to spend money, and this is the only
place with the standing to stop it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from motorooter.clock import Clock, SystemClock
from motorooter.llm.errors import LlmError, ToolCallFailed
from motorooter.llm.messages import Message, ToolCall, ToolMessage
from motorooter.llm.protocol import LlmClient
from motorooter.llm.tools import ProgressReport, ToolOutcome, ToolRegistry
from motorooter.routing.errors import RoutingError
from motorooter.trips.errors import TripError

logger = logging.getLogger(__name__)

EventKind = Literal[
    "message", "tool_started", "tool_progress", "tool_finished", "tool_failed", "done"
]


_DOMAIN_ERRORS = (ToolCallFailed, LlmError, RoutingError, TripError)
"""Failures whose messages we write, and which therefore may be shown to the model.

Everything else is sanitised. The distinction is authorship, not severity: a message this
project composes is a decision about what to disclose, and a message from a library or an
upstream is whatever that third party happened to put in it.
"""


@dataclass(frozen=True)
class AgentLimits:
    """Ceilings on a single run. Every one of these has a way to run away without it."""

    max_turns: int = 20
    """Model round-trips before the run is stopped.

    Was 8, chosen when the agent had no tools and justified as "enough for search, resolve,
    judge and a summary" — which describes a discovery run, not a conversation that edits a
    trip. Measured against a real model on a three-day route request, the assistant added
    seven waypoints one per turn and was cut off mid-route: the ceiling was not protecting
    against runaway work, it was stopping ordinary work, and the rider saw a half-built trip.

    Twenty covers a multi-day route — a dozen or so places, the reads between them, and a
    summary — with room before it bites. `max_tool_calls` is the limit that actually guards
    spend; this one guards against a model that will not stop talking.
    """

    max_tool_calls: int = 64
    """Tool executions across the whole run, however they are distributed across turns."""

    max_tool_output_chars: int = 400_000
    """Cumulative characters of tool output fed back. The context window is the real
    constraint, and hitting it surfaces as a provider 400 after the turns are already paid
    for — long after anything could be done about it."""

    max_result_chars: int = 50_000
    """Characters from any one tool result. Distinct from the cumulative cap: trimming a
    single large result to whatever is *left* of the total would exhaust the budget in the
    act of trimming, so the model would never see the trimmed result at all."""

    max_seconds: float = 300.0
    """Wall clock. A slow turn multiplied by the turn budget is a very long anonymous
    request."""

    max_refused_turns: int = 3
    """Turns where every call was refused before running — an invented tool name, or
    arguments that would not parse. These do not consume `max_turns`, so one wrong guess
    does not burn the run, but they cannot continue forever either."""

    max_consecutive_tool_failures: int = 3
    """Failures of one tool before it is withdrawn for the rest of the run. A dead upstream
    should be tried a few times and then stopped, not retried until the budget is gone."""

    def __post_init__(self) -> None:
        for name in (
            "max_turns",
            "max_tool_calls",
            "max_tool_output_chars",
            "max_result_chars",
            "max_seconds",
            "max_refused_turns",
            "max_consecutive_tool_failures",
        ):
            if getattr(self, name) <= 0:
                msg = f"{name} must be positive, got {getattr(self, name)}"
                raise ValueError(msg)


@dataclass(frozen=True)
class AgentEvent:
    """One thing that happened, in the order it happened."""

    kind: EventKind
    message: str = ""
    tool: str | None = None
    outcome: ToolOutcome | None = None

    progress: float | None = None
    """How far through the current tool, on a `tool_progress` event.

    Within the tool, not within the turn. A turn's total work is decided by the model while
    it is deciding it, so a turn-level fraction would be invented — the same reason the
    discovery bar counts work units rather than anchors.
    """

    truncated: bool = False
    """Set on the terminal `done` event when a limit stopped the run.

    On the terminal event specifically, because a consumer reading only the last event has
    to be able to tell "the assistant finished" from "the assistant was cut off mid-task",
    and those mean very different things to whoever is waiting.
    """


@dataclass
class _Budget:
    """Mutable counters for one run."""

    limits: AgentLimits
    started_at: float
    tool_calls: int = 0
    output_chars: int = 0
    refused_turns: int = 0
    consecutive_failures: dict[str, int] = field(default_factory=dict)

    def exhausted(self, now: float) -> str | None:
        """The limit that has been reached, if any, phrased for a human."""
        if self.tool_calls >= self.limits.max_tool_calls:
            return f"reached the limit of {self.limits.max_tool_calls} tool calls"
        if self.output_chars >= self.limits.max_tool_output_chars:
            return "reached the limit on how much tool output can be considered at once"
        if now - self.started_at >= self.limits.max_seconds:
            return f"ran out of time after {self.limits.max_seconds:.0f} seconds"
        if self.refused_turns >= self.limits.max_refused_turns:
            return "made too many calls that could not be run"
        return None

    def withdrawn(self, tool: str) -> bool:
        return self.consecutive_failures.get(tool, 0) >= self.limits.max_consecutive_tool_failures


class Agent:
    """Runs a conversation to completion, yielding progress as it goes."""

    def __init__(
        self,
        client: LlmClient,
        registry: ToolRegistry,
        *,
        limits: AgentLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._limits = limits or AgentLimits()
        self._clock = clock or SystemClock()

    async def run(self, messages: Sequence[Message]) -> AsyncIterator[AgentEvent]:
        """Drive the conversation, yielding events until the model stops or a limit does."""
        conversation: list[Message] = list(messages)
        specs = self._registry.specs()
        budget = _Budget(limits=self._limits, started_at=self._clock.now())
        turns = 0

        while turns < self._limits.max_turns:
            reached = budget.exhausted(self._clock.now())
            if reached is not None:
                yield self._stopped(reached)
                return

            reply = await self._client.complete(conversation, specs)
            conversation.append(reply)

            if reply.content:
                yield AgentEvent(kind="message", message=reply.content)

            if not reply.tool_calls:
                yield AgentEvent(kind="done")
                return

            executed = 0
            for call in reply.tool_calls:
                if budget.exhausted(self._clock.now()) is not None:
                    break
                yield AgentEvent(kind="tool_started", tool=call.name)

                if self._reports_progress(call.name):
                    # Progress escapes through a queue because the tool is awaited: anything
                    # it reports while running cannot be yielded from here until the await
                    # returns, which is the thirty seconds a rider is waiting through.
                    #
                    # Only for tools that opt in. Running every tool in a task would change
                    # exception semantics for all of them — a `BaseException` raised inside
                    # a task does not surface the way an awaited one does, which an existing
                    # test caught immediately.
                    async for update, done in self._with_progress(budget, call):
                        if done is None:
                            yield update
                        else:
                            event, result, ran = done
                else:
                    event, result, ran = await self._invoke(
                        budget, call.id, call.name, call.arguments
                    )

                conversation.append(self._record(budget, result))
                executed += int(ran)
                yield event

            # A turn where nothing actually ran was a wasted guess, not progress. Charging
            # it to the turn budget would let one invented tool name consume the whole run
            # before any real work happened.
            if executed:
                turns += 1
            else:
                budget.refused_turns += 1

        yield self._stopped(f"stopped after {self._limits.max_turns} turns without finishing")

    @staticmethod
    def _stopped(reason: str) -> AgentEvent:
        return AgentEvent(kind="done", message=reason, truncated=True)

    def _record(self, budget: _Budget, result: ToolMessage) -> ToolMessage:
        """Add a tool result to the conversation, trimmed to what the budget allows.

        Truncated rather than dropped: partial evidence is worth more than none, and the
        model is told it was cut so it does not treat a half list as the whole answer.
        """
        allowed = min(
            budget.limits.max_result_chars,
            budget.limits.max_tool_output_chars - budget.output_chars,
        )
        content = result.content
        if len(content) > allowed:
            content = content[: max(allowed, 0)] + "\n… (truncated: result was too large)"
        budget.output_chars += len(content)
        return result.model_copy(update={"content": content})

    def _reports_progress(self, name: str) -> bool:
        """Whether the named tool accepts an `on_progress` callback.

        Resolved here rather than inside `_invoke` so the caller knows, before it starts,
        whether it needs the machinery that lets a running tool speak.
        """
        try:
            return self._registry.get(name).reports_progress
        except ToolCallFailed:
            return False

    async def _with_progress(
        self, budget: _Budget, call: ToolCall
    ) -> AsyncIterator[tuple[AgentEvent, tuple[AgentEvent, ToolMessage, bool] | None]]:
        """Yield progress as it arrives, then the outcome last."""
        updates: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

        def report(message: str, fraction: float | None) -> None:
            updates.put_nowait(
                AgentEvent(kind="tool_progress", tool=call.name, message=message, progress=fraction)
            )

        async def invoke() -> tuple[AgentEvent, ToolMessage, bool]:
            try:
                return await self._invoke(budget, call.id, call.name, call.arguments, report)
            finally:
                updates.put_nowait(None)

        runner = asyncio.create_task(invoke())
        try:
            while True:
                update = await updates.get()
                if update is None:
                    break
                yield update, None
            outcome = await runner
            yield outcome[0], outcome
        finally:
            runner.cancel()

    async def _invoke(
        self,
        budget: _Budget,
        call_id: str,
        name: str,
        arguments: str,
        report: ProgressReport | None = None,
    ) -> tuple[AgentEvent, ToolMessage, bool]:
        """Run one tool call. The bool reports whether the tool actually executed."""
        if budget.withdrawn(name):
            detail = (
                f"the {name} tool is unavailable: it failed "
                f"{budget.limits.max_consecutive_tool_failures} times in a row and will not "
                "be called again during this request"
            )
            return (*self._failure(call_id, name, detail), False)

        try:
            tool = self._registry.get(name)
            parsed = tool.parse(arguments)
        except ToolCallFailed as exc:
            # Refused before running: the model's mistake, and correctable. Not charged to
            # the tool's failure count, which is for the tool being broken.
            return (*self._failure(call_id, name, str(exc)), False)

        budget.tool_calls += 1
        try:
            outcome = await tool.run(parsed, on_progress=report)
        except _DOMAIN_ERRORS as exc:
            budget.consecutive_failures[name] = budget.consecutive_failures.get(name, 0) + 1
            return (*self._failure(call_id, name, str(exc)), True)
        except Exception:
            budget.consecutive_failures[name] = budget.consecutive_failures.get(name, 0) + 1
            # The detail goes to the operator, never to the model: this is the path where
            # the text came from somewhere we do not control.
            logger.exception("tool %r raised an unexpected error", name)
            return (*self._failure(call_id, name, f"the {name} tool failed unexpectedly"), True)

        budget.consecutive_failures[name] = 0
        return (
            AgentEvent(kind="tool_finished", tool=name, outcome=outcome),
            ToolMessage(call_id=call_id, content=outcome.content),
            True,
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


__all__ = ["Agent", "AgentEvent", "AgentLimits", "EventKind"]
