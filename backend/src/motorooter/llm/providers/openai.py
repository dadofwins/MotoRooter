"""OpenAI chat-completions adapter.

Spoken over `httpx`, which the project already depends on, rather than the `openai` SDK. The
surface used here is one endpoint, staying on httpx keeps the adapter testable with `respx`
exactly like the ORS, Google and Cloud Storage adapters, and the SDK's value is mostly in the
parts this layer deliberately does not use — retries and streaming helpers that belong in
decorators, not inside a provider.

The model is a constructor argument with no default. Which model answers is a deploy
decision, and a default here would be one silently inherited by anything that forgot to
choose.
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from pydantic import ValidationError

from motorooter.llm.errors import LlmQuotaExceeded, LlmRefused, LlmUnavailable
from motorooter.llm.messages import (
    AssistantMessage,
    Message,
    ToolCall,
)

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAiClient:
    """Chat completions with tool calling."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = OPENAI_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 120.0,
        reasoning_effort: str | None = None,
    ) -> None:
        """
        Args:
            api_key: OpenAI key. Server-side only; the browser never sees it.
            model: pinned identifier, e.g. `gpt-5-mini`. No default, deliberately.
            base_url: override for a compatible endpoint or a recording proxy.
            client: injectable HTTP client, so callers can share a connection pool.
            timeout_s: generous — a tool-calling turn over a long conversation is slow, and
                a timeout here abandons work the user is waiting on.
            reasoning_effort: how hard the model should think, or `None` to let it decide.
                Worth setting low for mechanical work: extracting place names from search
                snippets measured 35-44s at the default and 2.9-3.4s at `minimal`, for the
                same answers. Left unset the payload omits it entirely, so a model with no
                such parameter is unaffected.
        """
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s
        self._reasoning_effort = reasoning_effort

    @property
    def model(self) -> str:
        return self._model

    @property
    def reasoning_effort(self) -> str | None:
        """Readable so wiring can be asserted. A setting that silently fails to apply is
        this project's recurring bug, and a private attribute makes it untestable."""
        return self._reasoning_effort

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._encode(message) for message in messages],
        }
        if tools:
            # Omitted rather than sent empty: the API rejects `"tools": []`.
            payload["tools"] = [{"type": "function", "function": spec} for spec in tools]
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort

        response = await self._post(payload)
        self._raise_for_status(response)
        return self._decode(response)

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """Content as it arrives, piece by piece.

        Content only, deliberately. Tool calls arrive split across chunks with their
        arguments in fragments, and reassembling them is a second problem with its own
        failure modes; the caller that needs this is the judge, whose reply is JSON and
        whose turn has no tools. `complete` remains the way to run a tool-calling turn.

        The point is progress. Scoring forty places is one call that takes half a minute, and
        without this there is nothing to report from — "scoring 41 places" sits still because
        the request is atomic, not because the work is.

        Raises: the same `LlmError` subclasses as `complete`, so a caller handles one
        hierarchy whichever it used.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._encode(message) for message in messages],
            "stream": True,
        }
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort

        async for line in self._stream_lines(payload):
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            piece = _content_of(data)
            if piece:
                yield piece

    async def _stream_lines(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Server-sent event lines, with failures translated before the first yield."""
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            async with client.stream(
                "POST", url, json=payload, headers=headers, timeout=self._timeout_s
            ) as response:
                if not response.is_success:
                    # The body has to be read before it can be inspected, and a streaming
                    # response has not been.
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    yield line
        except httpx.HTTPError as exc:
            msg = f"request to OpenAI failed: {exc}"
            raise LlmUnavailable(msg) from exc
        finally:
            if self._client is None:
                await client.aclose()

    # -- request ------------------------------------------------------------------------

    @staticmethod
    def _encode(message: Message) -> dict[str, Any]:
        """Domain message to wire shape. Nothing OpenAI-shaped exists outside this module."""
        if isinstance(message, AssistantMessage):
            encoded: dict[str, Any] = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                encoded["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.tool_calls
                ]
            return encoded

        if message.role == "tool":
            # `tool_call_id` is what links a result to its call. Without it the provider
            # rejects the whole conversation, which surfaces far from the cause.
            return {
                "role": "tool",
                "tool_call_id": message.call_id,
                "content": message.content,
            }

        return {"role": message.role, "content": message.content}

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                return await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"request to OpenAI failed: {exc}"
            raise LlmUnavailable(msg) from exc

    # -- response -----------------------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        detail = self._error_message(response)

        if status == 429:
            msg = f"OpenAI rate limit or spend cap reached: {detail}"
            raise LlmQuotaExceeded(msg)
        if status >= 500:
            msg = f"OpenAI returned HTTP {status}: {detail}"
            raise LlmUnavailable(msg)
        # 400-level: a bad key, an unknown model, filtered content. Retrying will not help,
        # and the upstream message is the only thing that says which it is.
        msg = f"OpenAI refused the request (HTTP {status}): {detail}"
        raise LlmRefused(msg)

    def _decode(self, response: httpx.Response) -> AssistantMessage:
        try:
            body = response.json()
        except ValueError as exc:
            msg = "OpenAI returned a body that was not JSON"
            raise LlmUnavailable(msg) from exc

        try:
            message = body["choices"][0]["message"]
            return AssistantMessage(
                content=message.get("content"),
                tool_calls=tuple(self._decode_call(call) for call in message.get("tool_calls", [])),
            )
        except (TypeError, KeyError, IndexError, AttributeError, ValidationError) as exc:
            # A shape change upstream is an availability problem, not a caller error, and
            # must not escape as a raw exception from inside an adapter. `ValidationError`
            # belongs here too: `content` arriving as a list of parts, or a numeric
            # tool-call id from a compatible endpoint, both fail model construction rather
            # than dict access.
            msg = f"unrecognized OpenAI response shape: {exc}"
            raise LlmUnavailable(msg) from exc

    @staticmethod
    def _decode_call(call: dict[str, Any]) -> ToolCall:
        function = call["function"]
        # `arguments` stays a raw string. It is model output, so it may be malformed or of
        # the wrong shape; both are the tool layer's to report back, not this module's.
        return ToolCall(
            id=call["id"], name=function["name"], arguments=function.get("arguments", "")
        )

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message", error))
        return str(error or "")[:200]


def _content_of(data: str) -> str | None:
    """The content delta in one frame, or `None` if there is not one.

    A malformed frame costs that frame. Losing a whole scoring run to one truncated line
    would be the expensive reading of a cheap problem, and the caller is reassembling text
    where a gap is visible rather than silent.
    """
    try:
        body = json.loads(data)
    except ValueError:
        return None
    try:
        delta = body["choices"][0]["delta"]
    except (TypeError, KeyError, IndexError):
        return None
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) and content else None
