# Milestones

## M0 — routing quality — **PASSED** (2026-08-25)

Does our routing produce roads a rider would actually take? Measured against WABDR Section 3
and confirmed by Tim. See "Routing quality" in the root `CLAUDE.md`. Consequences: hosted ORS
is good enough, waypoint density is a hard requirement on the LLM, and provider durations are
unusable.

## M1 — MVP — **COMPLETE** (2026-08-26)

All five of Tim's items work end to end against real providers.

| # | Item | Where it lives |
|---|---|---|
| 1 | Chat box with executing tool calls | Rail bottom-right; six tools over `POST /trips/{slug}/chat` |
| 2 | Click and drag a route | Per-leg, ~917 ms, cadence resolved per intent |
| 3 | One button for a full generation | "Find places along the route", ~20 s |
| 4 | POIs with Places detail — images, ratings | List in the rail and map pins, both opening the dialog |
| 5 | Route through found POIs, or add/ignore each | Per-group bulk buttons, right-click add, Ignore |

**Two deliberate departures from the literal wording**, both argued rather than assumed:

- Item 5 is **per group**, not one button over everything found. Twenty-nine places is a search
  result, not an itinerary, and a single button nobody presses is the demo-shaped version of the
  feature — which is why the gap survived unnoticed. The literal single button is a few lines if
  Tim wants it too.
- The landing screen **auto-opens** a returning rider's only trip, with a persistent "New trip"
  control so create stays reachable. Tim's call.

### What M1 cost, worth remembering

The long pole was never the feature work. It was that things were merged, green, and called by
nobody — **six times**: `createApiClient`, POI pins with no data source, `routed_from` unstamped
on the fast path, the chat client method living only on a stale branch, `PlaceDetails` never
assigned to `app.state`, and `trip_router` hand-rolling a copy instead of calling `stamped`. A
diff cannot show what does not call it, and every review passed.

The structural answer landed with `be/places-detail`: optional services are declared in one place
and **a name declared but not built raises at startup**. Extend that shape rather than trusting
review to catch the seventh.

## M2 and beyond — not yet scoped

Export to a real GPS unit is the first thing after M1, because it needs Tim's hardware and
its constraints (Garmin point limits, track vs route semantics) may reach back into the trip
model.
