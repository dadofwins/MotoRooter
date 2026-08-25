# backend/CLAUDE.md

Guidance for work inside `backend/`. The root `CLAUDE.md` holds shared architecture and
still applies — read both.

## Start of session: arm your mailbox

You cannot see the other sessions' chat. You talk to them by mail.

Before doing anything else, point the `Monitor` tool at `make mail-watch` with
`persistent: true`. Incoming messages then arrive as notifications while you work. Then run
`make mail-read` once to pick up anything waiting.

When a branch is ready, hand off in one command — it runs `make check`, pushes, and mails
the integrator, and refuses to proceed if checks fail:

```sh
make handoff MSG="what changed and what the reviewer should focus on"
```

Reply to a review with `scripts/mail send integrator "<subject>"`, body on stdin. Your role
(backend) is inferred from your branch prefix; nothing to configure. Full protocol is in
`docs/parallel-work.md`.

## Your role

You are the principal backend engineer on MotoRooter. You own everything under `backend/`
except the API contract (see Boundaries). Work on `be/*` branches in your own worktree.

## What is already built

The routing layer is complete and green: domain models, `RoutingProvider` protocol, shared
adapter contract suite, `FakeProvider`, ORS and Google adapters, polyline codec, registry,
policy resolver, caching/retry/quota decorators, config factory. Trip models, slug
validation, `TripStore` protocol with an in-memory implementation, and the REST API surface
also exist.

Read `src/motorooter/routing/` before adding a provider — the patterns there are the house
style, and `tests/routing/contract.py` is the bar every adapter must clear.

## Your queue, in dependency order

1. **`GcsTripStore`.** Cloud Storage at `trips/<slug>/trip.json`. Must pass
   `tests/trips/store_contract.py` unchanged — both `TripStoreContract` and
   `TripStoreRoundTripContract`. The round-trip tests are the ones that matter: JSON
   serialization is where tuple-vs-list drift and timezone loss creep in. Do not write to
   the container filesystem; Cloud Run's disk is ephemeral and per-instance.
2. **Leg stitching service.** Turn a multi-intent trip into one continuous geometry by
   routing each leg through its resolved provider and joining them. Leg boundaries are
   where the bugs live — test them directly, including the case where two adjacent legs use
   different engines and their endpoints do not exactly coincide.
3. **GPX export.** Track plus ordered waypoints. Garmin units have point-count limits;
   decimate rather than truncate, and test the limit explicitly.
4. **LLM tool layer.** OpenAI, server-side tool execution, streaming. Every tool must be a
   thin wrapper over the same service function the REST endpoint calls — the mouse path and
   the chat path must not diverge. Pin the model in config, never inline.
5. **Discovery and Places enrichment.** LLM output is *candidates only*. It will invent
   coordinates. Nothing reaches the map without resolving to a real `place_id` — the `Poi`
   model already enforces that unverified suggestions cannot be pinned to the route.

## Boundaries

**`src/motorooter/api/schemas.py` is the frontend contract.** Changing it regenerates the
TypeScript the frontend compiles against, so a change there can break their build. Do not
edit it unilaterally: propose the change, get integrator sign-off, then run `make contract`
and commit the regenerated `shared/openapi.json` and `frontend/src/api/schema.ts` in the
same commit.

Adding a *new* endpoint with new schemas is fine and does not need sign-off. Changing or
removing an existing shape does.

Never edit anything under `frontend/`.

## Git rules

- **Commit and push freely on your own branch.** That part is yours.
- **Never merge to `main`, and never push to `main`.** The integrator merges, after review,
  and only after telling Tim. If you think something must land immediately, say so by mail.
- **Rebase on `origin/main`, never merge `main` into your branch.** Linear history keeps
  review diffs legible. `make handoff` refuses to run if your branch is behind, so fetch and
  rebase when it tells you to.
- **One queue item per branch.** Finish it, hand off, start the next on a fresh branch while
  the review runs. A branch with three features in it is a branch nobody can review well.
- Do not rewrite history on a branch you have already handed off — the reviewer is looking
  at those commits.

## House rules

- **TDD, no exceptions.** Test first, watch it fail, then implement. Every one of the 418
  existing tests was written this way.
- `uv` for everything Python. Never `pip`, never a hand-rolled venv.
- `make check` must pass before you hand anything over: ruff, `mypy --strict`,
  `contract-check`, and the full suite.
- No test touches a live API. Use `FakeProvider` and recorded fixtures.
- Injected clocks for anything time-dependent — see `motorooter/clock.py`. Never a real
  `sleep` in a test.
- Provider names appear only inside their own adapter module. Dispatch on capability.
- Misconfiguration raises `RoutingConfigError` at startup, not on first request. A policy
  that would route dirt through a paved-only engine must fail the deploy.

## Known limitation worth fixing

Hosted ORS has no motorcycle profile, so dirt intents currently route through
`cycling-mountain` — it reaches tracks a car profile refuses, but applies bicycle access
rules. Before building much more on top, spike one known dirt section (a WABDR or OBDR
segment) and look at whether the result is something you would actually ride. If it is not,
self-hosted ORS with a custom moto profile is the fix, and `RoutingSettings(ors_base_url=)`
already supports pointing at one.
