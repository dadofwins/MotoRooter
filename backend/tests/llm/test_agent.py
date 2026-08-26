"""The tool-calling loop.

Everything here runs against a scripted fake client. No test reaches OpenAI, and the loop's
behaviour is fully determined by what the fake is told to return — which is the only way the
interesting cases (a model that loops, a model that invents a tool, a model that sends
malformed arguments) can be tested at all, since a real model produces them unpredictably.

The loop's job is not to be clever. It is to keep a wrong or hostile model from turning into
a wrong or expensive application: bounded turns, unknown tools refused, bad arguments handed
back rather than crashed on, and a tool failure reported to the model instead of ending the
conversation.
"""

import pytest

from motorooter.llm.agent import Agent, AgentEvent, AgentLimits
from motorooter.llm.errors import ToolCallFailed
from motorooter.llm.messages import AssistantMessage, ToolCall, ToolMessage, UserMessage
from motorooter.llm.providers.fake import FakeLlmClient
from motorooter.llm.tools import Tool, ToolArguments, ToolOutcome, ToolRegistry


class EchoArgs(ToolArguments):
    text: str


class EchoTool(Tool):
    """A tool that succeeds, recording what it was asked."""

    name = "echo"
    description = "Repeat the given text."
    arguments = EchoArgs

    def __init__(self) -> None:
        self.calls: list[EchoArgs] = []

    async def run(self, arguments: EchoArgs) -> ToolOutcome:
        self.calls.append(arguments)
        return ToolOutcome(content=f"echoed: {arguments.text}")


class ExplodingTool(Tool):
    name = "explode"
    description = "Always fails."
    arguments = EchoArgs

    async def run(self, arguments: EchoArgs) -> ToolOutcome:
        msg = "the upstream service is down"
        raise RuntimeError(msg)


def says(text: str) -> AssistantMessage:
    return AssistantMessage(content=text)


def calls(name: str, arguments: str, *, call_id: str = "call-1") -> AssistantMessage:
    return AssistantMessage(
        content=None, tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),)
    )


@pytest.fixture
def echo():
    return EchoTool()


@pytest.fixture
def registry(echo):
    return ToolRegistry([echo])


def agent_for(*replies: AssistantMessage, registry: ToolRegistry, **kwargs) -> Agent:
    return Agent(FakeLlmClient(replies=replies), registry, **kwargs)


async def collect(agent: Agent, prompt: str = "hello") -> list[AgentEvent]:
    return [event async for event in agent.run([UserMessage(content=prompt)])]


class TestAPlainAnswer:
    async def test_a_reply_with_no_tool_calls_ends_the_loop(self, registry):
        events = await collect(agent_for(says("Sure."), registry=registry))
        assert [event.kind for event in events] == ["message", "done"]

    async def test_the_assistant_text_is_surfaced(self, registry):
        events = await collect(agent_for(says("Sure."), registry=registry))
        assert events[0].message == "Sure."

    async def test_it_does_not_call_the_model_again(self, registry):
        client = FakeLlmClient(replies=(says("Sure."),))
        await collect(Agent(client, registry))
        assert client.call_count == 1


class TestCallingATool:
    async def test_the_tool_receives_parsed_arguments(self, echo, registry):
        await collect(agent_for(calls("echo", '{"text": "hi"}'), says("Done."), registry=registry))
        assert [call.text for call in echo.calls] == ["hi"]

    async def test_the_result_goes_back_to_the_model(self, echo, registry):
        client = FakeLlmClient(replies=(calls("echo", '{"text": "hi"}'), says("Done.")))
        await collect(Agent(client, registry))
        sent = client.conversations[-1]
        assert any("echoed: hi" in str(getattr(message, "content", "")) for message in sent)

    async def test_each_tool_call_is_announced_before_it_runs(self, registry):
        events = await collect(
            agent_for(calls("echo", '{"text": "hi"}'), says("Done."), registry=registry)
        )
        kinds = [event.kind for event in events]
        assert kinds.index("tool_started") < kinds.index("tool_finished")

    async def test_the_loop_continues_until_the_model_stops_calling_tools(self, echo, registry):
        await collect(
            agent_for(
                calls("echo", '{"text": "one"}'),
                calls("echo", '{"text": "two"}'),
                says("Done."),
                registry=registry,
            )
        )
        assert [call.text for call in echo.calls] == ["one", "two"]

    async def test_several_calls_in_one_reply_all_run(self, echo, registry):
        parallel = AssistantMessage(
            content=None,
            tool_calls=(
                ToolCall(id="a", name="echo", arguments='{"text": "one"}'),
                ToolCall(id="b", name="echo", arguments='{"text": "two"}'),
            ),
        )
        await collect(agent_for(parallel, says("Done."), registry=registry))
        assert [call.text for call in echo.calls] == ["one", "two"]

    async def test_a_tool_result_carries_its_call_id_back(self, registry):
        """Mismatched ids make the model answer about the wrong call, silently."""
        client = FakeLlmClient(
            replies=(calls("echo", '{"text": "hi"}', call_id="call-xyz"), says("Done."))
        )
        await collect(Agent(client, registry))
        tool_messages = [m for m in client.conversations[-1] if isinstance(m, ToolMessage)]
        assert [m.call_id for m in tool_messages] == ["call-xyz"]


class TestWhatTheUiSees:
    async def test_a_tool_can_surface_structured_progress(self, registry):
        """Discovery has to fill the map as it works, not after."""

        class FindsThings(Tool):
            name = "find"
            description = "Finds things."
            arguments = EchoArgs

            async def run(self, arguments: EchoArgs) -> ToolOutcome:
                return ToolOutcome(content="found 2", found=2)

        events = await collect(
            agent_for(
                calls("find", '{"text": "x"}'),
                says("Done."),
                registry=ToolRegistry([FindsThings()]),
            )
        )
        finished = next(event for event in events if event.kind == "tool_finished")
        assert finished.outcome is not None
        assert finished.outcome.found == 2

    async def test_the_final_event_is_always_done(self, registry):
        events = await collect(
            agent_for(calls("echo", '{"text": "hi"}'), says("Done."), registry=registry)
        )
        assert events[-1].kind == "done"


class TestAMisbehavingModel:
    """The model is untrusted input. None of these may reach the caller as an exception."""

    async def test_an_unknown_tool_is_reported_back_rather_than_raised(self, registry):
        client = FakeLlmClient(replies=(calls("teleport", "{}"), says("Sorry.")))
        events = await collect(Agent(client, registry))
        assert events[-1].kind == "done"
        assert any(event.kind == "tool_failed" for event in events)

    async def test_the_unknown_tool_error_names_what_is_available(self, registry):
        """A model told only "no" repeats itself; one told the menu can correct."""
        client = FakeLlmClient(replies=(calls("teleport", "{}"), says("Sorry.")))
        await collect(Agent(client, registry))
        sent = str(client.conversations[-1])
        assert "echo" in sent

    async def test_malformed_arguments_are_handed_back_not_crashed_on(self, registry):
        client = FakeLlmClient(replies=(calls("echo", "{not json"), says("Sorry.")))
        events = await collect(Agent(client, registry))
        assert events[-1].kind == "done"
        assert any(event.kind == "tool_failed" for event in events)

    async def test_arguments_failing_validation_are_handed_back(self, echo, registry):
        """Wrong shape, not wrong syntax: the schema is the thing being enforced."""
        client = FakeLlmClient(replies=(calls("echo", '{"wrong_field": 1}'), says("Sorry.")))
        await collect(Agent(client, registry))
        assert echo.calls == []

    async def test_the_validation_error_is_shown_to_the_model(self, registry):
        client = FakeLlmClient(replies=(calls("echo", '{"wrong_field": 1}'), says("Sorry.")))
        await collect(Agent(client, registry))
        assert "text" in str(client.conversations[-1])

    async def test_a_model_that_never_stops_is_cut_off(self, registry):
        """Otherwise one chat turn bills forever. Cost is not the design driver; a runaway
        loop is still a bug, and this is the only place that can stop it."""
        looping = FakeLlmClient(replies=(calls("echo", '{"text": "again"}'),), repeat_last=True)
        events = await collect(Agent(looping, registry, limits=AgentLimits(max_turns=3)))
        assert looping.call_count == 3
        assert events[-1].kind == "done"

    async def test_being_cut_off_is_visible_rather_than_silent(self, registry):
        looping = FakeLlmClient(replies=(calls("echo", '{"text": "again"}'),), repeat_last=True)
        events = await collect(Agent(looping, registry, limits=AgentLimits(max_turns=2)))
        assert events[-1].truncated is True


class TestAFailingTool:
    async def test_an_unexpected_failure_is_reported_without_its_text(self, registry):
        """The tool is named so the model can route around it; the exception is not.

        `ExplodingTool` raises a plain RuntimeError, which is the path where the message
        came from somewhere we do not control. See test_agent_limits.py for why that
        matters on an unauthenticated surface.
        """
        client = FakeLlmClient(replies=(calls("explode", '{"text": "x"}'), says("Sorry.")))
        await collect(Agent(client, ToolRegistry([ExplodingTool()])))
        conversation = str(client.conversations[-1])
        assert "explode" in conversation
        assert "the upstream service is down" not in conversation

    async def test_the_conversation_continues(self, registry):
        client = FakeLlmClient(replies=(calls("explode", '{"text": "x"}'), says("Recovered.")))
        events = await collect(Agent(client, ToolRegistry([ExplodingTool()])))
        assert events[-1].kind == "done"
        assert any(event.message == "Recovered." for event in events if event.kind == "message")

    async def test_a_failure_event_is_emitted_for_the_ui(self, registry):
        client = FakeLlmClient(replies=(calls("explode", '{"text": "x"}'), says("Sorry.")))
        events = await collect(Agent(client, ToolRegistry([ExplodingTool()])))
        failed = next(event for event in events if event.kind == "tool_failed")
        assert failed.tool == "explode"

    async def test_cancellation_is_not_swallowed(self, registry):
        class Cancels(Tool):
            name = "echo"
            description = "Cancels."
            arguments = EchoArgs

            async def run(self, arguments: EchoArgs) -> ToolOutcome:
                raise KeyboardInterrupt

        client = FakeLlmClient(replies=(calls("echo", '{"text": "x"}'), says("Done.")))
        with pytest.raises(KeyboardInterrupt):
            await collect(Agent(client, ToolRegistry([Cancels()])))


class TestTheRegistry:
    def test_it_exposes_specs_for_the_model(self, registry):
        specs = registry.specs()
        assert [spec["name"] for spec in specs] == ["echo"]

    def test_a_spec_carries_the_json_schema_of_its_arguments(self, registry):
        spec = registry.specs()[0]
        assert "text" in spec["parameters"]["properties"]

    def test_a_spec_carries_the_description_the_model_reads(self, registry):
        assert registry.specs()[0]["description"] == "Repeat the given text."

    def test_duplicate_tool_names_are_refused(self, echo):
        """Registration order would silently decide which one the model reaches."""
        with pytest.raises(ToolCallFailed):
            ToolRegistry([echo, EchoTool()])

    def test_an_empty_registry_is_refused(self):
        """An agent with no tools is a chat box, and silently so."""
        with pytest.raises(ToolCallFailed):
            ToolRegistry([])


class TestABudgetSizedForRealWork:
    """`max_turns` was set before any tool existed, and its docstring said so: "enough for
    search, resolve, judge and a summary". That describes a discovery run.

    Measured against a real model on Tim's own request — a three-day trip from Woodinville —
    the assistant added seven waypoints one per turn and was cut off mid-route with
    "stopped after 8 turns without finishing". Building a multi-day route is legitimately a
    dozen additions plus reads plus a summary, so the ceiling was not protecting against
    runaway work; it was stopping ordinary work.

    `max_tool_calls` still bounds the total, which is the limit that actually guards spend.
    """

    async def test_a_dozen_tool_calls_can_finish(self, registry):
        replies = [
            AssistantMessage(
                content=None,
                tool_calls=(ToolCall(id=f"c{i}", name="echo", arguments='{"text": "x"}'),),
            )
            for i in range(12)
        ]
        replies.append(AssistantMessage(content="Done."))
        events = await collect(agent_for(*replies, registry=registry))
        assert events[-1].kind == "done"
        assert events[-1].truncated is False, "a route-sized run must not be cut off"

    def test_the_ceiling_is_above_a_realistic_route(self):
        """Seven waypoints was not a runaway; it was a short trip."""
        assert AgentLimits().max_turns >= 15
