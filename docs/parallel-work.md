# Working in parallel

Three Claude sessions work on this repo at once: a backend engineer, a frontend engineer,
and an integrator. This document is the protocol they follow. It is short on purpose —
read it, then get on with it.

## Why worktrees

Two sessions sharing one working directory will clobber each other. There is no file
locking, and neither can see the other's uncommitted edits. Git worktrees give each its
own checkout against the same `.git`, so branches and history are visible to everyone
immediately.

```
/home/tim/src/MotoRooter      main    integrator
/home/tim/src/MotoRooter-be   be/*    backend engineer
/home/tim/src/MotoRooter-fe   fe/*    frontend engineer
```

Setup, run once from the main checkout:

```sh
git worktree add -b be/trip-storage   ../MotoRooter-be
git worktree add -b fe/map-canvas     ../MotoRooter-fe
```

Each engineer then works only inside their own directory. Dependencies are per-worktree, so
each runs `make install` once.

## Ownership

| Area | Owner |
|---|---|
| `backend/**` except `api/schemas.py` | Backend engineer |
| `frontend/**` except `src/api/schema.ts` | Frontend engineer |
| `backend/src/motorooter/api/schemas.py` | **Integrator** |
| `shared/openapi.json`, `frontend/src/api/schema.ts` | **Generated** — never hand-edited |
| Root `CLAUDE.md`, `Makefile`, `infra/**` | **Integrator** |

Nobody edits outside their column. If you need something from the other side, ask the
integrator rather than reaching across.

## The contract

`backend/src/motorooter/api/schemas.py` is the seam. It generates `shared/openapi.json`,
which generates `frontend/src/api/schema.ts`, which the frontend compiles against.

- Adding a **new** endpoint with new schemas: backend engineer may do this freely.
- Changing or removing an **existing** shape: integrator sign-off first. It can break the
  frontend build.
- After any contract change: `make contract`, and commit the regenerated files in the same
  commit as the change. `make contract-check` fails CI otherwise.

If the frontend needs a shape that does not exist, do not invent it locally. A
locally-invented type that later disagrees with the backend is precisely the failure the
generated contract exists to prevent.

## Definition of done

Before handing anything over, in your own worktree:

```sh
make check      # ruff + mypy --strict + contract-check + pytest + vitest + tsc
```

No exceptions. A branch that does not pass `make check` is not ready for review.

## Review protocol

Review is local — there is no `gh` CLI installed, so no pull requests.

**Author:**

1. `make check` passes.
2. Commit and push your branch: `git push -u origin be/trip-storage`.
3. Run `/code-review` on your own diff first and fix what it finds. Do not send the
   reviewer things you could have caught yourself.
4. Tell Tim the branch is ready and what to look at.

**Reviewer** (the other engineer, in their own worktree):

```sh
git fetch origin
git diff main...origin/be/trip-storage
```

Then run `/code-review` scoped to that diff and report findings back through Tim. Findings
live in chat scrollback, so state them compactly: file, line, what breaks, and a concrete
failure case. Vague "consider refactoring" notes are not worth the round trip.

**What each reviewer is looking for.** Review from your own perspective, not the author's —
that is the whole point of cross-review:

- The **frontend engineer** reviewing backend work asks: is the API shape actually usable
  from a component? Are error codes distinguishable? Does anything here force the client
  into an extra round trip, or into holding state the server should own?
- The **backend engineer** reviewing frontend work asks: is the client hammering an
  endpoint the free tier cannot sustain? Is it assuming a response shape the contract does
  not guarantee? Is it treating LLM-suggested data as verified?

**Integrator** does a final pass and merges to `main`.

## Merge order

The contract is frozen, so most work is independent. When both branches touch the same
area, backend lands first — the frontend can adapt to a real API faster than the backend
can adapt to an assumed client.

Rebase on `main` before handover, do not merge `main` into your branch. Linear history
makes the review diffs legible.

## Things that will bite

- **Cloud Run's filesystem is ephemeral.** Trips go to a bucket, never local disk.
- **The ORS free tier is ~2,000–2,500 requests/day** and the drag interaction can exhaust
  it in one session. Throttle, cache, and instrument request counts.
- **Google Places data mostly cannot be cached** beyond `place_id`. `PoiDetail` is
  response-only and has no persistence path — keep it that way.
- **LLM output is candidates only.** It invents coordinates. Nothing reaches the map
  without resolving to a real `place_id`.
- **Everything is public and unauthenticated** in the prototype. Trip slugs become storage
  paths, so slug validation is a security boundary, not formatting.
