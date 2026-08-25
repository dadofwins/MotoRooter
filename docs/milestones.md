# Milestones

## M0 — routing quality — **PASSED** (2026-08-25)

Does our routing produce roads a rider would actually take? Measured against WABDR Section 3
and confirmed by Tim. See "Routing quality" in the root `CLAUDE.md`. Consequences: hosted ORS
is good enough, waypoint density is a hard requirement on the LLM, and provider durations are
unusable.

## M1 — MVP — the planning experience, end to end

Tim's definition, verbatim in intent:

1. Type in the chat box and have tool calls execute — "find me more restaurants on the route"
   runs the appropriate tool.
2. Click and drag a route.
3. One button for a full generation: POIs, gas, campsites, hotels.
4. See POIs on the map, click one, get Google Places detail — images, ratings.
5. One button to route through the found POIs; or selectively add each to the route or ignore
   it.

**What M1 is not.** No GPX export, no day splitting, no multi-day planning, no accounts, no
trip-list UI. Persistence exists and works but is not part of the demo. This milestone is
about whether *planning a trip* feels good — everything else is downstream of that answering
yes.

### Critical path

The LLM tool layer gates three of the five items (1, 3, and most of 5). It is the long pole
and nothing else in the backend queue should precede it.

```
  LLM tool layer ──┬──> chat with tool calls        (item 1)
                   ├──> discovery / full generation (item 3)
                   └──> route-through-POIs          (item 5)

  Places enrichment ─────> POI detail dialog        (item 4)   independent, start any time
  Drag 3b (interactive) ─> click and drag           (item 2)   needs the vertical slice
  POI pins + dialog ─────> see and click POIs       (item 4)
```

### Backend order

1. **LLM tool layer.** OpenAI, server-side execution, NDJSON streaming over the existing
   replan endpoint. Every tool wraps the same service function the REST endpoint calls — the
   chat path and the mouse path must not diverge, because item 5 is the same operation
   reached both ways.
2. **Discovery**, behind the Replan button. LLM proposes candidates; nothing reaches the map
   unresolved. Waypoint density per M0 applies to *route* waypoints, not POIs.
3. **Places enrichment** (`GET /api/places/{place_id}`, currently a 501 stub with a frozen
   schema). Independent of the LLM work — worth doing first if the tool layer stalls, since
   it unblocks the frontend's dialog.
4. **Route-through-POIs.** Largely assembly: insert selected POIs as waypoints, re-route
   through the existing trip router.

### Frontend order

1. **Drag 3b** — the interactive half. Item 2, and the pure half is already reviewed.
2. **POI pins and detail dialog** — item 4. Buildable against the 501 stub today.
3. **Chat rail with streaming tool calls** — item 1.
4. **Add-to-route / ignore controls** — item 5.

### Deferred out of M1, deliberately

- GPX export, and the hardware test on a real GPS unit.
- Derived trip duration. Not in Tim's list, so it does not gate the demo — but nothing may
  display the provider's figure in the meantime (bicycle times, off by 2×).
- Ascent, until the 6,400–8,800 m against 3,188 m discrepancy is explained.
- The 25 m stitching gap threshold, until a mixed google/ors trip exists to measure it on.

## M2 and beyond — not yet scoped

Export to a real GPS unit is the first thing after M1, because it needs Tim's hardware and
its constraints (Garmin point limits, track vs route semantics) may reach back into the trip
model.
