"""Scripted LLM client.

Ships in `src` rather than `tests` because it is a supported seam: the agent tests, local
development without an OpenAI key, and `MOTOROOTER_OFFLINE=1` all depend on it.

It exists to make a *misbehaving* model testable. A real model will not reliably invent a
tool name, emit malformed arguments, or refuse to stop calling tools when asked — and those
are exactly the cases the loop has to survive.
"""

from collections.abc import Sequence
from typing import Any

from motorooter.llm.errors import LlmError
from motorooter.llm.messages import AssistantMessage, Message

FAKE_MODEL = "fake-model"


class FakeLlmClient:
    """Returns a scripted reply per turn, recording what it was sent."""

    def __init__(
        self,
        *,
        replies: Sequence[AssistantMessage] = (),
        repeat_last: bool = False,
        error: LlmError | None = None,
        model: str = FAKE_MODEL,
    ) -> None:
        """
        Args:
            replies: one reply per turn, in order.
            repeat_last: keep returning the final reply instead of running out. Lets a test
                script a model that never stops calling tools.
            error: raised instead of replying, for failure-path tests.
            model: reported as the pinned model.
        """
        self._replies = list(replies)
        self._repeat_last = repeat_last
        self._error = error
        self._model = model
        self.conversations: list[list[Message]] = []
        """Every conversation sent, so tests can assert what the model was told."""

        self.tool_specs: list[Sequence[dict[str, Any]]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return len(self.conversations)

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]
    ) -> AssistantMessage:
        self.conversations.append(list(messages))
        self.tool_specs.append(list(tools))

        if self._error is not None:
            raise self._error

        index = self.call_count - 1
        if index < len(self._replies):
            return self._replies[index]
        if self._repeat_last and self._replies:
            return self._replies[-1]
        # Running dry means the loop asked for more turns than the test scripted, which is
        # usually the test being wrong. Answer rather than hang, so the assertion fails
        # somewhere legible.
        return AssistantMessage(content="(the fake client ran out of scripted replies)")
