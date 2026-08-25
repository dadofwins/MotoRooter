"""OpenAI adapter, driven entirely by recorded response shapes. Never hits the network."""

from typing import Any

import httpx
import pytest
import respx

from motorooter.llm.errors import LlmError, LlmQuotaExceeded, LlmRefused, LlmUnavailable
from motorooter.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from motorooter.llm.providers.openai import OPENAI_BASE_URL, OpenAiClient

COMPLETIONS_URL = f"{OPENAI_BASE_URL}/chat/completions"

ECHO_SPEC = {
    "name": "echo",
    "description": "Repeat text.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
}


def reply(content: str | None = "Hello.", tool_calls: list[dict[str, Any]] | None = None):
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def tool_call(name: str = "echo", arguments: str = '{"text": "hi"}', call_id: str = "call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.fixture
def mock_openai():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def build_client(**overrides: Any) -> OpenAiClient:
    kwargs: dict[str, Any] = {"api_key": "sk-test", "model": "gpt-5-mini"}
    return OpenAiClient(**(kwargs | overrides))


class TestSendingTheConversation:
    async def test_the_pinned_model_is_sent(self, mock_openai):
        route = mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply())
        )
        await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert route.calls.last.request.read().decode().count('"gpt-5-mini"') == 1

    def test_the_model_is_reported(self):
        assert build_client(model="gpt-5-mini").model == "gpt-5-mini"

    async def test_the_api_key_is_sent_as_a_bearer_token(self, mock_openai):
        route = mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply())
        )
        await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"

    async def test_every_role_survives_translation(self, mock_openai):
        """A tool result that loses its role or id makes the provider reject the request."""
        route = mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply())
        )
        await build_client().complete(
            [
                SystemMessage(content="be helpful"),
                UserMessage(content="hi"),
                AssistantMessage(
                    content=None,
                    tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"text": "hi"}'),),
                ),
                ToolMessage(call_id="call-1", content="echoed: hi"),
            ],
            [ECHO_SPEC],
        )
        sent = route.calls.last.request.read().decode()
        assert '"system"' in sent
        assert '"tool"' in sent
        assert '"call-1"' in sent

    async def test_tools_are_declared_in_the_shape_the_api_expects(self, mock_openai):
        route = mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply())
        )
        await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        sent = route.calls.last.request.read().decode()
        assert '"type": "function"' in sent or '"type":"function"' in sent

    async def test_no_tools_is_sent_as_no_tools_field(self, mock_openai):
        """An empty `tools` array is rejected by the API; omitting it is the correct form."""
        route = mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply())
        )
        await build_client().complete([UserMessage(content="hi")], [])
        assert '"tools"' not in route.calls.last.request.read().decode()


class TestReadingTheReply:
    async def test_plain_text_comes_back_as_content(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply("Sure."))
        )
        answer = await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert answer.content == "Sure."

    async def test_tool_calls_are_translated(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply(None, [tool_call()]))
        )
        answer = await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert [(call.id, call.name) for call in answer.tool_calls] == [("call-1", "echo")]

    async def test_tool_arguments_stay_a_raw_string(self, mock_openai):
        """They are model output and may be malformed; parsing belongs to the tool layer."""
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply(None, [tool_call(arguments="{not json")]))
        )
        answer = await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert answer.tool_calls[0].arguments == "{not json"

    async def test_several_tool_calls_in_one_reply(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(
                200, json=reply(None, [tool_call(call_id="a"), tool_call(call_id="b")])
            )
        )
        answer = await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert len(answer.tool_calls) == 2

    async def test_content_and_tool_calls_together(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply("Looking now.", [tool_call()]))
        )
        answer = await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])
        assert answer.content == "Looking now."
        assert len(answer.tool_calls) == 1


MALFORMED_BODIES: list[Any] = [
    {"unexpected": True},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]},
    {"choices": [{"message": {"content": "x", "tool_calls": [{"id": 12345}]}}]},
    {"choices": [{"message": {"content": "x", "tool_calls": [{"id": "a"}]}}]},
    {"choices": [{"message": {"content": "x", "tool_calls": "not-a-list"}}]},
    {"choices": "not-a-list"},
    [],
    "a bare string",
    None,
]


class TestNothingButAnLlmErrorEscapes:
    """The invariant, rather than the exceptions I happened to think of.

    The previous version of this listed `httpx` errors and `KeyError`, which is a list of
    guesses. Two real escapes were sitting outside it: `content` arriving as a list of parts,
    and a numeric tool-call id — both raise `pydantic.ValidationError` from the model
    constructors, and both surfaced as a 500 `internal_error` instead of a 502.
    """

    @pytest.mark.parametrize("body", MALFORMED_BODIES)
    async def test_a_malformed_body_raises_only_an_llm_error(self, mock_openai, body):
        mock_openai.post(COMPLETIONS_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(LlmError):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    @pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 409, 418, 429, 500, 503])
    async def test_any_status_with_a_junk_body_raises_only_an_llm_error(self, mock_openai, status):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(status, content=b"\x00\x01 not text")
        )
        with pytest.raises(LlmError):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("no route"),
            httpx.ReadTimeout("slow"),
            httpx.RemoteProtocolError("bad framing"),
            httpx.TooManyRedirects("looping"),
        ],
    )
    async def test_any_transport_error_raises_only_an_llm_error(self, mock_openai, error):
        mock_openai.post(COMPLETIONS_URL).mock(side_effect=error)
        with pytest.raises(LlmError):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])


class TestFailureTranslation:
    """Beyond the invariant: the *right* LlmError, since callers act on the difference."""

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_upstream_failures_are_unavailable(self, mock_openai, status):
        mock_openai.post(COMPLETIONS_URL).mock(return_value=httpx.Response(status))
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_rate_limiting_is_quota(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(LlmQuotaExceeded):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_client_errors_are_refusals(self, mock_openai, status):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(status, json={"error": {"message": "bad key"}})
        )
        with pytest.raises(LlmRefused):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_the_refusal_carries_the_upstream_message(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
        )
        with pytest.raises(LlmRefused, match="invalid api key"):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_a_transport_error_is_unavailable(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_a_non_json_body_is_unavailable(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, content=b"<html>proxy error</html>")
        )
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_an_unrecognised_reply_shape_is_unavailable(self, mock_openai):
        """A shape change upstream must not surface as a KeyError from inside the adapter."""
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_a_reply_with_no_choices_is_unavailable(self, mock_openai):
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])

    async def test_a_malformed_tool_call_entry_is_unavailable(self, mock_openai):
        """Missing `function` is a protocol violation, not a model mistake to hand back."""
        mock_openai.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=reply(None, [{"id": "a", "type": "function"}]))
        )
        with pytest.raises(LlmUnavailable):
            await build_client().complete([UserMessage(content="hi")], [ECHO_SPEC])


def test_it_satisfies_the_protocol():
    from motorooter.llm.protocol import LlmClient

    assert isinstance(build_client(), LlmClient)
