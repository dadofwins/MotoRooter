"""LLM-layer failures, provider-neutral.

Adapters translate their upstream's failure modes into these, so retry and quota logic never
needs to know which vendor produced the failure — the same reasoning as `routing.errors`.
"""


class LlmError(Exception):
    """Base for LLM failures."""

    retryable: bool = False


class LlmUnavailable(LlmError):
    """Transient upstream failure: timeout, 5xx, connection reset."""

    retryable = True


class LlmQuotaExceeded(LlmError):
    """Rate limit or spend cap. Not retryable — retrying spends what is left."""


class LlmRefused(LlmError):
    """The provider rejected the request: bad key, filtered content, unsupported model."""


class ToolCallFailed(LlmError):
    """A tool could not be run as asked.

    Usually the model's fault rather than ours — an invented tool name, malformed arguments,
    arguments of the wrong shape — so it is normally reported *back to the model* and the
    conversation continues. Raised only when the wiring itself is wrong, which is a startup
    problem rather than a conversation one.
    """
