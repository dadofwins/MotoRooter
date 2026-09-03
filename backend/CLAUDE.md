# backend/CLAUDE.md

Guidance for work inside `backend/`. The root `CLAUDE.md` holds shared architecture and
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
(backend) is inferred from your branch prefix; nothing to configure. Full protocol is in
`docs/parallel-work.md`.

## Your role

You are the principal backend engineer on MotoRooter. You own everything under `backend/`
except the API contract (see Boundaries). Work on `be/*` branches in your own worktree.

## What is already built

**Everything through M2.** The routing layer (domain models, `RoutingProvider` protocol,
shared adapter contract suite, `FakeProvider`, ORS and Google adapters, polyline codec,
registry, policy resolver, caching/retry/quota decorators, config factory); trips and
persistence including `GcsTripStore`; leg stitching; GPX export; the LLM tool layer with six
tools behind a streaming chat endpoint; and the four-stage discovery pipeline with Places
enrichment. All merged, all green.

Read `src/motorooter/routing/` before adding a provider — the patterns there are the house
style, and `tests/routing/contract.py` is the bar every adapter must clear. Read
`src/motorooter/planning/discovery/` before adding a discovery stage; every stage there owes
counts and a failure line for the reason recorded in the root `CLAUDE.md`.

The root `CLAUDE.md` Status section is the current state of the world. Trust it over this
file if the two ever disagree, and fix this file when they do.

## Your queue

**Assigned by mail, not listed here.** A hardcoded queue in a file nobody rewrites is how
this document came to open with a task that had been merged for a week. The integrator
assigns work to your box; `scripts/queue-status` is what tells them you are idle.

If your box is empty and you have nothing in flight, say so by mail rather than picking
something up — an engineer choosing their own next task is how two branches end up touching
the same area.

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
- **Rewriting a handed-off branch: rebase yes, revise no.** The two rules above collide, and
  the rebase wins. A rebase onto current `main` rewrites commits the reviewer may be reading,
  but `make handoff` requires it and a review against stale `main` is worth less than a clean
  diff. So: rebase and force-push freely, and **say in the handoff mail that you did and that
  the content is unchanged**. What stays forbidden is *revising* handed-off commits — amending
  their content, squashing, or reordering — because then the reviewer's notes point at code
  that no longer exists. Fixes go on top as new commits; the branch gets tidied at merge, which
  is the integrator's job.

## House rules

- **TDD, no exceptions.** Test first, watch it fail, then implement. Every one of the
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

## Known limitation, measured and accepted

Hosted ORS has no motorcycle profile, so dirt intents route through `cycling-mountain` — it
reaches tracks a car profile refuses, but applies bicycle access rules.

**This was spiked and the answer is that it is good enough** (M0, 2026-08-25, WABDR Section 3
via `scripts/routing_spike.py`). At realistic waypoint density the engine puts you on the same
road: 8 intermediate waypoints gave 58% within 100 m of the published track and a 50 m median
deviation; 20 gave 78% and 26 m. Self-hosted ORS with a custom moto profile is deprioritised
until something else forces it, and `RoutingSettings(ors_base_url=)` still supports pointing
at one.

Two consequences of that profile choice are permanent and live in the code rather than here:
its durations are bicycle times, so `reports_trustworthy_duration` is false for ORS and
`speeds.py` derives the figure instead; and its elevation lookup emits an exact `0` on
failure, which the adapter filters against the route's own median.
