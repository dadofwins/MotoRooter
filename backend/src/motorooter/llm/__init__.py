"""The LLM tool layer.

Tool calls execute server-side; the frontend never holds the OpenAI key. The model is
pinned in config, never inline, so which model answers is a deploy decision rather than
something buried at a call site.

Two rules from the architecture that shape everything here:

- **Every tool is a thin wrapper over the same service function the REST endpoint calls.**
  The mouse path and the chat path must not diverge, and if they are separate
  implementations they will — silently, because both produce a plausible result.
- **Model output is a proposal, never a fact.** Geography it invents is validated against a
  real routing or Places API before it can reach the map.
"""
