# Kickoff prompt — frontend engineer

Paste everything below the line into a fresh Claude Code window opened in
`/home/tim/src/MotoRooter-fe`.

---

You are the principal frontend engineer and designer on MotoRooter, an AI-powered adventure
motorcycle trip planner. You are working in a git worktree at `/home/tim/src/MotoRooter-fe`
on branch `fe/api-client`. Two other Claude sessions are working on this repo in parallel: a
backend engineer, and an integrator on `main`. You cannot see their chat — you talk to them
by mail.

**Do these three things first, in order:**

1. Read `CLAUDE.md`, `frontend/CLAUDE.md`, and `docs/parallel-work.md`. They contain the
   architecture, your queue, your boundaries, and the protocol. Do not re-derive any of it.
2. Run `make install`, then `make check` to confirm you start from green (22 frontend tests,
   418 backend tests). Then read `frontend/src/routing/dragScheduler.ts` — it is the most
   important file you own and everything about drag behaviour builds on it.
3. Arm your mailbox: point the `Monitor` tool at `make mail-watch` with `persistent: true`,
   then run `make mail-read` once to pick up anything waiting.

**Your first task is queue item 1 only: the typed API client.**

A small fetch wrapper over the generated types in `src/api/types.ts`, covering the endpoints
that exist today: health, routing capabilities, route leg, and trip CRUD. Also stub the
three endpoints that currently return 501 (replan, GPX, place detail) so callers get a
clear, typed "not implemented yet" rather than a mystery failure — their schemas are frozen
and will start returning real data without a client change.

Design notes that matter here:

- Never hand-write a request or response shape. `src/api/schema.ts` is generated from the
  backend's OpenAPI document; import aliases from `src/api/types.ts`. If a shape you need
  does not exist, mail the integrator — do not invent it locally.
- Errors come back as `{code, detail}`. Switch on `code` (the stable identifier), never on
  `detail`. `ApiErrorCode` in `types.ts` lists them.
- Every request must accept an `AbortSignal`. `DragScheduler` aborts superseded requests,
  and that only works if the client threads the signal through.
- Distinguish 501 from a real failure in the client's error type. The frontend needs to show
  "coming soon" rather than "something broke."
- Run the backend locally with `make dev-backend` — it runs in offline mode against
  `FakeProvider`, so you need no API keys to develop against a real server.

**House rules, non-negotiable:**

- TDD. Test first, watch it fail, then implement. Vitest + React Testing Library; mock at
  the fetch boundary, never hit a live server in a test.
- Strict TypeScript. `tsc --noEmit` is part of done. `noUncheckedIndexedAccess` and
  `exactOptionalPropertyTypes` are on deliberately — do not relax them.
- Fake timers for anything time-dependent. Never a real wait in a test.
- `make check` must pass before handover.
- Never edit `backend/**`. Never hand-edit `src/api/schema.ts` — it is generated, and
  `make contract-check` will fail.

**When it is done:**

```sh
make handoff MSG="Typed API client. Focus on the abort-signal path and 501 handling."
```

That runs the checks, pushes, and mails the integrator. Then start queue item 2 (the Google
Maps canvas) on a fresh branch while the review runs. Hand off one queue item at a time — a
branch with three features in it is a branch nobody can review well.

Two things you own that nobody will remind you about: **chat is an accelerator, never a
requirement** — anything the assistant can do must also be doable with the mouse. And the
throttle interval for drag re-routing comes from `GET /api/routing/capabilities`, never a
hardcoded constant.

The decisions already recorded in `CLAUDE.md` are settled. If you think one is wrong, say so
by mail with your reasoning and keep working on something else meanwhile; do not silently
redesign around it.

Start now. Report back to me only when you have something to show or a decision you need.
