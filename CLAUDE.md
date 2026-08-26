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

**Built and merged.** Backend: the routing layer end to end (models, provider protocol, shared
adapter contract suite, `FakeProvider`, ORS and Google adapters, polyline codec, registry,
policy resolver, caching/retry/quota decorators, config factory). Trip and POI models, slug
validation, `TripStore` with in-memory and Cloud Storage implementations, compare-and-swap on
writes. Leg stitching, the trip-level router, `RouteFingerprint`. The REST API with generated
TypeScript types and an `ErrorCode` union. Rate limiting that separates "too fast" from "budget
spent". Route metrics (twistiness, detour ratio, distance-to-route) with thresholds pinned to
real geometry. The LLM tool-calling core. Discovery end to end — corridor anchors, Brave search,
LLM extraction, Places resolution, category-from-Places, and the judge — behind `POST
/api/trips/{slug}/replan`. Derived durations and POI detail.

Frontend: typed API client, `DragScheduler`, the Google Maps canvas, drag-to-reroute complete
(throttled, handle-only feedback, stale-response rejection, guaranteed drag-end commit). POI
pins with add-to-route and the Places detail dialog. Surface summary. Miles by default with a
km toggle, and time estimates. Trip lifecycle — save, load, share by link — and the front door:
a per-browser recent-trips list and a create path that carries the rider's chosen name.

Discovery runs in **19.1 s live** (was ten minutes and stalling), and the front door
auto-opens a returning rider's only trip with New trip always reachable.

**Shelved, not rejected:** `be/road-expansion` (roads-as-leads). Measured as finding *fewer*
POIs than baseline for 1.4–2.3× the searches — but against a funnel discarding 84% of
candidates upstream, so the number says nothing about the mechanism, which demonstrably works.
Re-measure after anchor naming is fixed. Do not review or merge it before then.

**In flight:** `be/anchor-naming` (the Stage 0 fix — the highest-value item in the backend
queue), `fe/multi-leg-structure`.

**Stubbed with frozen schemas** (501, so the frontend builds against real shapes): GPX export,
and the chat endpoint `POST /api/trips/{slug}/chat`.

**Not built:** the chat rail — the last M1 item with nothing behind it. Multi-leg trips: a trip
is still one leg spanning every waypoint, which blocks per-segment routing modes and is why
drag latency is a whole-route recompute. `Trip.default_intent`, forward geocoding, the settings
dialog, GPX export.

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
| Web search | Brave Search API (discovery stage 1) |
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

## Discovery architecture

Discovery has **four** stages, and they answer different questions with different tools.
Mixing them is the main way this goes wrong.

```
  SEARCH            EXTRACT              RESOLVE                JUDGE
  Brave web search  LLM reads snippets   Google Places lookup   metrics + LLM
  what is written   which PLACES are     does it exist, where   how good is it,
  about this area   named in that text   exactly, is it open    worth the detour
```

**Why four and not three.** The original design went straight from search to Places, and a
spike on a real corridor showed it yields almost nothing: search returns pages *about* places,
not places. Twenty results from Chinook Pass gave "Camping Near Chinook Pass | Free Campsites
Near You", "The Essential Guide to the Chinook Scenic Byway", a Reddit thread, and an article
about Northern California. Feeding those titles to Places resolves to nothing — or worse, to
something real with the wrong name.

The information is there, but in the *snippet* rather than the title: "These dispersed camping
sites are located outside of the Halfway Flat Campground area..." names a real place a title
never would. So a stage reads the prose and names the places in it.

This fits the rule rather than bending it: pulling a place name out of a paragraph is not
measurable, it is precisely a language task, and a far narrower question than scoring. It has
a natural guard — **the model may only name places whose names appear in the text it was
given**, so it cannot invent a campsite, and every extraction is checkable against its source.
Cost is one call per result batch, not per candidate, so it is not a fan-out multiplier.

**1. Search — Brave.** This is the only source for the half of the product that matters most.
Places knows a restaurant exists and its rating; it does not know a road is a great
motorcycle road. That knowledge lives in ride reports, forum threads, BDR guides and blogs,
and web search is the only way to reach it. Queries are generated per corridor and per
category: "best motorcycle roads near <pass>", "wild camping <forest> BDR", "<town>
motorcycle friendly hotel".

**2. Extract — LLM over snippets.** Names the places a search result is *about*, constrained
to names present in the text so it cannot invent one. Grounded is not the same as useful,
though: one live snippet produced eight grounded names, most of them the geography the real
place sits *inside*, and a national forest pinned on a map is worse than nothing. Prefer
deterministic pruning — drop the place we searched for, collapse duplicates, cap the count —
and reach for the prompt only for what those cannot express.

**Relevance is NOT decided here.** From text alone the finest available judgement is "in the
right state", and a corridor is tens of metres wide. A Cayuse Pass query returned Miller Peak,
Stafford Creek and De Roux Horse Camp: real, correctly grounded, correctly Washington, and
100 km away in the Teanaway. The model has no way to know that. Relevance is a distance check
against the route **after** resolve, because it needs coordinates and only Places produces
them — which turns a judgement into arithmetic, as the rule requires.

**3. Resolve — Google Places.** Search results and model output are both *claims*. Places
turns a claim into a real `place_id` with real coordinates, hours and rating. Nothing reaches
the map unresolved — the `Poi` model already refuses to pin an unverified LLM suggestion to
the route. A candidate that will not resolve is dropped, not guessed at.

**4. Judge — computed first, LLM second.**

> **Measure what is measurable; ask the model only what is not.**

This is the rule that keeps discovery cheap, deterministic and testable. A great deal of
"how interesting is this road" is arithmetic on geometry we already have:

| Signal | How |
|---|---|
| Twistiness | Summed absolute heading change per km, from the leg geometry |
| Elevation gain/loss | From ORS elevation, already requested |
| Surface mix | `SurfaceSpan`s, already parsed |
| Detour cost | Added distance and time versus the direct line |
| Remoteness | Distance to the nearest fuel POI |

None of that needs a model, and a model would be slower, non-deterministic, and capable of
being confidently wrong about a number it could have computed. Compute them, test them,
and feed them to the LLM as *evidence*.

The LLM then judges what genuinely needs judgement: is this scenic, is it locally famous, is
the detour worth it for this rider, does the ride report say it washes out in spring. It
receives the computed metrics and the search snippets and returns a score with a reason —
never a coordinate it invented, and never a number it could have been given.

### Stage 0: naming the anchor. This is where discovery quality is decided.

Before anything can be searched, a coordinate has to become a *search term*. That prerequisite
stage turns out to govern everything downstream, and it was measured (2026-08-25,
Ellensburg–Cashmere, 85.6 km) doing enormous damage:

**21 of 25 named candidates were dropped for distance. Median 169 km off route, minimum 59 km.
Not one was within 30 km of the 15 km corridor filter.** Snoqualmie Falls and Bull Run came
back for a route on the other side of the Cascades. The filter was not too tight — nothing was
close. The pipeline was searching the wrong area entirely, and no amount of tuning downstream
recovers from that.

The cause is what a reverse geocode returns for a point on a road:

    (47.1946,-120.9559)  'West Davis Street'
    (47.3873,-120.5483)  '84VX9FP2+WM'
    (47.5210,-120.4630)  'Cottage Avenue'

Two distinct failures, and the second is the general one:

1. **A Plus Code was being handed to web search.** `name_for` fell through to
   `formatted_address.split(",")[0]`, and for a remote point Google returns a plus code with
   `types: ['plus_code']` and nothing else. The function's own docstring already forbade this
   — *"a coordinate in a web query matches nothing and costs a metered search to discover
   that"* — and a plus code is a coordinate.

2. **The real distinction is *distinctive* versus *generic*, not route versus locality.** A
   named pass, parkway, byway or forest road is a genuine search term. "Cottage Avenue" exists
   in every town in America, so search returns pages about anywhere. Preferring `route` over
   `locality` is right on a mountain — `Mather Memorial Parkway` beats `Enumclaw` 50 km away —
   and actively harmful in a valley. It is also why two adjacent anchors on a corridor that
   *is* one long road both came back "Mather Memorial Parkway".

Prune generic names deterministically and fall through to locality; do not ask a model to
judge it. Prefer the Geocoding API's own `types` over string matching on suffixes where you
can — Google already labels a `plus_code`, a `natural_feature` and a `route`, and a suffix
denylist built from English street names will not travel.

**The lesson for anything built on top of discovery:** measure *where candidates are lost*
before optimising how many are generated. Road expansion — following a road to find the places
on it — measured as finding *fewer* POIs than baseline for 1.4–2.3× the search volume, and the
mechanism was not at fault. It was pouring more candidates into a funnel already discarding
five of six upstream.

### Reasoning effort is per-stage, and it is measured

The rule above has a corollary that cost a day to find: **when you do ask the model, ask for
only the thinking the task needs.**

Extraction was the pipeline's bottleneck, and it looked like a timeout problem. It was not.
A batch of fifteen snippets took **35–44 s** at the default reasoning budget and **2.9–3.4 s**
at `reasoning_effort: minimal`, returning the same six places. The slow runs were not more
accurate — one of them proposed "Washington State, USA", the region it had been handed, as
somewhere to visit. Extraction is constrained to names present in its input, so the model is
copying, not deciding, and there is nothing for reasoning to add.

Judging measured the *opposite*, which is why this is a per-stage setting and not a global
one. The same candidate scored 0.90 at the default, 0.65 at `minimal`, 0.45 at `low`. There
the thinking is the answer, and a threefold speedup that reorders the list is not a speedup.

So: **measure each stage before setting its budget, and record the numbers next to the
constant.** Two stages of one pipeline wanted opposite settings, and guessing was wrong in
both directions — the timeout was raised from 8 s to 25 s on measurement and still failed,
because the measurement answered the wrong question. A timeout below normal latency does not
fail fast, it fails always: the run completes, reports no error, and returns nothing.

### Building it

Mirror the routing layer, which exists and works:

- A `DiscoverySource` protocol with adapters (`brave`, `places`, `llm`), no source name
  outside its own module, and a shared contract test suite every adapter passes.
- Retry and quota decorators, as routing has. **Caching is not uniform here, and cannot be**
  — see the licensing constraint below. Discovery fans out far more requests per user action
  than routing does, so retry-with-backoff is what absorbs the 429s that concurrency earns.
  Backoff on `RateLimited`, give up immediately on `QuotaExceeded`; retrying an exhausted
  budget is a storm that looks exactly like the outage it is retrying. **Read the server's
  retry hint before guessing** — Brave returns `x-ratelimit-reset` in seconds, and blind
  exponential backoff sleeps far longer than a per-second window needs. Exponential is the
  fallback for when the header is missing, not the default.
- **Measure limits against the real key, not the published tier.** Brave documents the free
  tier at one request per second; our key reports `50;w=1`. Either number would have been
  quoted confidently from the wrong source, and one of them makes `DEFAULT_CONCURRENCY = 6`
  a guaranteed 429 generator.
- **Deduplicate queries before issuing them.** This, not caching, is what reduces request
  volume — and it is unaffected by licensing. Adjacent anchors routinely reverse-geocode to
  the same road name and then issue byte-identical searches: both anchors on the live Chinook
  corridor came back "Mather Memorial Parkway" and searched the same three queries twice.
- Hermetic tests. Recorded fixtures for Brave, Places and OpenAI; no test touches a live API.

### Constraints

- **Places caching stays limited to `place_id`.** Unchanged by any of this.
- **Brave forbids caching search results. Checked, 2026-08-25.** The Search API terms
  prohibit customers from "store[ing], cach[ing], or creat[ing] a database of Search Results,
  in whole or in part, other than transient storage required for operation of Customer
  Applications". No retention window, no 24-hour allowance — the only permitted storage is
  what serving a request needs. Brave sells plans that grant storage rights explicitly, so
  this is a commercial decision rather than a technical one, and not one to make quietly.
  Caching extraction *output* is ours to keep, but it keys on snippets we may not store, so
  it is not worth the tangle.
- **Every tool the assistant can call must also be reachable by mouse.** Discovery is the
  biggest test of that rule: "find me more restaurants on the route" and a Restaurants button
  must run the same service function, not two implementations that drift.

## Routing modes: the rider-facing vocabulary

Three, named by Tim, mapping onto the existing `LegIntent` rather than a parallel vocabulary:

| shown to the rider | `LegIntent` | engine | reports surface |
|---|---|---|---|
| Fast | `highway_connector` | Google | no |
| Twisties (paved) | `twisty_paved` | Google | no |
| Offroad | `unpaved` | ORS | **yes** |

`technical_offroad` and `manual_track` stay unlabelled for now — the field expresses all five,
so adding a label later is a label, not a migration.

**Do not hardcode which modes report surface.** Read `reports_surface` from
`GET /api/routing/capabilities` and resolve each intent to its provider. A hand-kept list
goes stale the day the policy table repoints an intent, which has already happened once and
produced an entirely grey route. The picker should tell a rider *at the moment of choosing*
that Fast and Twisties cost them the dirt/paved/unsurveyed breakdown.

**Mode is per-leg, not per-trip.** `Trip.default_intent` seeds new segments;
`TripLeg.intent` decides how each one actually routes, and a rider can change it per segment
when creating or dragging a point. The routing layer has supported this since the first
branch — "a trip is a list of legs, and each leg carries its own routing policy" — and the
vertical slice's single-leg-spanning-every-waypoint model is the thing standing in the way,
not the architecture.

Splitting into real legs also fixes drag latency: re-routing the affected leg is the design,
and today "the affected leg" is the entire route.

## Surface reporting: unknown stays unknown

`unpaved_fraction` counts only spans explicitly tagged unpaved. `Surface.UNKNOWN` is not
counted as either paved or dirt.

This is a deliberate product decision, not an oversight, and it has a measured cost. An OSM
audit of WABDR Section 3 (`scripts/wabdr_osm_audit.py`, 61 probes over 126 km) found **25% of
the distance carries no `surface` tag at all** and 75% no `tracktype`. So the reported figure
systematically *under*-states how much dirt a route contains — roughly 41% reported against
~48% actual on that section.

**Confirmed against the live stack (2026-08-25).** The first run of the real pipeline over
Cashmere–Blewett Pass (39.4 km ORS `unpaved`, 15 surface spans) came back **34% dirt, 26%
paved, 40% unsurveyed**. Unsurveyed was the *largest single share* — bigger than either real
surface — against the 25% the OSM audit predicted. Whatever else is true, the slice this
project nearly rounded away is the biggest one on a real adventure route. The same run
confirmed the other two live: `twisty_paved` through Google returns zero spans, so the
capability-driven labelling is load-bearing rather than hypothetical; and ORS reported 2.31 h
for 39 km (17 km/h, the bicycle profile) against a derived 45 min, so the displayed figure is
now about a third of what the engine says and is the honest one.

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
