"""Bounds, and what the model is allowed to be told.

Two separate concerns, both about a run that goes wrong rather than one that goes right.

**Disclosure.** Trips are unauthenticated. Anything handed to the model also reaches the
screen, and an anonymous caller who can induce a tool error can read back whatever the
exception happened to contain — a filesystem path, a bucket name, an upstream URL. The rule
here is absolute: no exception's own text is ever forwarded. Messages the model sees are
written by us, keyed off the error's code.

**Bounds.** `max_turns` counts model round-trips, which is not the same as counting work.
One reply carrying five thousand tool calls is five thousand executions inside one turn, and
a tool returning large JSON fills the context long before the turn budget runs out. Each
dimension that can run away needs its own ceiling.
"""

import pytest
from pydantic import BaseModel

from motorooter.clock import FakeClock
from motorooter.llm.agent import Agent, AgentLimits
from motorooter.llm.messages import AssistantMessage, ToolCall, ToolMessage, UserMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.llm.tools import ProgressReport, Tool, ToolArguments, ToolOutcome, ToolRegistry
from motorooter.routing.errors import NoRouteFound, ProviderUnavailable


class Args(ToolArguments):
    text: str = "x"


class Boom(Tool):
    """Raises whatever it is given, to order."""

    name = "boom"
    description = "Fails."
    arguments = Args

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.call_count = 0

    async def run(
        self,
        arguments: Args,
        on_progress: ProgressReport | None = None,
    ) -> ToolOutcome:
        self.call_count += 1
        raise self._error


class Big(Tool):
    name = "big"
    description = "Returns a lot."
    arguments = Args

    def __init__(self, size: int) -> None:
        self._size = size

    async def run(
        self,
        arguments: Args,
        on_progress: ProgressReport | None = None,
    ) -> ToolOutcome:
        return ToolOutcome(content="x" * self._size)


class Ok(Tool):
    name = "ok"
    description = "Works."
    arguments = Args

    def __init__(self) -> None:
        self.call_count = 0

    async def run(
        self,
        arguments: Args,
        on_progress: ProgressReport | None = None,
    ) -> ToolOutcome:
        self.call_count += 1
        return ToolOutcome(content="fine")


def says(text: str) -> AssistantMessage:
    return AssistantMessage(content=text)


def calls(name: str, count: int = 1, arguments: str = '{"text": "x"}') -> AssistantMessage:
    return AssistantMessage(
        content=None,
        tool_calls=tuple(
            ToolCall(id=f"call-{index}", name=name, arguments=arguments) for index in range(count)
        ),
    )


async def collect(agent: Agent):
    return [event async for event in agent.run([UserMessage(content="go")])]


def sent_to_model(client: FakeLlmClient) -> str:
    return " ".join(
        message.content for message in client.conversations[-1] if isinstance(message, ToolMessage)
    )


class TestNothingInternalReachesTheModel:
    """The blocker. Every one of these strings is something an anonymous caller could read."""

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError(2, "No such file or directory: '/home/tim/.config/sa-abc.json'"),
            RuntimeError("connection to https://api.example.com/v1?key=SECRET-123 failed"),
            KeyError("/var/secrets/motorooter/openai"),
            ValueError("bucket motorooter-prod-trips is not accessible"),
        ],
    )
    async def test_the_exception_text_is_not_forwarded(self, error):
        tool = Boom(error)
        client = FakeLlmClient(replies=(calls("boom"), says("Sorry.")))
        await collect(Agent(client, ToolRegistry([tool])))

        forwarded = sent_to_model(client)
        for secret in ("/home/tim", "SECRET-123", "/var/secrets", "motorooter-prod-trips"):
            assert secret not in forwarded

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError(2, "No such file or directory: '/home/tim/.config/sa-abc.json'"),
            RuntimeError("connection to https://api.example.com/v1?key=SECRET-123 failed"),
        ],
    )
    async def test_the_exception_text_does_not_reach_the_ui_either(self, error):
        """`AgentEvent.message` is streamed to the browser; it is the same disclosure."""
        client = FakeLlmClient(replies=(calls("boom"), says("Sorry.")))
        events = await collect(Agent(client, ToolRegistry([Boom(error)])))
        failed = " ".join(event.message for event in events if event.kind == "tool_failed")
        assert "/home/tim" not in failed
        assert "SECRET-123" not in failed

    async def test_the_model_is_still_told_something_actionable(self):
        """Sanitised is not the same as useless — it still has to be able to react."""
        client = FakeLlmClient(replies=(calls("boom"), says("Sorry.")))
        await collect(Agent(client, ToolRegistry([Boom(RuntimeError("x"))])))
        assert "boom" in sent_to_model(client)

    async def test_a_domain_failure_is_forwarded_in_full(self):
        """Domain errors are information the model can act on, so it gets them verbatim.

        The split is deliberate: we author these messages, so their content is a decision
        rather than an accident. Only the `except Exception` path — where the text comes
        from somewhere we do not control — is sanitised.
        """
        client = FakeLlmClient(replies=(calls("boom"), says("Sorry.")))
        await collect(
            Agent(
                client,
                ToolRegistry([Boom(NoRouteFound("no dirt road connects those", provider="ors"))]),
            )
        )
        assert "no dirt road connects those" in sent_to_model(client)

    async def test_a_transient_domain_failure_is_distinguishable(self):
        """ "Widen the search" and "try again later" are different responses."""
        client = FakeLlmClient(replies=(calls("boom"), says("Sorry.")))
        await collect(
            Agent(
                client,
                ToolRegistry([Boom(ProviderUnavailable("upstream returned 503", provider="ors"))]),
            )
        )
        assert "503" in sent_to_model(client)

    async def test_argument_errors_name_the_field_without_pydantic_internals(self):
        """`str(ValidationError)` embeds the offending input value and a docs URL."""
        client = FakeLlmClient(replies=(calls("ok", arguments='{"text": 12345678}'), says("x")))
        await collect(Agent(client, ToolRegistry([Ok()])))
        forwarded = sent_to_model(client)
        assert "text" in forwarded
        assert "errors.pydantic.dev" not in forwarded
        assert "12345678" not in forwarded


class TestWorkIsBoundedNotJustTurns:
    async def test_one_reply_cannot_run_unlimited_tools(self):
        """Five thousand calls in a single message is five thousand executions in one turn."""
        tool = Ok()
        client = FakeLlmClient(replies=(calls("ok", count=500), says("Done.")))
        await collect(Agent(client, ToolRegistry([tool]), limits=AgentLimits(max_tool_calls=10)))
        assert tool.call_count == 10

    async def test_exceeding_the_call_budget_ends_the_run(self):
        client = FakeLlmClient(replies=(calls("ok", count=500),), repeat_last=True)
        events = await collect(
            Agent(client, ToolRegistry([Ok()]), limits=AgentLimits(max_tool_calls=10))
        )
        assert events[-1].kind == "done"
        assert events[-1].truncated is True

    async def test_cumulative_tool_output_is_capped(self):
        """A tool returning large JSON fills the context long before the turns run out."""
        client = FakeLlmClient(replies=(calls("big"), calls("big"), calls("big"), says("Done.")))
        events = await collect(
            Agent(client, ToolRegistry([Big(1000)]), limits=AgentLimits(max_tool_output_chars=1500))
        )
        assert events[-1].truncated is True

    async def test_output_within_the_cap_is_untouched(self):
        client = FakeLlmClient(replies=(calls("big"), says("Done.")))
        await collect(
            Agent(client, ToolRegistry([Big(100)]), limits=AgentLimits(max_tool_output_chars=1000))
        )
        assert len(sent_to_model(client)) == 100

    async def test_a_single_oversized_result_is_truncated_rather_than_dropped(self):
        """Partial evidence beats none; the model is told it was cut."""
        client = FakeLlmClient(replies=(calls("big"), says("Done.")))
        await collect(
            Agent(client, ToolRegistry([Big(10_000)]), limits=AgentLimits(max_result_chars=500))
        )
        forwarded = sent_to_model(client)
        assert len(forwarded) < 10_000
        assert "truncated" in forwarded.lower()

    async def test_a_run_has_a_deadline(self):
        """120 s per turn times eight turns is sixteen minutes for one anonymous request."""
        clock = FakeClock()

        class Slow(Tool):
            name = "ok"
            description = "Takes time."
            arguments = Args

            async def run(
                self,
                arguments: Args,
                on_progress: ProgressReport | None = None,
            ) -> ToolOutcome:
                clock.advance(60.0)
                return ToolOutcome(content="fine")

        client = FakeLlmClient(replies=(calls("ok"),), repeat_last=True)
        events = await collect(
            Agent(
                client,
                ToolRegistry([Slow()]),
                limits=AgentLimits(max_turns=100, max_seconds=180.0),
                clock=clock,
            )
        )
        assert events[-1].truncated is True
        assert client.call_count < 100

    @pytest.mark.parametrize(
        "limits",
        [
            {"max_turns": 0},
            {"max_tool_calls": 0},
            {"max_tool_output_chars": 0},
            {"max_result_chars": 0},
            {"max_seconds": 0},
            {"max_consecutive_tool_failures": 0},
        ],
    )
    def test_a_non_positive_limit_is_refused(self, limits):
        with pytest.raises(ValueError):
            AgentLimits(**limits)


class TestTheTerminalEventSaysWhetherItFinished:
    async def test_a_completed_run_is_not_truncated(self):
        client = FakeLlmClient(replies=(says("Done."),))
        events = await collect(Agent(client, ToolRegistry([Ok()])))
        assert events[-1].kind == "done"
        assert events[-1].truncated is False

    async def test_a_cut_off_run_is_marked_on_the_terminal_event(self):
        """A consumer reading only the last event must be able to tell the two apart."""
        client = FakeLlmClient(replies=(calls("ok"),), repeat_last=True)
        events = await collect(Agent(client, ToolRegistry([Ok()]), limits=AgentLimits(max_turns=2)))
        assert events[-1].kind == "done"
        assert events[-1].truncated is True

    async def test_the_terminal_event_says_why(self):
        client = FakeLlmClient(replies=(calls("ok"),), repeat_last=True)
        events = await collect(Agent(client, ToolRegistry([Ok()]), limits=AgentLimits(max_turns=2)))
        assert events[-1].message != ""


class TestAToolThatKeepsFailing:
    async def test_it_is_dropped_after_repeated_failures(self):
        """A dead upstream should not be retried for the whole run."""
        tool = Boom(RuntimeError("upstream down"))
        client = FakeLlmClient(replies=(calls("boom"),), repeat_last=True)
        await collect(
            Agent(
                client,
                ToolRegistry([tool]),
                limits=AgentLimits(max_turns=10, max_consecutive_tool_failures=2),
            )
        )
        assert tool.call_count == 2

    async def test_the_model_is_told_the_tool_is_unavailable(self):
        client = FakeLlmClient(replies=(calls("boom"),), repeat_last=True)
        await collect(
            Agent(
                client,
                ToolRegistry([Boom(RuntimeError("down"))]),
                limits=AgentLimits(max_turns=6, max_consecutive_tool_failures=2),
            )
        )
        assert "unavailable" in sent_to_model(client).lower()

    async def test_a_success_resets_the_count(self):
        """Intermittent failures must not permanently disable a working tool."""

        class Flaky(Tool):
            name = "flaky"
            description = "Fails then works."
            arguments = Args

            def __init__(self) -> None:
                self.call_count = 0

            async def run(
                self,
                arguments: Args,
                on_progress: ProgressReport | None = None,
            ) -> ToolOutcome:
                self.call_count += 1
                if self.call_count % 2 == 1:
                    msg = "transient"
                    raise RuntimeError(msg)
                return ToolOutcome(content="fine")

        tool = Flaky()
        client = FakeLlmClient(replies=(calls("flaky"),), repeat_last=True)
        await collect(
            Agent(
                client,
                ToolRegistry([tool]),
                limits=AgentLimits(max_turns=6, max_consecutive_tool_failures=2),
            )
        )
        assert tool.call_count == 6

    async def test_the_run_still_ends_cleanly(self):
        client = FakeLlmClient(replies=(calls("boom"), says("I gave up.")))
        events = await collect(Agent(client, ToolRegistry([Boom(RuntimeError("down"))])))
        assert events[-1].kind == "done"


class TestAModelStuckOnAnInventedTool:
    async def test_a_refused_call_does_not_consume_the_turn_budget(self):
        """Otherwise one wrong guess burns the whole run before any real work happens."""
        tool = Ok()
        client = FakeLlmClient(
            replies=(calls("teleport"), calls("teleport"), calls("ok"), says("Done."))
        )
        await collect(Agent(client, ToolRegistry([tool]), limits=AgentLimits(max_turns=3)))
        assert tool.call_count == 1

    async def test_but_it_cannot_loop_forever_on_one(self):
        client = FakeLlmClient(replies=(calls("teleport"),), repeat_last=True)
        events = await collect(
            Agent(client, ToolRegistry([Ok()]), limits=AgentLimits(max_refused_turns=2))
        )
        assert events[-1].kind == "done"
        assert client.call_count <= 4


class TestArgumentsAreStrict:
    async def test_an_unrecognised_field_is_refused_rather_than_ignored(self):
        """Pydantic ignores extras by default, so `radius` against `radius_m` silently
        becomes the default: the model asked for 50 km, got 5, and was told nothing."""

        class Search(ToolArguments):
            query: str
            radius_m: int = 5000

        class SearchTool(Tool):
            name = "search"
            description = "Search."
            arguments = Search

            def __init__(self) -> None:
                self.seen: list[Search] = []

            async def run(
                self,
                arguments: Search,
                on_progress: ProgressReport | None = None,
            ) -> ToolOutcome:
                self.seen.append(arguments)
                return ToolOutcome(content="ok")

        tool = SearchTool()
        client = FakeLlmClient(
            replies=(
                AssistantMessage(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="a",
                            name="search",
                            arguments='{"query": "cafes", "radius": 50000}',
                        ),
                    ),
                ),
                says("Done."),
            )
        )
        await collect(Agent(client, ToolRegistry([tool])))
        assert tool.seen == []
        assert "radius" in sent_to_model(client)

    def test_the_published_schema_forbids_extra_fields(self):
        """The model should be told the rule, not just corrected after breaking it."""

        class Search(ToolArguments):
            query: str

        class SearchTool(Tool):
            name = "search"
            description = "Search."
            arguments = Search

            async def run(
                self,
                arguments: Search,
                on_progress: ProgressReport | None = None,
            ) -> ToolOutcome:
                return ToolOutcome(content="ok")

        spec = ToolRegistry([SearchTool()]).specs()[0]
        assert spec["parameters"]["additionalProperties"] is False

    def test_a_tool_whose_arguments_allow_extras_is_refused_at_startup(self):
        """A tool author who forgets the base class should not find out in production."""

        class Loose(BaseModel):
            query: str

        class LooseTool(Tool):
            name = "loose"
            description = "Loose."
            arguments = Loose

            async def run(
                self,
                arguments: Loose,
                on_progress: ProgressReport | None = None,
            ) -> ToolOutcome:
                return ToolOutcome(content="ok")

        with pytest.raises(Exception, match="extra"):
            ToolRegistry([LooseTool()])


class TestPathologicalArguments:
    async def test_deeply_nested_json_does_not_abort_the_run(self):
        """`json.loads` raises RecursionError, which is not a ValueError, so it escaped."""
        nested = "[" * 60_000 + "]" * 60_000
        client = FakeLlmClient(replies=(calls("ok", arguments=nested), says("Sorry.")))
        events = await collect(Agent(client, ToolRegistry([Ok()])))
        assert events[-1].kind == "done"
        assert any(event.kind == "tool_failed" for event in events)
