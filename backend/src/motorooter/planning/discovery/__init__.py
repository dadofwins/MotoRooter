"""Discovery: finding things worth stopping for along a route.

Three stages that answer different questions with different tools, and mixing them is the
main way this goes wrong.

    SEARCH    Brave web search    what exists and what it is like
    RESOLVE   Google Places       does it exist, where exactly, is it open
    JUDGE     metrics + LLM       how good is it, is it worth the detour

The rule the whole design turns on: **measure what is measurable, ask the model only what is
not.** Twistiness, surface mix, detour cost and remoteness are arithmetic on geometry already
held. A model asked for those is slower, non-deterministic, and perfectly capable of being
confidently wrong about a number it could have been handed. Compute them, test them, and pass
them to the model as evidence — then ask it the thing it is actually good at: is this scenic,
is it locally famous, does the ride report say it washes out in spring.

Search results and model output are both *claims*. Nothing reaches the map without resolving
to a real `place_id`, and a candidate that will not resolve is dropped rather than guessed at.
"""
