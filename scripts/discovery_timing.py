"""How long does discovery take, and how often does it say anything?

The live run that motivated this work took just over two minutes for one corridor and
emitted an update roughly every twenty-five seconds. A rider cannot tell that from frozen.

This measures the same two numbers without credentials. Every external call is replaced by
an `asyncio.sleep` at a scaled-down latency, so the absolute times mean nothing — the
*ratio* between serial and parallel is the claim, and it is scale-invariant because the work
is pure waiting. The pipeline itself is the real one; only the four I/O seams are fakes.

    uv run --project backend python scripts/discovery_timing.py

Latencies are shaped from what the live run showed: search and lookup are fast, resolution
is fast and per-candidate, extraction is the expensive one.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/src"))

from motorooter.planning.discovery.category import CategoryClassifier  # noqa: E402
from motorooter.planning.discovery.extract import PlaceExtractor  # noqa: E402
from motorooter.planning.discovery.judge import CandidateJudge  # noqa: E402
from motorooter.planning.discovery.models import Candidate  # noqa: E402
from motorooter.planning.discovery.naming import PlaceNamer  # noqa: E402
from motorooter.planning.discovery.pipeline import DiscoveryPipeline  # noqa: E402
from motorooter.planning.discovery.queries import SearchQuery  # noqa: E402
from motorooter.planning.discovery.resolve import PlacesResolver  # noqa: E402
from motorooter.routing.models import Coordinate, RouteLeg  # noqa: E402
from motorooter.trips.models import PoiCategory  # noqa: E402

SCALE = 0.02
"""Everything is divided by fifty so a run takes seconds rather than minutes. The ratio the
script reports is unaffected; the absolute numbers are meaningless and not printed as if
they were wall clock."""

NAME_S = 0.4 * SCALE
SEARCH_S = 1.2 * SCALE
EXTRACT_S = 6.0 * SCALE
RESOLVE_S = 0.5 * SCALE
JUDGE_S = 4.0 * SCALE

CATEGORIES = [PoiCategory.WILD_CAMP, PoiCategory.VIEWPOINT, PoiCategory.FOOD]

LEG = RouteLeg(
    geometry=tuple(Coordinate(lat=46.8 + i * 0.02, lon=-121.5) for i in range(120)),
    distance_m=260_000.0,
    duration_s=11_000.0,
    provider="fake",
    intent="twisty_paved",
)


class SlowNamer(PlaceNamer):
    def __init__(self) -> None:
        super().__init__(api_key="unused")

    async def name_for(self, anchor: Coordinate) -> str:
        await asyncio.sleep(NAME_S)
        return f"Anchor {anchor.lat:.2f}"

    async def region_for(self, anchor: Coordinate) -> str:
        await asyncio.sleep(NAME_S)
        return "Washington"


class SlowSource:
    name = "slow-fake"

    async def search(
        self, query: SearchQuery, *, near: Coordinate, limit: int = 5
    ) -> tuple[Candidate, ...]:
        await asyncio.sleep(SEARCH_S)
        return tuple(
            Candidate(
                name=f"Place {n} near {query.place}",
                category=query.category,
                found_near=near,
                source=self.name,
                snippet=f"a description of Place {n}",
            )
            for n in range(3)
        )


class SlowResolver(PlacesResolver):
    def __init__(self) -> None:
        super().__init__(api_key="unused")

    async def resolve(self, candidates, *, route=(), corridor_m=15_000.0, concurrency=6):
        await asyncio.sleep(RESOLVE_S * len(candidates) / max(concurrency, 1))
        return ()


class SlowLlm:
    """One client, two latencies: extraction and judging cost different amounts."""

    def __init__(self, delay: float, reply: str) -> None:
        self._delay = delay
        self._reply = reply
        self.model = "slow-fake"
        self.conversations: list[object] = []
        self.calls = 0

    async def complete(self, messages, tools):
        from motorooter.llm.messages import AssistantMessage

        self.calls += 1
        self.conversations.append(messages)
        await asyncio.sleep(self._delay)
        return AssistantMessage(content=self._reply)


def build(extractor_llm: SlowLlm) -> DiscoveryPipeline:
    return DiscoveryPipeline(
        namer=SlowNamer(),
        source=SlowSource(),
        extractor=PlaceExtractor(extractor_llm),
        resolver=SlowResolver(),
        classifier=CategoryClassifier(SlowLlm(EXTRACT_S, '{"categories": []}')),
        judge=CandidateJudge(SlowLlm(JUDGE_S, '{"scores": []}')),
    )


async def run(concurrency: int) -> tuple[float, int, list[float], int]:
    """Wall clock, update count, gaps between updates, and LLM calls made."""
    extractor_llm = SlowLlm(EXTRACT_S, '{"places": []}')
    pipeline = build(extractor_llm)

    started = time.perf_counter()
    stamps: list[float] = []
    async for _ in pipeline.run(LEG, CATEGORIES, concurrency=concurrency):
        stamps.append(time.perf_counter())
    elapsed = time.perf_counter() - started

    gaps = [b - a for a, b in zip([started, *stamps], stamps, strict=False)]
    return elapsed, len(stamps), gaps, extractor_llm.calls


async def main() -> int:
    serial, serial_updates, serial_gaps, serial_calls = await run(concurrency=1)
    parallel, par_updates, par_gaps, par_calls = await run(concurrency=6)

    print("Simulated latencies, scaled by 1/50. Ratios are the claim; times are not.\n")
    print(f"{'':10} {'elapsed':>9} {'updates':>9} {'worst gap':>11} {'extract calls':>15}")
    for label, elapsed, updates, gaps, calls in (
        ("serial", serial, serial_updates, serial_gaps, serial_calls),
        ("parallel", parallel, par_updates, par_gaps, par_calls),
    ):
        worst = max(gaps) if gaps else 0.0
        print(f"{label:10} {elapsed:8.2f}s {updates:9} {worst:10.2f}s {calls:15}")

    print(f"\nspeedup: {serial / parallel:.1f}x")
    print("\nUnscaled, i.e. what a rider would see:")
    print(f"  serial   ~{serial / SCALE:>5.0f}s total, worst silence ~{max(serial_gaps) / SCALE:.0f}s")
    print(f"  parallel ~{parallel / SCALE:>5.0f}s total, worst silence ~{max(par_gaps) / SCALE:.0f}s")
    print(
        "\nThe serial figure is the check on the model: the live run took just over two "
        "minutes,\nso a simulation landing near it is evidence the latencies are shaped "
        "right."
    )
    print(
        "\nSince confirmed live at 19.1s over the Chinook Pass corridor, against the ~21s "
        "predicted\nhere — but only after two live bugs this simulation could not have "
        "found: extraction\ntimed out on every anchor and yielded nothing, and a lone 429 "
        "would have discarded a\nwhole batch of resolutions. Wall clock was the easy part "
        "to model and the least of it."
    )
    print(
        "\nTwo separate wins, and the table only shows one of them. Both rows are the new "
        "pipeline,\nso both already have the finer-grained events — parallelism is what "
        "126s -> 21s measures.\nThe 25s of live silence became ~6s from the extra events, "
        "not from concurrency."
    )
    print(
        f"\nWorst silence is the honest caveat: it is the same ~6s in both rows, because it "
        f"is one\nbatched extraction and running anchors at once does not shorten any single "
        f"call. Median\ngap is {statistics.median(par_gaps) / SCALE:.1f}s because updates now "
        "arrive in bursts — anchors finish searching\ntogether, then sit in extraction "
        "together. Splitting extraction back up would trade that\ngap for the cost the "
        "batching just removed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
