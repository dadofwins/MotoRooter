# MotoRooter

An AI-powered trip planner for adventure motorcycles. Describe a ride in plain language *or*
build it entirely with the mouse; MotoRooter finds a route that prefers twisties and dirt rather
than merely tolerating them, discovers places worth stopping at along the corridor, and exports
GPX for a motorcycle GPS.

**Chat is an accelerator, never a requirement.** Every action the assistant can take is also
reachable with the mouse. That is a design rule rather than an aspiration — a tool the assistant
gains owes a UI affordance in the same change, and there is a test that fails when one is
missing.

---

## What it does

**Routes for the ride, not the arrival.** Three rider-facing modes — Fast, Twisties, Offroad —
chosen **per segment**, not per trip. A trip is a list of legs and each leg carries its own
policy, so Woodinville → Cashmere can be highway out, dirt over the pass, and twisties home.
Different engines serve different legs: Google for tarmac, OpenRouteService for anything unpaved.

**Tells the truth about surface.** Routes report dirt, paved and **unsurveyed** as three separate
figures. Unsurveyed is not folded into paved — an OSM audit of WABDR Section 3 found 25% of its
distance carries no surface tag at all, and on a real mixed trip 40% of the route was unsurveyed,
the largest single share. A route that is 40% dirt, 35% paved and 25% unknown is a materially
different proposition from one that is 40% dirt and 60% paved.

**Finds places in four stages, and verifies every one.** Web search (Brave) → an LLM naming the
places a result is *about* → Google Places resolving each claim into a real `place_id` and
coordinate → scoring from computed metrics plus judgement. Nothing unverified reaches the map:
the model may name a place, never place one.

**Estimates duration honestly.** Hosted ORS routes dirt through a bicycle profile and reports
bicycle times — eight hours for 133 km. Whether an engine's duration can be believed is a
declared capability, resolved per leg, so Google's car times are used where they are right and a
surface-weighted model is used where they are not.

**Exports for a real device.** Track plus ordered waypoints, with discovered places carrying the
judge's own reason so a rider knows on the device *why* somewhere is on the list. Long routes are
decimated with Ramer–Douglas–Peucker rather than truncated, because a hairpin is the shape that
matters and even sampling drops it as readily as a redundant straight.

---

## Requirements

- **Python 3.13** and [uv](https://docs.astral.sh/uv/) — never `pip`, never a hand-rolled venv
- **Node 22**
- API keys: Google Maps, OpenRouteService, OpenAI, Brave Search

MotoRooter runs without any keys at all in offline mode; see below.

---

## Install

```sh
git clone git@github.com:dadofwins/MotoRooter.git
cd MotoRooter
make install          # uv sync + npm install
```

## Run it

### Offline, no keys

```sh
make dev-backend-offline    # in one terminal
make dev-frontend           # in another
```

Registers only `FakeProvider`, so the whole app and the entire test suite run with no
credentials. **Routes are straight lines between waypoints** — fine for exercising the UI,
useless for judging whether a route looks right.

### With real providers

Create `backend/.env`:

```sh
ORS_API_KEY=...
OPENAI_API_KEY=...
BRAVE_SEARCH_API_KEY=...
GOOGLE_MAPS_SERVER_KEY=...       # server-side: Places, Directions, Geocoding
GOOGLE_MAPS_BROWSER_KEY=...      # referrer-restricted; used only to build photo URLs
```

and `frontend/.env.local`:

```sh
VITE_GOOGLE_MAPS_BROWSER_KEY=... # same value as GOOGLE_MAPS_BROWSER_KEY
VITE_GOOGLE_MAPS_MAP_ID=...      # a vector Map ID from the Cloud console
```

Then:

```sh
make dev              # backend on :8000, frontend on :5173
```

Open **http://localhost:5173** — `localhost`, not `127.0.0.1`, as Vite binds IPv6.

Both files are gitignored. If `make dev` refuses to start, something is already listening on
:8000 — usually a server left running in another worktree, which will serve the UI happily while
being older than your branch.

### Two keys, not one

This matters and is easy to get wrong. **A referrer restriction only works for calls made from a
web page**, so the same restriction that makes a browser key safe makes it useless server-side.

| | restriction | APIs |
|---|---|---|
| **Browser key** — in the page, in photo URLs, public by construction | HTTP referrers: your origins | Maps JavaScript, Places |
| **Server key** — never leaves the backend | none, or IP | Places, Directions, Geocoding |

If only one is configured the app still starts, logs a warning naming the variable to set, and
publishes the server key into photo URLs. The warning going quiet is how you know the split took.

---

## Using it

**Start a trip.** Name it at the front door, then either type a place name or click the map. A
name that matches several real places offers you the candidates rather than guessing — choosing
among verified places is judgement, inventing a coordinate is not.

**Build the route.** Click to place points in *Add points* mode; switch to *Browse* when you want
to read the map without adding to it. Drag the line to reshape a leg. Right-click a point to
remove it, or the line to split a leg in two — which is how you give each half its own riding
mode.

**Ask for what you want.** *"Three days out of Leavenworth, as much dirt as possible"*, or *"find
me somewhere to camp near the pass"*. The assistant uses the same operations the mouse does.

**Find places.** The button runs discovery over the whole route; the category chips narrow it,
which makes the run cheaper as well as more relevant. Click a place for photos, ratings and
reviews. Route through the best of them in one action, or add them one at a time.

**Export.** Download GPX and load it on the unit.

---

## Deploy

One Cloud Run service, not two: Vite compiles to static files, which the Python image serves
alongside the API. One origin, so no CORS.

```sh
gcloud config set project <your-project>
infra/bootstrap.sh --dry      # see what it would create
infra/bootstrap.sh            # APIs, bucket, Artifact Registry, secrets, IAM
make deploy
```

`bootstrap.sh` is idempotent and reads the five keys from `backend/.env` into Secret Manager
without printing them. Afterwards, **add the deployed URL to the browser key's referrer list** or
the map is refused on the new origin — which looks like a broken key rather than a missing
referrer.

**The deployed service is unauthenticated and every trip is world-readable and world-editable by
link.** That is the prototype's design, not an oversight, but it means anyone with the URL can
spend your API quota. Decide deliberately before sharing it.

---

## Development

```sh
make check       # everything CI runs: ruff, mypy --strict, contract-check, pytest, vitest, tsc
make test        # pytest + vitest
make fmt         # ruff format + --fix
make contract    # regenerate shared/openapi.json and frontend/src/api/schema.ts
```

**Tests first, then implementation.** No test touches a live API; routing uses `FakeProvider` and
recorded fixtures. `mypy --strict` and `tsc --noEmit` are part of the definition of done.

### The contract

`backend/src/motorooter/api/schemas.py` is the seam. It generates `shared/openapi.json`, which
generates `frontend/src/api/schema.ts`. Never hand-edit the generated files; `make contract-check`
fails when they drift.

Two guards exist because this project produced **ten** components that were correct, tested, green
and reachable from nothing — a class of defect no diff can show:

- Every **response** field must be read by app code or listed as deliberately unread **with a
  reason**, and an entry whose field *is* read fails, so a stale exemption cannot survive.
- Every **request** field and query parameter must be sent, or written off the same way.

They are tripwires rather than abstractions: they cannot know whether a field *should* be used,
only that somebody decided.

### Layout

```
backend/    FastAPI app, routing, discovery, the LLM tool layer, GPX
frontend/   Vite + React + TypeScript
shared/     the generated OpenAPI document
infra/      Dockerfile, Cloud Build config, bootstrap
docs/       milestones, parallel-work protocol, measurements
scripts/    one-off measurement harnesses, kept because re-deriving them is expensive
```

`CLAUDE.md` holds the architecture and, more usefully, the reasoning and the measurements behind
the decisions — including several that were wrong the first time and why.

---

## Licence

GPL-3.0. New dependencies must be GPL-3.0-compatible.

Both hosted free tiers in use here are **non-commercial only**, and Brave's terms forbid caching
search results. Revisit before any public launch.
