"""Turning typed text into places that exist.

The mouse half of place-name entry, and the thing that lets the assistant stop inventing
coordinates. Both call the same lookup: a rider typing "Blewett Pass" and the model asking for
it are the same question, and two implementations of it would drift.

It also closes something older than the chat feature. The original trip-creation spec was
"type a starting and ending address or choose to click on the map" — the clicking shipped and
the typing did not, because nothing could turn a name into a coordinate.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from motorooter.api.deps import Lookup
from motorooter.api.errors import NotImplementedYet
from motorooter.api.schemas import ERROR_RESPONSES, ErrorResponse, GeocodeResponse, GeocodeResult
from motorooter.routing.models import Coordinate

router = APIRouter(prefix="/api/geocode", tags=["geocode"], responses=ERROR_RESPONSES)

NOT_IMPLEMENTED = 501


@router.get(
    "",
    response_model=GeocodeResponse,
    summary="Find places by name",
    description=(
        "Places matching a search text, verified against Google Places, best first.\n\n"
        "**Several results, not one.** A name is a claim until something verifies it, and "
        "plenty of names verify to more than one real place. Resolving that ambiguity "
        "silently is the failure this shape exists to avoid.\n\n"
        "An empty `results` is an ordinary answer — a typo matches nothing. Only `place_id` "
        "may be stored; everything else is per-request under Google's terms."
    ),
    responses={NOT_IMPLEMENTED: {"model": ErrorResponse}},
)
async def geocode(
    lookup: Lookup,
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=200,
            # Requires a non-space character: `min_length` counts spaces, and sending
            # whitespace to a metered API to be told it matched nothing is a wasted request.
            pattern=r"\S",
            description="What to search for.",
        ),
    ],
    near: Annotated[
        str | None,
        Query(
            description=(
                "`lat,lon` to bias results toward, usually the trip's last waypoint. Omit it "
                "when there is nothing to bias from; a made-up centre would silently prefer "
                "one of several real places."
            ),
            pattern=r"^-?\d+(\.\d+)?,\s*-?\d+(\.\d+)?$",
        ),
    ] = None,
) -> GeocodeResponse:
    """Search for a place by name."""
    if lookup is None:
        raise NotImplementedYet("place search (no Places credentials configured)")

    found = await lookup.search(q.strip(), near=_near(near))
    return GeocodeResponse(
        results=[
            GeocodeResult(
                name=item.name,
                address=item.address,
                place_id=item.place_id,
                coordinate=item.coordinate,
                kinds=list(item.kinds),
            )
            for item in found
        ]
    )


def _near(raw: str | None) -> Coordinate | None:
    """The bias point, or `None`.

    Validated rather than tolerated: a malformed `near` that is silently dropped answers
    confidently from the wrong hemisphere, which is worse than refusing.
    """
    if raw is None:
        return None
    latitude, longitude = (part.strip() for part in raw.split(","))
    # An out-of-range value raises `ValidationError`, which the app already answers as a 422.
    # Refusing beats dropping the disambiguator and answering confidently from elsewhere.
    return Coordinate(lat=float(latitude), lon=float(longitude))
