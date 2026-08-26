# frontend/CLAUDE.md

Guidance for work inside `frontend/`. The root `CLAUDE.md` holds shared architecture and
still applies — read both.

## Start of session: arm your mailbox

You cannot see the other sessions' chat. You talk to them by mail.

Before doing anything else, point the `Monitor` tool at `make mail-watch` with
`persistent: true`. Incoming messages then arrive as notifications while you work. Then run
`make mail-read` once to pick up anything waiting.

When a branch is ready: self-review your diff against `docs/self-review.md` first, then
hand off in one command. It runs `make check`, pushes, and mails the integrator, and refuses
to proceed if checks fail, your tree is dirty, or you are behind `origin/main`:

```sh
make handoff MSG="what changed and what the reviewer should focus on"
```

When a handoff body contains backticks, **pipe it on stdin instead of using `MSG=`**. Your
shell expands `MSG="..."` before make sees it, so `` `identifier` `` runs as a command
substitution and is replaced by nothing — a handoff went out with every identifier silently
deleted. A quoted heredoc cannot be mangled that way:

```sh
make handoff <<'EOF'
What changed, with `identifiers` intact.
EOF
```

Do **not** try to invoke the `/code-review` skill — it is user-invocable only and will fail
with `disable-model-invocation`. Self-review means reading your own diff against the
checklist. The integrator runs the real review.

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
- **Rewriting a handed-off branch: rebase yes, revise no.** The two rules above collide, and
  the rebase wins. A rebase onto current `main` rewrites commits the reviewer may be reading,
  but `make handoff` requires it and a review against stale `main` is worth less than a clean
  diff. So: rebase and force-push freely, and **say in the handoff mail that you did and that
  the content is unchanged**. What stays forbidden is *revising* handed-off commits — amending
  their content, squashing, or reordering — because then the reviewer's notes point at code
  that no longer exists. Fixes go on top as new commits; the branch gets tidied at merge, which
  is the integrator's job.

## House rules

- **A hook or factory that returns an object of helpers returns function-typed
  *properties*, never method shorthand, and memoises the object.**

  ```ts
  //  ✗  intervalFor(intent: LegIntent): number | null
  //  ✓  readonly intervalFor: (intent: LegIntent) => number | null
  ```

  Method shorthand declares "this may be used", so destructuring it off the result — which
  every caller does — trips `unbound-method`. Worse, an object literal rebuilt each render
  invalidates anything keyed on it: that is what silently destroyed a whole drag gesture,
  because the session was keyed on a capabilities object that was new every time. Both hooks
  that returned helpers hit this, one as a bug and one as a lint error.

- **Rendering the app to look at it: put your overrides *after* the stylesheet, and check both
  colour schemes.** There is no browser in the test setup, but `google-chrome-stable` is
  installed, so a throwaway test can emit the component tree plus `src/index.css` to a file and
  Chrome can screenshot it headless. Two traps, both of which cost a full attempt each:

  `:root { color-scheme: light dark }` means the *user agent* picks form-control and `Canvas`
  colours from the OS preference. Stripping the dark `@media` block is not enough — the app's own
  `:root` wins the cascade over anything injected before it, and what you get is a plausible
  render of a state that cannot exist: light CSS with dark selects and a black sticky heading.
  Inject `:root { color-scheme: light !important }` **after** the stylesheet.

  **A dumped `innerHTML` loses every value React set as a *property*.** `<select value=…>` is the
  one that bites: React assigns the DOM property, serialisation writes only attributes, so the
  dump shows each select on its *first* option. Both leg pickers rendered "Fast" over intents that
  were `unpaved` and `twisty_paved`, and that was nearly filed as a bug. Checkboxes survive;
  selects, and anything else driven by a property, do not. A harness that lies confidently is
  worse than no harness — so assert the state in a test and use the render for layout and colour.

  **Render over something like the real surface, not over a flat pane colour.** The map is
  satellite imagery and terrain, and a contrast figure computed against `#e8e6e1` describes a
  surface that only exists while the map is loading. The fan's leader lines measured 3.88:1
  against the pane tone and were nearly invisible over striped imagery; the fix was a casing, the
  same trick the pins get from their white ring. A busy background in the harness costs one
  gradient and answers the question that matters.

  And check light as well as dark. **The same alpha that passes over near-black fails over
  white** — `rgb(0 0 0 / 0.5)` is 3.95:1 on white and 5.19:1 on the dark surface — so a palette
  verified in dark mode has not been verified. Nine muted classes failed WCAG AA that way, after
  the dark figures had been computed and found fine.

- **What the real Maps API actually does, checked 2026-08-26.**

  Every map behaviour here is tested against fakes we wrote, and a fake confirms the assumptions
  it encodes. `frontend/scripts/maps-probe/run.py` is the only thing that asks the API itself. It
  cannot be a test — no test touches a live API — so this table is the record. Re-run it before
  trusting a new assumption, and add the answer here.

  | belief | verdict |
  |---|---|
  | `AdvancedMarkerElement` emits `contextmenu` | **FALSE.** It emits `click` and not `contextmenu`, with and without `gmpClickable`. Right-click is taken from the pin's DOM node instead — see `onPinContextMenu`. |
  | the pin is hit-testable at its own screen position | true; `elementFromPoint` lands inside the content |
  | a right-click there carries usable coordinates | true; `clientX`/`clientY` intact |
  | `Polyline` emits `contextmenu` with a `domEvent` | true |
  | `fitBounds` fires `zoom_changed` | true; one event, zoom 12 → 8 |
  | `map.getZoom()` returns a number | true |
  | `marker.position = …` is cheap enough for frame rate | true; 0.008 ms, so a whole eight-pin fan costs 0.7 ms |
  | `google.maps.Marker` (no Map ID) emits `contextmenu` | true. It renders as an `AREA` element, which is why the first attempt — dispatching at a guessed pixel — missed it. |
  | `Polyline` emits `mousedown` with a `latLng` | true; the drag gesture can start |
  | `map.setOptions({ draggable: false })` stops panning | true; the option round-trips |
  | `map` emits `mousemove` and `mouseup` during a drag | true — **but only with panning off**, which is what the canvas does on grab. With panning on, Maps reads a press-and-move as a pan and consumes the moves itself; a probe that skipped that step recorded a failure that was its own. |
  | `map` emits a `click` after a drag | **FALSE.** A trusted press-release that stays put does emit one; a press-move-release does not. Both halves measured, because the absence means nothing without the control. |

  That last row was a live defect. The canvas armed a flag at drag-end to swallow the click it
  expected, so it waited for something that never came and swallowed the rider's *next
  deliberate* click instead — one lost waypoint per drag, silently. It is a timestamp with a
  250 ms window now, which is correct whichever way the API behaves.

  The first row is the one that matters: the entire right-click menu was correct, tested, green
  and did nothing wherever a Map ID is set, which is production. A fake that emitted the event
  hid it, and no diff could have shown it.

- **What the browser's geolocation actually reports, checked 2026-08-26.**

  The permission question is the whole design — a prompt on page load asks for something before
  the app has shown anything worth granting it for. Measured in headless Chrome, with the granted
  case set up over the DevTools protocol:

  | origin and history | `permissions.query` | `getCurrentPosition` |
  |---|---|---|
  | localhost, never asked | `prompt` | prompts |
  | localhost, already granted | `granted` | resolves silently, no prompt |
  | plain `http://` on a LAN address | **`denied`** | "Only secure origins are allowed" |

  The third row is why `browserLocation.ts` has no `isSecureContext` branch: an insecure origin
  already reports `denied`, so the rule for "the rider said no" covers it. Worth having checked —
  `navigator.geolocation` is still *present* there, so a presence test would have said all was
  well right up to a deployment served over plain HTTP. **And localhost is a secure context**, so
  dev never sees the failure.

- **Verify with the exit code, never by reading the output.** `make check` prints ruff's
  "All checks passed!" from the backend step and can still exit non-zero on the frontend
  lint that follows. `make check; echo $?` is the only honest check, and piping it through
  `grep` throws the status away.

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
