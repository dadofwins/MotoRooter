"""Building the blurb writer from configuration.

Its own model client rather than the chat one reused, because the two want opposite budgets.
A chat turn may be midway through a tool sequence with the rider watching, so it gets 120
seconds and no reasoning cap. A blurb is decoration nothing waits on, so it gets a few
seconds and minimal effort — and if it misses, the header keeps its static line.

Which model answers is a deploy decision, read from the same pinned setting as everything
else. There is no inline default on this path.
"""

from motorooter.blurb.writer import BlurbWriter
from motorooter.llm.providers.openai import OpenAiClient
from motorooter.planning.discovery.factory import DiscoverySettings

BLURB_TIMEOUT_S = 8.0
"""Short, because nothing waits on this.

The rail renders its static header immediately and swaps in the line if one arrives. A
budget longer than a rider's patience would buy nothing: a blurb that lands after they have
started typing is worse than no blurb, because the header changes under them.
"""

BLURB_EFFORT = "minimal"
"""Reasoning effort. Measured rather than inherited from another stage's answer.

Measured 2026-09-04 through `BlurbWriter` against the live API — `scripts/blurb_effort_spike.py`,
so the numbers describe the path that ships. One routed Leavenworth loop with a saved
campground, five runs each:

    minimal   1.2-2.0 s    "sick offroad loop out of leavenworth - half unpaved,
                            pin a camp at Eightmile?"
    default   7.6-10.4 s   "tight 58 km offroad loop leavenworth -> blewett pass ->
                            leavenworth, 50% unpaved/50% unsurveyed - drop a pin for
                            eightmile campground?"

**Five to eight times faster, and not worse.** Both settings obeyed the voice, both stayed
inside the facts, and both reported unsurveyed separately from paved. The default's lines are
longer and more inventory-like — it spends the extra thinking listing what the rider can
already see on their own map, which is the one thing the prompt asks it not to do.

So this is extraction's answer rather than the judge's, and for extraction's reason: the
model is arranging facts it was handed under a rule forbidding it from adding any, so there
is nothing for reasoning to decide. That was the expectation going in, which is exactly why
it was worth measuring — the same expectation was wrong about judging.

Two runs of five at the default exceeded `BLURB_TIMEOUT_S`, so effort and budget are not
independent: raising this means raising that, and at the default a fifth of blurbs would
simply not arrive.
"""


def build_blurb_writer(settings: DiscoverySettings) -> BlurbWriter | None:
    """The blurb writer, or `None` when there is no key.

    `None` disables the header line rather than the deployment, the same choice chat and
    discovery make for the same reason.
    """
    if not settings.openai_api_key:
        return None
    return BlurbWriter(
        OpenAiClient(
            api_key=settings.openai_api_key,
            model=settings.model,
            timeout_s=BLURB_TIMEOUT_S,
            reasoning_effort=BLURB_EFFORT,
        )
    )
