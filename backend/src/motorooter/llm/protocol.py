"""The one interface every LLM provider implements.

Narrow on purpose, exactly like `RoutingProvider`: one method. That is what makes
`FakeLlmClient` trivial — and therefore the agent loop's behaviour testable against a model
that misbehaves on demand, which a real one will not do to order.
"""

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from motorooter.llm.messages import AssistantMessage, Message


@runtime_checkable
class LlmClient(Protocol):
    """A chat model that can call tools.

    Implementations must raise only `LlmError` subclasses, so callers never catch a
    vendor-specific exception.
    """

    @property
    def model(self) -> str:
        """The pinned model identifier, for logging and for reporting what answered."""
        ...

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]
    ) -> AssistantMessage:
        """One turn: send the conversation, get back text, tool calls, or both.

        Raises:
            LlmUnavailable: transient upstream failure.
            LlmQuotaExceeded: rate limit or spend cap.
            LlmRefused: bad credentials, filtered content, unknown model.
        """
        ...
