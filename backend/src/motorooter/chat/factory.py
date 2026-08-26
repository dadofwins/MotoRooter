"""Building the chat model from configuration.

Separate from the discovery factory but reading the same settings, because they need the
same OpenAI credential and the same pinned model. Which model answers is a deploy decision:
there is no inline default anywhere on this path.
"""

from motorooter.llm.protocol import LlmClient
from motorooter.llm.providers.openai import OpenAiClient
from motorooter.planning.discovery.factory import DiscoverySettings

CHAT_TIMEOUT_S = 120.0
"""The long default, and here it is the right one.

Extraction and judging got their own shorter budgets because they are mechanical calls where
a stall costs an anchor. A chat turn is the opposite: the rider is watching, the model may be
midway through a tool sequence, and abandoning it at twenty seconds throws away work they are
waiting for. Nothing else is queued behind it.
"""


def build_chat_model(settings: DiscoverySettings) -> LlmClient | None:
    """The pinned chat model, or `None` when there is no key.

    `None` disables one endpoint rather than the deployment, which is the same choice
    discovery makes for the same reason.
    """
    if not settings.openai_api_key:
        return None
    return OpenAiClient(
        api_key=settings.openai_api_key,
        model=settings.model,
        timeout_s=CHAT_TIMEOUT_S,
    )
