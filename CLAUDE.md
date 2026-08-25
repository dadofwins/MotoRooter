# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MotoRooter is an AI-powered adventure motorcycle trip planner. Users describe a trip in natural
language *or* build it entirely with the mouse; the app finds a spectacular route (twisties,
unpaved sections, views, BDR segments), discovers stays/food/gas/POIs along it, and exports GPX
for a motorcycle GPS.

**Design rule that governs everything: chat is an accelerator, never a requirement.** Every action
the assistant can take must also be reachable via map interaction or a form control. If you add a
tool to the LLM layer, you owe it a UI affordance in the same change.

## Milestones

`docs/milestones.md` holds the current milestone definition and the ordering that follows
from it. **M0 (routing quality) has passed.** M1 is the MVP planning experience: chat with
working tool calls, drag-to-reroute, one-button POI generation, Places detail on click, and
routing through selected POIs.

The LLM tool layer gates three of M1's five items and is the long pole. Nothing else in the
backend queue precedes it.

## Parallel work

Three sessions work on this repo at once — a backend engineer, a frontend engineer, and an
integrator — each in its own git worktree. **Read `docs/parallel-work.md` before starting**:
it defines file ownership, the contract-change protocol, and the review loop. Scoped
instructions live in `backend/CLAUDE.md` and `frontend/CLAUDE.md`.

The one rule worth repeating here: `backend/src/motorooter/api/schemas.py` is the frontend
contract. It generates `shared/openapi.json`, which generates `frontend/src/api/schema.ts`.
Changing an existing shape there needs integrator sign-off; `make contract-check` fails CI
if the generated files drift.

## Status

**Built:** the routing layer end to end — domain models, `RoutingProvider` protocol, shared adapter
contract suite, `FakeProvider`, ORS and Google Directions adapters, polyline codec, registry,
policy resolver, caching/retry/quota decorators, config factory. Trip and POI models, slug
validation, `TripStore` protocol with an in-memory implementation and a shared store contract
suite. The REST API surface with generated TypeScript types. Frontend `DragScheduler` (throttle,
sequence numbers, commit-on-release), app shell, multi-stage Dockerfile, Cloud Build config.

**Stubbed with frozen schemas** (501, so the frontend can build against real shapes): replan,
GPX export, Places enrichment.

**Not built:** `GcsTripStore`, leg stitching, GPX export, the LLM tool layer, discovery and
Places enrichment, the Google Maps canvas, and the chat UI.

Update this section as reality changes, and do not describe a component as existing until it does.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python, fully type-annotated, FastAPI + Pydantic v2 |
| Frontend | React + **TypeScript**, Vite |
| Map | Google Maps JavaScript API (vector) |
| Routing | Pluggable providers (see Routing architecture); first adapters: hosted OpenRouteService for unpaved, Google Directions for on-road |
| POI enrichment | Google Places API |
| LLM | OpenAI, function/tool calling |
| Hosting | Google Cloud Run |
| Storage | Cloud Storage bucket, trip-name-prefixed |

## Layout

Monorepo:

```
backend/    FastAPI app, routing orchestration, LLM tool layer, GPX export
frontend/   Vite + React + TS
shared/     Schema contract (see below)
infra/      Dockerfiles, Cloud Run service YAML, deploy scripts
```

**Deployment is one Cloud Run service, not two.** The frontend does not need a service of its own:
Vite compiles React+TS to static HTML/JS/CSS, so Node is a *build-time* dependency only, with no
runtime process to host. The Dockerfile is multi-stage — a Node stage runs `vite build`, and the
Python stage copies the resulting `dist/` into the FastAPI image and serves it via `StaticFiles`.
One origin, so no CORS. Remember the SPA fallback: unmatched non-API paths must return
`index.html`, not a 404, or deep links and page refreshes break.

A separate frontend service would only be warranted by server-side rendering (which this app,
being a stateful map canvas, does not want) or by CDN edge caching of assets — and if asset
latency ever matters, the cheap fix is Cloud CDN in front of the same service, not a second one.

The 2-service shape you picked assumed a self-hosted router; a hosted routing provider removes
service B. Keep `infra/` structured so a self-hosted routing service (ORS or Valhalla) can be added
later without reshuffling — that is the likely escape hatch when hosted rate limits or costing
control become the bottleneck.

## Schema contract

Route geometry, waypoints, and POI records cross the Python/TypeScript boundary constantly and are
the most bug-prone surface in the app. Define them once as Pydantic models, generate the OpenAPI
schema, and generate TypeScript types from it as a build step. Do not hand-maintain parallel type
definitions on both sides.

Prefer GeoJSON as the wire format. Decode Google's encoded polylines at the boundary rather than
letting encoded strings leak into domain models.

## Persistence

Prototype-grade and deliberately public: no accounts, no auth. A trip is identified by a
user-chosen unique name and is world-readable and world-editable via a shareable link.

**Do not write trips to the container filesystem.** Cloud Run's disk is ephemeral and per-instance
— data would vanish on restart and be invisible to sibling instances. Use a Cloud Storage bucket
keyed `trips/<slug>/trip.json` instead. This preserves the directory mental model, and if you want
literal filesystem semantics you can mount the bucket as a Cloud Run GCS FUSE volume; note FUSE has
no atomic rename, so write-then-swap patterns need care.

Trip names become object paths, so **slugify and validate them**: lowercase, `[a-z0-9-]` only,
reject path separators, `.`/`..`, and empty/overlong input. Treat this as an input-validation test
case, not a runtime nicety. Name collisions overwrite by design for now; when this stops being a
prototype the fix is an edit token stored alongside the trip, not retrofitted auth.

## Routing architecture

Routing is **pluggable by design**: no provider name may appear outside its own adapter module.
Different sections of one trip routinely use different engines, and the engine set will change.

**A trip is a list of legs, and each leg carries its own routing policy.** Per-section provider
choice is therefore a property of the data, not a branch in the code. A leg records which policy
produced it, so re-routing one leg never disturbs its neighbors.

Four pieces:

1. **`RoutingProvider` protocol** — one narrow interface (`route`, and optionally `match`), taking
   a domain-level `RouteRequest` and returning a normalized `RouteLeg` (GeoJSON `LineString`,
   distance, duration, surface spans, elevation, provider tag). Providers translate to and from
   their own wire formats internally; nothing provider-shaped escapes the adapter. Decode encoded
   polylines here.
2. **Capability declaration** — each provider advertises what it can do: prefers-unpaved, map
   matching, alternatives, max waypoints per request, elevation, and its cost profile
   (`live_update_interval_ms`, quota ceiling). The resolver dispatches on *capability*, never on a
   hardcoded provider name, so adding an engine doesn't touch dispatch logic. The cost fields are
   what let the UI throttle per provider (see Drag-to-reroute) without knowing which engine it's
   talking to.
3. **Policy resolver** — maps a leg's intent (`highway_connector`, `twisty_paved`, `unpaved`,
   `technical_offroad`, `manual_track`) to a provider plus profile parameters. Config-driven table,
   overridable per leg by the user. This is where "different algorithm per road type" lives.
4. **Decorator providers** — caching, retry/backoff, quota accounting, and request logging are each
   a provider that wraps another provider. Written once, applied uniformly, and independently
   testable. Compose as `Caching(QuotaGuard(Retry(Ors(...))))`.

Because the protocol is narrow, a `FakeProvider` returning fixture geometry is trivial — that is
what keeps the test suite hermetic and TDD practical for stitching logic.

Planned adapters: `ors`, `google`, `fake`. `valhalla` and `graphhopper` are the anticipated later
additions; the architecture exists so they cost an adapter, not a refactor.

### Provider choice: OpenRouteService

ORS over GraphHopper, on four counts:

- **Free tier is usable.** ORS allows roughly 2,000–2,500 directions requests/day. GraphHopper's
  free plan is **500 credits/day**.
- **GraphHopper paywalls the one feature this app needs.** Its free plan "cannot use the flexible
  mode (`ch.disable=true`)" — flexible mode *is* the custom-model mechanism for weighting surface
  and track type, i.e. the entire basis for preferring dirt. Getting it starts at €69/mo.
- **Self-hostable with an identical API.** ORS is open source, so the escape hatch from rate limits
  is running the same adapter against your own instance — no code change. Self-hosted ORS also
  unlocks full custom profile YAML, which is better unpaved tuning than either hosted tier offers.
- **License fit.** ORS is GPL-family, matching this project.

Both free tiers are **non-commercial only**. Revisit before any public launch.

Caveat: hosted ORS's unpaved-preference tuning is real but limited (`profile_params`,
`avoid_features`, preference weighting). If routing quality plateaus, self-hosted ORS with a custom
profile is the next step, and Valhalla the one after.

## Routing quality: validated (M0, 2026-08-25)

The riskiest assumption in the project has been tested against a real route and **confirmed
acceptable by the rider**. WABDR Section 3 (Ellensburg–Cashmere, 126.7 km) routed through
hosted ORS `cycling-mountain` and compared against the published BDR track
(`scripts/routing_spike.py`):

| intermediate waypoints | follows the BDR (within 100 m) | median deviation |
|---|---|---|
| none | 37% | 782 m |
| 8 (one per ~16 km) | 58% | **50 m** |
| 20 (one per ~6 km) | 78% | **26 m** |

Three conclusions, all load-bearing:

**1. Hosted ORS is good enough. Do not self-host yet.** At realistic waypoint density the
engine puts you on the same road. Self-hosted ORS with a custom moto profile, and Valhalla,
are both deprioritised until something else forces them. The pluggable architecture still
earns its keep — it just is not needed today.

**2. Waypoint density is a hard product requirement, not a nicety.** Endpoint-to-endpoint
routing does not reproduce a curated route, and never will: a BDR exists because people chose
those roads, not because they are optimal under any cost function. **The LLM must emit
waypoints at roughly 10–15 km spacing.** A tool that returns only start, end and must-sees
will produce a plausible route down the wrong roads, and nothing downstream will notice.

**3. Provider durations are unusable — compute our own.** `cycling-mountain` returns *bicycle*
times: 8.0 h for 133 km, about 16 km/h. Trip planning is duration-driven, so a rider would be
told a four-hour day takes eight. Derive ETA from distance and surface; ignore
`RouteLeg.duration_s` from this provider for anything user-facing.

Unresolved: reported ascent (6,400–8,800 m against the reference's 3,188 m) looks wrong.
Either the profile takes much steeper lines or ORS elevation is noisy over gravel. Do not show
climb figures to a user until someone checks.

## Surface reporting: unknown stays unknown

`unpaved_fraction` counts only spans explicitly tagged unpaved. `Surface.UNKNOWN` is not
counted as either paved or dirt.

This is a deliberate product decision, not an oversight, and it has a measured cost. An OSM
audit of WABDR Section 3 (`scripts/wabdr_osm_audit.py`, 61 probes over 126 km) found **25% of
the distance carries no `surface` tag at all** and 75% no `tracktype`. So the reported figure
systematically *under*-states how much dirt a route contains — roughly 41% reported against
~48% actual on that section.

Under-reporting is the right failure direction: a rider who is told 41% and finds 48% has a
better day than the reverse. But the UI must **show the unknown share** rather than let it
silently vanish into the paved remainder. A route that is 40% dirt, 35% paved and 25%
unsurveyed is a materially different proposition from one that is 40% dirt and 60% paved, and
the rider is entitled to know which they are looking at.

Do not "improve" this by inferring surface from `highway=track` outside the adapter layer.
The ORS adapter already treats an untagged `track` as unpaved, which is a defensible
wire-format reading; guessing anywhere else would inflate the headline statistic.

## Planning pipeline

The five stages are distinct backend concerns — keep them in separate modules so each is testable
without the others. Stages 2–4 are the slow, LLM-touching path and run only on explicit **Replan**
(see Interaction model); stage 1 and mouse-driven leg edits stay synchronous.

1. **Parameters** — start, end, duration, must-see points. Settable by chat, map click, or form.
2. **Route search** — the core differentiator. Build a candidate route that *prefers* twisty and
   unpaved segments rather than merely tolerating them. Split the trip into legs and route each
   with the provider its policy resolves to (see Routing architecture) — Google Directions for
   on-road connectors, ORS for unpaved and adventure segments. Stitch legs into one continuous
   geometry; leg boundaries are where stitching bugs live, so test them directly.
3. **Discovery** — LLM-driven search for camps, wild camping, hotels, unique stays, food, gas, and
   viewpoints along the corridor. LLM output is *candidates only* and must be treated as
   unverified: it will hallucinate coordinates and invent places.
4. **Enrichment** — resolve each candidate against Google Places to get a real `place_id`, photos,
   ratings, and reviews for the detail dialog. A candidate that fails to resolve does not get
   pinned on the map.
5. **Export** — GPX for motorcycle GPS units. Emit both a track and ordered waypoints; test against
   the target device families' quirks (Garmin's point-count limits in particular).

## LLM layer

Tool calls execute server-side; the frontend never holds the OpenAI key. Stream responses so the
map can update as the assistant works.

Every tool the assistant exposes must be a thin wrapper over the same service function the REST
endpoint calls — the mouse path and the chat path must not diverge in behavior. Pin the model in
config, never inline. LLM-proposed geography is always validated against a real routing or places
API before it reaches the map.

## Interaction model: fast path vs. slow path

The app has two speeds, and **they must never block each other**. This is the central frontend
constraint.

**Fast path — synchronous, target sub-second.** Mouse edits: dragging the route, adding or removing
a via-point, reordering waypoints, pinning a POI. These hit the routing engine directly for the
*affected leg only* and touch no LLM. Nothing on this path may await a GenAI call. Optimistically
render the drag locally and reconcile when the leg response lands.

**The fast path is the main quota risk.** ORS's free tier is ~2,000–2,500 requests/day, and naive
per-frame re-routing during a drag would exhaust it in a single session. Three non-optional
mitigations: throttle live updates per provider (see Drag-to-reroute), debounce rapid discrete
edits, and cache legs keyed on rounded endpoints plus policy so undo/redo and repeated drags are
free. Treat quota as a design constraint on the interaction, not an ops detail — and instrument
request counts from day one so the ceiling is visible before it's hit.

**Slow path — explicit, user-triggered.** LLM route search, discovery, and enrichment (pipeline
stages 2–4) run only when the user presses **Replan**. Never fire them automatically on route edit.
Stream progress so the map fills in incrementally rather than freezing behind a spinner, and keep
the map fully interactive while it runs.

Because the two paths decouple, the route can drift out of sync with the POIs discovered for an
earlier version of it. Track a dirty flag when the geometry changes after the last plan and surface
it on the Replan button — stale suggestions the user can't detect are worse than no suggestions.
Replan should be incremental where possible: preserve user-pinned POIs and untouched legs rather
than discarding the whole plan.

### Drag-to-reroute

Google's `DirectionsRenderer` supports `draggable: true` only for routes *Google* computed. Routes
from a non-Google provider are custom polylines, so **drag-to-reroute must be implemented by hand**:
drag handle → insert a via-point at the nearest point on the line → re-request the affected leg
only → splice the new geometry in. Budget real time for this; it is the single most demanding piece
of frontend work in the plan, and it is what makes the app feel like Google Maps rather than a form.
Re-request per leg, never for the whole route — whole-route recompute is what makes editors feel
sluggish.

**Live update cadence is throttled per provider, not globally.** Cheap engines refresh during the
drag; expensive ones hold off. The interval is a tunable declared alongside the provider's other
capabilities (`live_update_interval_ms`) so it moves with the adapter and can be retuned from config
without touching interaction code. Starting values, all subject to change once real usage data
exists:

| Provider | Interval | Rationale |
|---|---|---|
| `google` | ~1000 ms | Cheap per request; near-live feedback is affordable |
| `ors` | ~3000 ms, or preview-only | Free tier is the binding constraint |
| `fake` | 0 | Tests shouldn't wait |

For an expensive provider, "preview-only" is a legitimate setting: rubber-band a straight line from
the drag handle during the gesture and issue no request at all until release. Make that a config
choice, not a code fork.

Four rules the implementation must honor:

- **Drag-end always fires.** The release request is authoritative and unconditional, even if a
  throttled update just returned. This is what guarantees every point is properly connected — mid-
  drag results are previews and are never the final geometry.
- **Tag every request with a monotonic sequence number and discard stale responses.** Out-of-order
  arrivals are the classic drag-interaction bug: a slow earlier request landing after a fast later
  one silently reverts the user's edit. Abort superseded requests in flight — it saves quota too.
- **Mid-drag results are preview state.** They never persist to Cloud Storage, never enter undo
  history, and never set the Replan dirty flag. Only the drag-end commit does.
- **Throttle, not debounce, during the gesture.** Pure debounce shows nothing until the user pauses;
  a leading-edge throttle with a trailing call keeps the line alive while the drag is in motion.
  Debounce is still correct for discrete rapid edits (repeated waypoint add/remove) outside a drag.

POI pins need distinct iconography per category and a right-click "add to route" action.

## External API constraints

- **Places caching is contractually limited.** Google's terms let you store `place_id`
  indefinitely; most other Places content may not be cached beyond a short window. Cache
  `place_id` in the trip document and re-fetch display fields on load. Verify the current terms
  before building anything that depends on longer retention.
- **Don't mix map providers.** Drawing your own polylines on a Google basemap is fine. Rendering
  Google-derived content on a non-Google basemap is not. This is why Mapbox routing was ruled out.
- **BDR tracks are licensed content.** Do not vendor Backcountry Discovery Routes GPX files into
  the repo. Reference them, or derive routing preferences from OSM `surface`/`tracktype`/
  `smoothness` tags.
- Every external call is billed per request. Cache aggressively where terms allow, and make the
  test suite hermetic — no test hits a live API.

## TDD

Tests first, then implementation. This is a hard requirement, not a preference.

- Backend: `pytest`. No test touches a live API. Routing tests use `FakeProvider`; commit recorded
  fixtures for ORS, Google, Places, and OpenAI responses rather than generating them at test time.
  Every provider adapter is tested against a shared contract-test suite so adapters stay
  interchangeable.
- Frontend: Vitest + React Testing Library.
- Type checking is part of the definition of done: `mypy --strict` on backend, `tsc --noEmit` on
  frontend. Full annotations on all Python signatures.
- Ruff for lint and format (the `.gitignore` already anticipates it).

The highest-value tests, given where the bugs will actually be: multi-engine leg stitching, slug
validation, GPX output correctness, the drag-to-reroute splice, stale-response rejection under
out-of-order arrivals, drag-end always committing, and replan preserving user-pinned POIs across a
re-run. Drive throttle tests with an injected clock — never `setTimeout` and a real wait.

## Commands

```
make install        # uv sync + npm install
make dev            # backend :8000 (offline mode) + frontend :5173
make check          # lint + typecheck + test — everything CI runs
make test           # pytest + vitest
make lint           # ruff check + eslint
make typecheck      # mypy --strict + tsc --noEmit
make fmt            # ruff format + --fix
make deploy         # Cloud Build -> Cloud Run

cd backend  && uv run pytest tests/routing/test_ors.py::TestOrsContract   # single backend test
cd frontend && npx vitest run -t "drag end"                               # single frontend test
```

Python dependencies are managed with **uv** — `uv sync`, `uv add`, `uv run`. Never `pip` or a
hand-rolled venv; `uv.lock` is committed and `--frozen` is used in the container build.

`MOTOROOTER_OFFLINE=1` registers only `FakeProvider`, so the app and the whole test suite run
with no API keys. `make dev-backend` sets it by default.

Secrets (OpenAI, Google Maps, routing provider keys) come from Secret Manager in Cloud Run and a
gitignored `.env` locally. The Maps JS API key is necessarily public — restrict it by HTTP referrer
and by API, and keep it distinct from the server-side keys.

## Licensing

GPL-3.0. New dependencies must be GPL-3.0-compatible.

**Do not add per-file GPLv3 headers.** The `LICENSE` file is sufficient, and no file in the
tree carries one — adding them to new files only would make the codebase inconsistent.
