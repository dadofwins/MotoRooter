# Milestones

## M0 — routing quality — **PASSED** (2026-08-25)

Does our routing produce roads a rider would actually take? Measured against WABDR Section 3
and confirmed by Tim. See "Routing quality" in the root `CLAUDE.md`. Consequences: hosted ORS
is good enough, waypoint density is a hard requirement on the LLM, and provider durations are
unusable.

## M1 — MVP — the planning experience, end to end

Tim's definition, verbatim in intent, with where each item actually stands
(**last audited 2026-08-25, against `main`, by reading the code rather than the queue**):

| # | Item | State |
|---|---|---|
| 1 | Type in the chat box and have tool calls execute | **In progress.** Rail merged; tools being built |
| 2 | Click and drag a route | **Done** |
| 3 | One button for a full generation: POIs, gas, campsites, hotels | **Done** |
| 4 | See POIs, click one, get Places detail — images, ratings | **Done** |
| 5 | Route through the found POIs, or add/ignore each | **Half.** Selective add works; the bulk button does not exist |

**What M1 is not.** No GPX export, no day splitting, no multi-day planning, no accounts.

### What is left, and nothing else is

**Item 1 — the assistant.** `fe/chat-rail` is merged and streams; `POST /trips/{slug}/chat` is
still 501 and no concrete `Tool` exists. Six tools agreed: `find_places`, `add_waypoint`,
`remove_waypoint`, `set_leg_intent`, `add_poi_to_route`, `describe_trip`.

Two of those cannot ship yet, and the reason is the rule rather than the code. *Every tool the
assistant can call must also be reachable by mouse.* Today `remove_waypoint` has only "Remove
last point", and `set_leg_intent` has no per-leg control at all — so shipping either would make
chat the only way to do something, in an app whose central design rule is that it must not be.
The affordances are the blocker, not the tools.

**Item 5 — the bulk button.** "One button to route through the found POIs" has no
implementation. `addPoiToRoute` covers the selective half via right-click. This is the smallest
remaining M1 item and the easiest to forget, because item 5 reads as done from the demo.

### Also outstanding, not part of M1 but asked for

- **Settings dialog** behind a gear icon, housing miles/km. Tim asked for this directly; it is
  the last unaddressed item from his replan feedback. Do not let it keep slipping.
- Per-category discovery control, so "find me more restaurants" has a mouse equivalent at that
  granularity. Belongs with the settings dialog.

### Resolved since this document was written

- Derived trip duration — **done**, and then corrected: trustworthiness is a per-provider
  capability, because Google's car profile beats our estimate on highway. See root `CLAUDE.md`.
- The stitching gap threshold — **measured** on a real mixed google/ors trip: 0.5 m and 11.3 m
  observed, bridged below 500 m.
- Discovery speed — ten minutes and stalling, now 19.1 s live.
- Anchor naming — was searching the wrong area entirely; 21 of 25 candidates dropped for
  distance before the fix.

### Still deferred, deliberately

- GPX export, and the hardware test on a real GPS unit.
- Ascent, until the 6,400–8,800 m against 3,188 m discrepancy is explained.
- `be/road-expansion`, shelved pending re-measurement now that naming is fixed.

## M2 and beyond — not yet scoped

Export to a real GPS unit is the first thing after M1, because it needs Tim's hardware and
its constraints (Garmin point limits, track vs route semantics) may reach back into the trip
model.
