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

## Talking to each other

The three sessions cannot see each other's chat. They communicate through a **mailbox**:
a directory outside every worktree (worktrees are separate directories, so anything inside
one is invisible to the others). It lives at `../motorooter-mail/` and is not in git —
it is coordination state, not source.

```sh
scripts/mail whoami            # your role, inferred from the branch prefix
scripts/mail send <box> <subj> # body on stdin; boxes: integrator, backend, frontend
scripts/mail read  <box>       # print unread and archive them
scripts/mail peek  <box>       # print unread without archiving
scripts/mail watch <box>       # one line per new message — for the Monitor tool
```

Your role is derived from the branch: `be/*` → backend, `fe/*` → frontend, `main` →
integrator. Nothing to configure.

**Arm your mailbox at the start of every session.** Point the `Monitor` tool at
`make mail-watch` with `persistent: true`. Each incoming message then arrives as a
notification while you keep working — you never poll, and you never miss a handoff.

Messages are archived rather than deleted, so `../motorooter-mail/<role>/archive/` is the
record of what was asked and why.

## Review protocol

Review is local — there is no `gh` CLI installed, so no pull requests. The mailbox
replaces PR comments.

**Author:**

1. Self-review your own diff against `docs/self-review.md`. Do not send the reviewer things
   you could have caught yourself.

   Note: the `/code-review` skill is **user-invocable only** — an agent cannot call it, and
   attempting to will fail with `disable-model-invocation`. Self-review here means reading
   `git diff origin/main...HEAD` deliberately against the checklist. If Tim wants a
   tool-driven pass, he can type `/code-review` in your window himself.
2. Hand off in one command:

   ```sh
   make handoff MSG="GcsTripStore. Focus on JSON round-trip fidelity and the FUSE rename path."
   ```

   That runs `make check`, pushes the branch, and mails the integrator. It refuses to
   proceed if checks fail, so a broken branch cannot be handed over.
3. Keep working. The review arrives in your mailbox.

**If your branch cannot pass `make check` alone** — a signed-off contract change that breaks
the other side's build is the usual case — do not sit on it unpushed:

```sh
make handoff-blocked MSG="what fails, and why it is not yours to fix"
```

That skips the gate deliberately, pushes, and flags the branch as needing integrator
resolution. The gate exists to stop *broken* work being handed off, not to hide work that is
correct on your side and blocked on someone else's file.

**Integrator** is woken by the message, reviews the diff, fans out subagent reviewers where
the change is large or risky, and mails findings back to the author's box. Cross-review by
the other engineer is requested the same way.

**Reviewer** (whichever session is asked):

```sh
git fetch origin
git diff main...origin/be/trip-storage
```

Findings go back by mail. State them compactly: file, line, what breaks, and a concrete
failure case. Vague "consider refactoring" notes are not worth the round trip.

Review from *your own* perspective, not the author's — that is the point of cross-review:

- The **frontend engineer** reviewing backend work asks: is the API shape actually usable
  from a component? Are error codes distinguishable? Does anything here force the client
  into an extra round trip, or into holding state the server should own?
- The **backend engineer** reviewing frontend work asks: is the client hammering an
  endpoint the free tier cannot sustain? Is it assuming a response shape the contract does
  not guarantee? Is it treating LLM-suggested data as verified?

**Integrator** does a final pass and merges to `main`.

## The integrator is not exempt

`main` has a `pre-push` hook that runs `make check` and refuses the push if it fails. Enable
it once per clone:

```sh
git config core.hooksPath .githooks
```

It exists because the integrator pushed a red `main` three times in one session, every time
by chaining `commit && push` behind a check whose exit code a pipe had swallowed. The
engineers had `make handoff` gating them and the integrator had good intentions; only one of
those worked. `SKIP_PUSH_CHECK=1` is the escape hatch, and using it should feel deliberate.

## Keeping everyone fed

```sh
scripts/queue-status
```

Who is on what, and — the point of it — who has **nothing to do**. The integrator's attention
is event-driven: the mailbox wakes them when work *arrives* and stays silent when work
*stops*. An engineer whose branch was merged and who was never given a next task is invisible
to every other mechanism here. That is not hypothetical; one sat idle through three review
rounds before Tim noticed.

Run it after every merge. **A merge notice is not an assignment.**

The pre-push hook prints it automatically on every push to `main`, because relying on the
integrator to remember failed three times in one session — each time caught by Tim rather
than by any mechanism here.

## Escalation

Review rounds are capped at **two** per branch. If a disagreement survives two rounds, the
integrator stops the loop and brings Tim in with a short summary of both positions — not a
transcript. Two agents arguing politely can burn a great deal of budget without converging.

Escalate immediately, without waiting for round two, when:

- The disagreement is about the API contract or an architectural decision already recorded
  in `CLAUDE.md`.
- Resolving it would change scope, cost, or the deploy shape.
- Either side is asking for a third-party service, dependency, or credential that does not
  already exist.

The integrator never merges to `main` without telling Tim.

## Branch size

Hand off **one queue item at a time**. A branch that implements three things is a branch
nobody can review properly, and it blocks the other engineer for longer. Finish item 1,
`make handoff`, start item 2 on a fresh branch while the review runs.

## Settled decisions

The choices recorded in `CLAUDE.md` — OpenRouteService over GraphHopper, single Cloud Run
service, Cloud Storage for trips, contract-first types, chat-is-never-required — are
settled. Do not silently redesign around them. If you think one is wrong, say so by mail
with your reasoning and keep working on something else meanwhile; the integrator will
escalate if it has merit.

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
