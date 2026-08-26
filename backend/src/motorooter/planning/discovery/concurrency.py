"""How much metered work discovery runs at once.

Discovery is almost entirely waiting on four different APIs, so doing it one request at a
time is the wrong shape: a 300 km corridor took minutes of wall clock while the CPU did
nothing. It also cannot be unbounded — every provider here has a per-minute ceiling, and the
failure mode of exceeding one is a wave of 429s that looks exactly like the service being
broken.

One number, shared, because the ceiling being respected belongs to the stack rather than to
any single stage. The stages bound themselves with a plain `asyncio.Semaphore`: the pipeline
needs to emit several progress events per unit of work and the resolver needs input order
back, and no one helper is a good fit for both.
"""

DEFAULT_CONCURRENCY = 6
"""Requests in flight per stage.

Chosen against the tightest per-minute ceiling in the stack rather than against what the
event loop could manage. Six concurrent searches at roughly a second each is around 360 a
minute at full tilt — comfortably inside Brave's and Places' limits with room for the retry
traffic a transient failure produces, and far enough below them that a burst does not turn
into a wave of 429s indistinguishable from an outage.

Per stage, not per run, so two stages overlapping can put twice this in flight. That is
accounted for in the headroom above rather than coordinated, since a global budget would
couple stages that otherwise know nothing about each other.
"""
