"""Types the blurb needs from its caller."""

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class Turn:
    """One exchange from the rail, as the client recorded it.

    Its own type rather than `llm.messages.Message`, so a tool call or a system prompt
    cannot arrive here by accident: the blurb wants what the rider and the assistant said
    to each other and nothing else. The client owns the transcript — nothing about a
    conversation is held server-side.
    """

    role: Literal["user", "assistant"]
    content: str
