"""Assembling the discovery pipeline from settings.

Same shape as `routing.factory` and `trips.factory`: the single place that names concrete
adapters, so everything downstream depends only on `DiscoveryPipeline`.

Unlike routing, a missing credential yields `None` rather than raising. Discovery needs four
separate API keys, and a backend that refused to boot without all of them would take every
other endpoint down for want of a feature most requests never touch. The replan endpoint
reports the absence as a 501, which is what the frontend already distinguishes; the rest of
the app runs.
"""

import dataclasses
import os

import httpx

from motorooter.llm.protocol import LlmClient
from motorooter.llm.providers.openai import OpenAiClient
from motorooter.planning.discovery.category import CategoryClassifier
from motorooter.planning.discovery.extract import PlaceExtractor
from motorooter.planning.discovery.judge import CandidateJudge
from motorooter.planning.discovery.naming import PlaceNamer
from motorooter.planning.discovery.pipeline import DiscoveryPipeline
from motorooter.planning.discovery.resolve import PlacesResolver
from motorooter.planning.discovery.sources.brave import BraveSearchSource

EXTRACT_TIMEOUT_S = 25.0
"""How long a structured extraction may take before it is abandoned.

Extraction reads a handful of snippets and returns a short list, so it has no business
taking the 120 s a chat turn is allowed — one stalled call was holding an anchor for two
minutes.

Twenty-five rather than the single digits that were proposed, because eight was measured
against the live API and timed out on *ordinary* calls: a batch of ten snippets through
gpt-5-mini does not reliably return in under ten seconds, and a limit below normal latency
does not fail fast, it fails always. What actually bounds the damage is that a timeout now
costs one anchor's leads instead of the run.

Chat keeps the long default, which is why this is separate rather than lowered globally.
"""

JUDGE_TIMEOUT_S = 45.0
"""Longer than extraction: scoring reasons over a whole corridor is a real piece of work,
and losing it loses everything the run has already paid for."""

DEFAULT_MODEL = "gpt-5-mini"
"""Pinned here rather than inline, so which model answers is a deploy decision.

A starting point rather than a conclusion: discovery scoring is judgement-heavy, which is
where model choice actually shows, and it is worth comparing on a real corridor.
"""


@dataclasses.dataclass(frozen=True)
class DiscoverySettings:
    """Everything discovery needs. Any missing key disables the feature, not the app."""

    brave_api_key: str | None = None
    openai_api_key: str | None = None
    places_api_key: str | None = None
    model: str = DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        return bool(self.brave_api_key and self.openai_api_key and self.places_api_key)


def settings_from_env() -> DiscoverySettings:
    """Read discovery config from the environment.

    `MOTOROOTER_OFFLINE=1` disables it outright: discovery is nothing but external services,
    so there is no meaningful offline version of it.
    """
    if os.environ.get("MOTOROOTER_OFFLINE") == "1":
        return DiscoverySettings()
    return DiscoverySettings(
        brave_api_key=os.environ.get("BRAVE_SEARCH_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        # The same key Places resolution uses. Currently also the browser key; see the note
        # in `resolve` about what happens when that gets referrer-restricted.
        places_api_key=os.environ.get("GOOGLE_MAPS_SERVER_KEY") or None,
        model=os.environ.get("MOTOROOTER_LLM_MODEL") or DEFAULT_MODEL,
    )


def build_discovery(settings: DiscoverySettings) -> DiscoveryPipeline | None:
    """The pipeline, or `None` if it cannot be built.

    One shared HTTP client across every adapter: a discovery run makes dozens of requests to
    four hosts, and a client per request would make that dozens of TLS handshakes.
    """
    if not settings.configured:
        return None

    client = httpx.AsyncClient()

    def model(timeout_s: float) -> LlmClient:
        return OpenAiClient(
            api_key=settings.openai_api_key or "",
            model=settings.model,
            client=client,
            timeout_s=timeout_s,
        )

    # Two budgets rather than one: a stalled extraction should cost an anchor's leads in
    # seconds, while a stalled judgement would lose everything the run has already paid for.
    quick = model(EXTRACT_TIMEOUT_S)
    return DiscoveryPipeline(
        namer=PlaceNamer(api_key=settings.places_api_key or "", client=client),
        source=BraveSearchSource(api_key=settings.brave_api_key or "", client=client),
        extractor=PlaceExtractor(quick),
        resolver=PlacesResolver(api_key=settings.places_api_key or "", client=client),
        classifier=CategoryClassifier(quick),
        judge=CandidateJudge(model(JUDGE_TIMEOUT_S)),
    )
