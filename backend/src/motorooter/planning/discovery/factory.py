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
from motorooter.planning.discovery.retry import RetryingSearchSource
from motorooter.planning.discovery.sources.brave import BraveSearchSource

EXTRACT_TIMEOUT_S = 15.0
"""How long a structured extraction may take before it is abandoned.

Extraction reads a handful of snippets and returns a short list, so it has no business
taking the 120 s a chat turn is allowed — one stalled call was holding an anchor for two
minutes.

This number has moved twice, and the history is the point. Eight seconds was a guess and
timed out on *ordinary* calls. Twenty-five was measured, and still timed out, because a
batch of fifteen snippets genuinely took 35-44 s. Both were the wrong question: the call was
slow because the model was reasoning about a task that needs no reasoning, and at
`EXTRACT_EFFORT` the same batch takes 2.9-3.4 s. Fifteen is roughly four times normal
latency, which is headroom rather than a wager.

Chat keeps the long default, which is why this is separate rather than lowered globally.
"""

EXTRACT_EFFORT = "minimal"
"""How hard the model thinks when naming places in a snippet.

Barely, and that is not a compromise. Measured on one live batch of fifteen results, the
default budget took 35-44 s and `minimal` took 2.9-3.4 s, and both returned the same six
places — the slow runs were not more accurate, and one of them offered "Washington State,
USA", the region it had been handed, as somewhere to visit.

The stage is already constrained to names that appear in its input, so there is nothing for
reasoning to add: the model is copying, not deciding. Judging is the opposite and keeps the
default.

One caveat, unmeasured beyond a single corridor: `minimal` copies slightly more of a page
title into a name — "Bumping Lake Campground | Goose Prairie, Washington" rather than
"Bumping Lake Campground". Places text search absorbed it, but trimming title furniture
deterministically would be a better answer than paying twelve times the latency for it.
"""

JUDGE_TIMEOUT_S = 45.0
"""Longer than extraction: scoring reasons over a whole corridor is a real piece of work,
and losing it loses everything the run has already paid for.

Judging keeps the default reasoning budget, and unlike the claim above this one is measured.
The same candidate scored 0.90 at the default, 0.45 at `low` and 0.65 at `minimal` — the
thinking is not overhead here, it is the answer, and a threefold speedup that reorders the
list is not a speedup worth having. Extraction could skip it because copying a name from a
paragraph has one right answer; deciding whether a waterfall is worth the detour does not.
"""

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

    def model(timeout_s: float, effort: str | None = None) -> LlmClient:
        return OpenAiClient(
            api_key=settings.openai_api_key or "",
            model=settings.model,
            client=client,
            timeout_s=timeout_s,
            reasoning_effort=effort,
        )

    # Two budgets rather than one: a stalled extraction should cost an anchor's leads in
    # seconds, while a stalled judgement would lose everything the run has already paid for.
    # The mechanical stages also skip the thinking, which is where the seconds actually went.
    quick = model(EXTRACT_TIMEOUT_S, EXTRACT_EFFORT)
    return DiscoveryPipeline(
        namer=PlaceNamer(api_key=settings.places_api_key or "", client=client),
        # Wrapped, not replaced: candidates carry the source name as provenance, and the
        # decorator passes it through. Retry is the whole of the decorator stack here —
        # Brave's terms forbid caching results, so there is no cache to compose with it.
        source=RetryingSearchSource(
            BraveSearchSource(api_key=settings.brave_api_key or "", client=client)
        ),
        extractor=PlaceExtractor(quick),
        resolver=PlacesResolver(api_key=settings.places_api_key or "", client=client),
        classifier=CategoryClassifier(quick),
        judge=CandidateJudge(model(JUDGE_TIMEOUT_S)),
    )
