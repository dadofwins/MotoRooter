"""Which found places are worth changing the rider's route for.

Discovery proposes; this decides. The bar is deliberately not "is this place good" — it is
"is this place good enough that taking the decision away from the rider is a favour". Adding
too few is a rider adding one by hand; adding too many is a route they did not ask for, so
every bound here fails in the first direction.

**The score is a ranking, not a measurement.** Judging the same corridor twice, minutes
apart and with no code changed, moved individual scores by up to 0.15 and changed the number
clearing 0.85 from seven to three — the same places, differently numbered. So a rule of the
form "route through everything above X" hands the size of the edit to model variance, and a
rider running the same trip twice would get a different route for no reason they could see.
Rank and cap; the floor is a gate, not the selector.

Three bounds, each answering a different question, all of them arithmetic:

| bound | question | value |
|---|---|---|
| floor | is this place any good? | score >= 0.70 |
| detour budget | does this reshape the trip? | added distance <= 15% of the leg |
| count | is this still a route or a bus tour? | one per two hours of riding |

The count is usually what binds, which is why it is the one a caller can override. The other
two are not parameters: they are the safety rails, and a caller who could widen them could
ask for a route through forty places that each cost an hour.
"""

from collections.abc import Sequence

from motorooter.planning.metrics import nearest_distance_m
from motorooter.routing.models import RouteLeg
from motorooter.speeds import leg_duration_s
from motorooter.trips.models import Poi

MIN_SCORE = 0.70
"""Below this the judge itself was hedging, so we do not act on it unasked.

Read off real reasons rather than tuned to produce a pleasing count, which is what makes it
arguable by the next person. On two live corridors the prose turns at consistent places: at
0.75 and above the reasons are unqualified ("a strong detour", "an obvious stop"); at 0.60
they start hedging ("useful but not special", "more functional than a pleasant hangout"); at
0.45 and below they are openly negative ("only worth it if you specifically need"). 0.70 is
the last rung where the judge is still recommending.

Raising it to 0.80 would have dropped a campground with a beginner motorcycle loop starting
at it — which is precisely what this app exists to find.
"""

DETOUR_BUDGET_FRACTION = 0.15
"""How much longer the ride may get, in total, across every place added.

The only one of the three bounds expressed in the units the harm actually occurs in, which
makes it the load-bearing one. A budget rather than a per-place cap because two detours that
each look affordable are not necessarily affordable together.
"""

DETOUR_COST_MULTIPLIER = 2.0
"""Added distance, estimated as twice the distance off the route.

An out-and-back is the worst case and this assumes it, so the estimate over-states a place
the road passes close to — which fails in the safe direction. Measuring the truth would mean
re-routing once per candidate before deciding whether to route through it, which is a
metered request per place to answer a question a free approximation already answers
conservatively.
"""

HOURS_PER_ADDITION = 2.0
"""One stop per this many hours of riding, by default.

From how often a rider genuinely stops — fuel, a meal, a camp — and set at the low end of
that. It gives one addition on a two-hour leg and about nine across a three-day trip.

It is the binding constraint in practice and therefore the number most worth arguing with,
which is exactly why it is a default and not a constant: a caller can ask for more. A rider
who says nothing gets the conservative answer, and "find me a few more stops along here"
works by asking, which is how everything else in this app scales.
"""

_SECONDS_PER_HOUR = 3600.0


def default_limit(legs: Sequence[RouteLeg]) -> int:
    """How many places to route through when nobody said, across the whole trip.

    Paced by *time* rather than distance, because the same 400 km is five hours of tarmac and
    ten of dirt, and it is the hours that decide how often someone wants to get off the bike.

    Across every leg, not the longest one. On a four-leg 797 km trip the longest leg's 5.1
    hours gave two stops for 14.5 hours of riding — a rule that quietly scaled with how the
    rider happened to split their route rather than with how far they were going.

    At least one: nought would make the feature silently do nothing on a short ride, or on a
    trip with nothing routed yet.
    """
    hours = sum(leg_duration_s(leg) for leg in legs) / _SECONDS_PER_HOUR
    return max(1, int(hours / HOURS_PER_ADDITION))


def above_the_floor(pois: Sequence[Poi]) -> tuple[Poi, ...]:
    """Every place good enough to consider adding, best first.

    Separate from `worth_routing_through` so that "good enough" has one definition rather
    than two: a caller reporting what it *left out* is asking this question, and answering
    it by reimplementing the floor is how the two drift.

    Unscored places are excluded rather than ranked last. A pin the rider dropped
    themselves was never judged, and silence is not consent to reroute through it.
    """
    return tuple(
        sorted(
            (
                place
                for place in pois
                if place.score is not None and place.score >= MIN_SCORE and not place.on_route
            ),
            key=lambda place: place.score or 0.0,
            reverse=True,
        )
    )


def worth_routing_through(
    pois: Sequence[Poi], *, legs: Sequence[RouteLeg], limit: int | None = None
) -> tuple[Poi, ...]:
    """The places to route through, best first.

    Args:
        pois: everything discovery has saved against the trip. Unscored pins and places
            already on the route are ignored.
        legs: the trip's routed legs. Each one carries its own detour budget, and together
            they set the default count.
        limit: how many at most. `None` asks for `default_limit`. Zero or negative asks for
            none, which is honoured rather than corrected — a caller that computed its way to
            zero means it.

    **The budget is per leg**, against the leg each place actually sits beside. One budget
    drawn from the longest leg was both too generous next to a short leg and too mean next to
    a long one, and it let a place beside a 40 km connector spend an allowance earned by a
    400 km day. The harm lands on a leg, so the bound belongs there.

    Best first rather than in route order: this ranks, and `insert_in_route_order` decides
    where each one goes.
    """
    allowed = default_limit(legs) if limit is None else limit
    if allowed <= 0 or not legs:
        return ()

    budgets = [DETOUR_BUDGET_FRACTION * leg.distance_m for leg in legs]
    chosen: list[Poi] = []
    for place in above_the_floor(pois):
        if len(chosen) >= allowed:
            break
        index, cost = _nearest_leg(place, legs)
        if cost > budgets[index]:
            # Skipped rather than stopped: a cheap good place ranked behind an expensive one
            # must not be lost to it, on this leg or any other.
            continue
        budgets[index] -= cost
        chosen.append(place)
    return tuple(chosen)


def _nearest_leg(place: Poi, legs: Sequence[RouteLeg]) -> tuple[int, float]:
    """Which leg this place sits beside, and roughly what routing through it would add.

    Nearest rather than any leg it is within reach of: a place between two legs is a detour
    from the one it is closest to, and charging the further leg would let the same place be
    afforded twice over.
    """
    costs = [_detour_cost_m(place, leg) for leg in legs]
    index = min(range(len(costs)), key=lambda position: costs[position])
    return index, costs[index]


def _detour_cost_m(place: Poi, leg: RouteLeg) -> float:
    """Roughly how much longer the ride gets if the route goes through `place`."""
    off_route = nearest_distance_m(leg.geometry, place.coordinate)
    if off_route is None:
        # `RouteLeg` validates its geometry as two points or more, so there is always
        # something to measure against. The helper's optional return exists for callers
        # holding a route that may not have been computed yet.
        raise AssertionError("unreachable: a RouteLeg always has geometry")  # pragma: no cover
    return DETOUR_COST_MULTIPLIER * off_route
