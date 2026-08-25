# Self-review checklist

Run through this against your own diff before `make handoff`. It exists because the
`/code-review` skill is user-invocable only — an agent cannot call it — so self-review here
means reading your own diff deliberately, not invoking a tool.

```sh
git fetch origin && git diff origin/main...HEAD
```

Read the whole thing. Not the summary, the diff. Most of what a reviewer catches is visible
to the author too, if they look at what they actually wrote rather than what they meant.

## Every change

- [ ] `make check` passes. (`make handoff` enforces this, but know it before you start.)
- [ ] Tests were written before the implementation, and you watched them fail. A test that
      never failed has not been shown to test anything.
- [ ] No debug leftovers: stray prints, commented-out code, `.only` / `.skip`, `xfail`.
- [ ] Comments explain *why*, not *what*. Delete any that restate the line below them.
- [ ] The diff contains one queue item. If it grew a second, split it.
- [ ] Nothing outside your ownership column in `docs/parallel-work.md` was touched.

## Anything touching external services

- [ ] No test reaches a live API. Fixtures or fakes only.
- [ ] Every failure mode is mapped to the shared error hierarchy — timeouts, 4xx, 5xx,
      malformed payloads. A raw `KeyError` from a shape change must not reach a caller.
- [ ] Request counts are bounded. The ORS free tier is ~2,000–2,500/day and the drag
      interaction can exhaust it in one session.
- [ ] Nothing from Google Places is persisted except `place_id`.

## Anything touching time

- [ ] Uses the injected clock (`motorooter/clock.py`) or fake timers. No real `sleep`, no
      wall-clock reads in a code path a test exercises.

## Anything touching stored data

- [ ] Round-trips exactly: tuples stay tuples, `None` stays `None`, timestamps keep their
      timezone. Assert on the reloaded object, not on the object you saved.
- [ ] Nothing is written to the container filesystem. Cloud Run's disk is ephemeral and
      per-instance.
- [ ] User input that becomes a path or a key is validated, not trusted.

## Anything touching LLM output

- [ ] Treated as unverified candidates. Coordinates are resolved against a real API before
      anything reaches the map.

## Before you hand off

Write the `MSG` as if you were the reviewer: what changed, and the one or two places where
you are least confident. "Focus on the concurrent-write path" gets you a better review than
"implemented GcsTripStore". If you had to make a judgement call, name it — that is exactly
what a second pair of eyes is for.

If something in this list does not apply, skip it. If something bit you that is not on the
list, add it.
