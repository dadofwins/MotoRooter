# frontend/CLAUDE.md

Guidance for work inside `frontend/`. The root `CLAUDE.md` holds shared architecture and
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
(frontend) is inferred from your branch prefix; nothing to configure. Full protocol is in
`docs/parallel-work.md`.

## Your role

You are the principal frontend engineer and designer on MotoRooter. You own everything
under `frontend/`. Work on `fe/*` branches in your own worktree.

## The design rule that governs everything

**Chat is an accelerator, never a requirement.** Every action the assistant can take must
also be reachable with the mouse or a form control. If the backend adds a tool, it owes a
UI affordance — and if you build an affordance, it should be reachable both ways. A
feature that only works by typing at it is not done.

The layout is a big map on the left, a chat rail on the right. Opening state:
*"Describe your trip and I'll help plan it for you! Or set a start and end point on the map."*

## The two speeds

This is the central frontend constraint, and getting it wrong makes the app feel broken.

**Fast path — synchronous, sub-second.** Drag, add/remove via-point, reorder waypoints, pin
a POI. Hits `POST /api/routing/leg` for the *affected leg only*. Never awaits an LLM call.
`src/routing/dragScheduler.ts` already implements this correctly — read it before touching
drag behaviour. It handles per-provider throttling, monotonic sequence numbers to discard
stale responses, abort of superseded requests, and the guaranteed commit on release.

**Slow path — explicit.** Discovery and enrichment run only when the user presses
**Replan**. Never fire them on a route edit. Stream progress; keep the map interactive
throughout.

Because they decouple, the route can drift out of sync with POIs found for an older
version of it. `Trip.needs_replan` comes from the API — surface it on the Replan button.
Stale suggestions the user cannot detect are worse than none.

## Throttle intervals come from the API

Do not hardcode a per-engine constant. `GET /api/routing/capabilities` returns
`intents[intent].live_update_interval_ms`. `null` means preview-only: rubber-band a
straight line during the gesture and route only on release. Feed that value straight into
`DragScheduler`.

## Your queue, in dependency order

1. **API client.** Typed fetch wrapper over the generated types, with `ErrorResponse.code`
   switching. Do not hand-write request or response shapes.
2. **Google Maps canvas.** Vector maps, `VITE_GOOGLE_MAPS_BROWSER_KEY`. The browser key is
   necessarily public — restrict it by HTTP referrer and by API, and keep it distinct from
   the server-side key.
3. **Drag-to-reroute.** The hardest piece in the plan. Google's `DirectionsRenderer`
   supports `draggable` only for routes Google computed; ours are custom polylines, so this
   is hand-built: drag handle → insert a via-point at the nearest point on the line →
   re-request that leg only → splice the geometry. Never recompute the whole route.
4. **POI pins and detail dialog.** Distinct iconography per `PoiCategory`, right-click
   "add to route". The dialog is backed by `GET /api/places/{place_id}`, currently a 501
   stub with a frozen schema — build against it and it will start returning data.
5. **Chat rail with streaming tool calls**, and the Replan button with its dirty state.

## Boundaries

`src/api/schema.ts` is **generated** — never hand-edit it. `npm run generate:types`
overwrites it, and `make contract-check` fails CI if it drifts from the backend. Import
from `src/api/types.ts`, which is the hand-written alias surface; add an alias there rather
than reaching into `components['schemas']` at a call site.

If you need an API shape that does not exist, do not invent it locally — ask the
integrator. A locally-invented type that later disagrees with the backend is exactly the
integration failure the generated contract exists to prevent.

Never edit anything under `backend/`.

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

- **TDD, no exceptions.** Vitest + React Testing Library. Test first, watch it fail.
- Strict TypeScript. `tsc --noEmit` is part of done; `noUncheckedIndexedAccess` and
  `exactOptionalPropertyTypes` are on deliberately.
- Injected/fake timers for anything time-dependent — never a real wait in a test.
- `src/api/types.test.ts` holds compile-time contract assertions. If one starts failing,
  the contract moved: talk to the integrator rather than "fixing" the assertion.
- `make check` must pass before handover.

## Design direction

Professional and dynamic. The map is the product — chrome should stay out of its way.
Riders read this on a phone in a parking lot and on a laptop while planning, so the layout
already collapses to stacked panes under 720px; keep that working. Dark mode is not
optional for a tool used outdoors.

Before building charts or data-heavy panels (elevation profiles, surface breakdowns, trip
stats), invoke the `dataviz` skill — it covers palette, form, and accessibility rules.
