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

from collections.abc import Sequence
from typing import Any

import httpx

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
    ) -> None:
        """
        Args:
            api_key: OpenAI key. Server-side only; the browser never sees it.
            model: pinned identifier, e.g. `gpt-5-mini`. No default, deliberately.
            base_url: override for a compatible endpoint or a recording proxy.
            client: injectable HTTP client, so callers can share a connection pool.
            timeout_s: generous — a tool-calling turn over a long conversation is slow, and
                a timeout here abandons work the user is waiting on.
        """
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s

    @property
    def model(self) -> str:
        return self._model

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

        response = await self._post(payload)
        self._raise_for_status(response)
        return self._decode(response)

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
        except (TypeError, KeyError, IndexError, AttributeError) as exc:
            # A shape change upstream is an availability problem, not a caller error, and
            # must not escape as a KeyError from inside an adapter.
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
