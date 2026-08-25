"""The discovery domain: claims, resolutions, evidence, judgements.

The one invariant worth building types around: **search results and model output are claims,
not facts.** A blog says a hot spring is at some coordinate. A model will invent one and sound
certain about it. Nothing reaches the map unresolved.

That rule is carried by the types rather than by discipline. A `Candidate` has no trustworthy
location and no `place_id`, so it *cannot* be turned into a pinned `Poi` — the only route to
the map runs through `ResolvedCandidate`, whose coordinate comes from Places rather than from
whoever made the claim. A future caller cannot take the shortcut, because there is not one.

The stages are separate types for the same reason. A candidate that has been resolved but not
judged is a different thing from one that has been scored, and collapsing them into one
optional-everything model would make "is this safe to pin" a question about which fields
happen to be populated.
"""

from pydantic import BaseModel, ConfigDict, Field

from motorooter.routing.models import Coordinate
from motorooter.trips.models import Poi, PoiCategory, PoiSource


class Candidate(BaseModel):
    """Something a source claims exists. Unverified by construction.

    Deliberately has no `place_id` and no trusted coordinate. Anything holding one of these
    knows it is holding a claim.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    category: PoiCategory

    found_near: Coordinate
    """The corridor anchor whose search produced this — our coordinate, not the source's.

    The only location a candidate has that is worth anything: it says which part of the
    route to look near, without believing a word the source said about where the thing is.
    """

    source: str = Field(min_length=1)
    """Adapter that made the claim, e.g. `brave` or `llm`. Decides how far to trust it."""

    claimed_coordinate: Coordinate | None = None
    """Where the source said it is. A hint for resolution, never a pin."""

    snippet: str | None = None
    """The words that justified it — a ride report, a forum post.

    Kept verbatim rather than summarised, because it is the judge's actual evidence: "the
    gravel washes out after spring melt" is the sort of thing no metric will ever produce.
    """

    url: str | None = None


class ResolvedCandidate(BaseModel):
    """A candidate that Places confirmed, with a real identity and a real location."""

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    """Kept, so provenance survives: which source suggested this, and on what evidence."""

    place_id: str = Field(min_length=1)
    """The only Places field safe to store indefinitely, per their terms."""

    coordinate: Coordinate
    """From Places. Not the claim — a source naming the wrong valley must not move the pin."""

    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    user_rating_count: int | None = Field(default=None, ge=0)
    """Places' own rating, carried in memory for the judge and **never persisted**.

    Google's terms permit storing `place_id` indefinitely and very little else, so these live
    only on the way through: `to_poi` copies the id and drops them, which is the boundary
    that enforces the rule. Anything that writes a `ResolvedCandidate` to storage would
    breach it, so nothing does — `Poi` is the only persisted shape.

    Worth having: "4.4 stars from 15 reviews" is real evidence about whether a place is
    worth stopping at, and it is a fact rather than a judgement, so the model should be
    handed it rather than asked to guess.
    """

    distance_off_route_m: float | None = Field(default=None, ge=0.0)
    """How far off the corridor it sits, measured when the coordinate first existed.

    Recorded here rather than recomputed later because resolution is the first moment there
    is a coordinate to measure, and the same number is both the relevance filter and evidence
    for the judge. Computing it twice would let the two copies disagree.
    """

    def to_poi(self, *, poi_id: str, on_route: bool = False, note: str | None = None) -> Poi:
        """The verified POI this resolves to.

        `PoiSource.PLACES` rather than the discovering source: Places is what vouched for the
        location, and that is what `Poi.is_verified` is asking about. Recording `brave` here
        would claim a verification that a web search cannot give.
        """
        return Poi(
            id=poi_id,
            name=self.candidate.name,
            category=self.candidate.category,
            coordinate=self.coordinate,
            source=PoiSource.PLACES,
            place_id=self.place_id,
            on_route=on_route,
            note=note,
        )


class Evidence(BaseModel):
    """Measured facts handed to the judge, so it is not asked to estimate them.

    Every field is optional, and absent is not zero. A provider that cannot report surface is
    not a road with no dirt on it; elevation gain has no trustworthy source at all today. A
    scorer reading a missing signal as a zero would quietly rank every unmeasured road as flat
    tarmac, which on the back roads this app exists to find is most of them.
    """

    model_config = ConfigDict(frozen=True)

    distance_off_route_m: float | None = Field(default=None, ge=0.0)
    """How far off the line it sits. The input to "is the detour worth it"."""

    twistiness_deg_per_km: float | None = Field(default=None, ge=0.0)
    unpaved_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    unknown_surface_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    detour_ratio: float | None = Field(default=None, ge=0.0)

    distance_to_fuel_m: float | None = Field(default=None, ge=0.0)
    """Remoteness. Matters more on a motorcycle than the numbers suggest."""

    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    user_rating_count: int | None = Field(default=None, ge=0)
    """From Places, and response-only — never persisted. Their terms permit `place_id` and
    little else, so these live on the way through and are not written down."""


class ScoredCandidate(BaseModel):
    """A judged candidate, with the evidence and the reasoning that produced the score."""

    model_config = ConfigDict(frozen=True)

    resolved: ResolvedCandidate
    evidence: Evidence
    """Retained so a human can check whether the judgement follows from the numbers.

    This is what makes the stage reviewable at all: a score alone cannot be argued with, and
    the first thing anyone will want to know is why a road they love scored 0.3.
    """

    score: float = Field(ge=0.0, le=1.0)
    """Normalised, and bounded because a model asked for a score out of ten will
    occasionally return eleven."""

    reason: str = Field(min_length=1)
    """Why, in the judge's own words. An unexplained number is unreviewable, and a rider
    will want to argue with it."""
