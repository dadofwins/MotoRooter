"""Corrects the declared media type for streaming endpoints in the OpenAPI document.

FastAPI derives a route's success media type from its `response_class`, but that choice
propagates to *every* response on the route — so declaring a stream also relabels the
route's error bodies as streams, and merges the stream's `type: string` onto their model
refs. Both produce wrong types in the generated frontend client.

Declaring the media type in the decorator's `responses` dict does not work either: FastAPI
adds to the content map rather than replacing it, leaving two media types where there is
one.

So the success response is declared normally and its media-type key is corrected here,
after generation. The frontend compiles against this document, so it needs to be true.
"""

from typing import Any

STREAMING_RESPONSES: dict[tuple[str, str], str] = {
    ("/api/trips/{slug}/replan", "post"): "application/x-ndjson",
    ("/api/trips/{slug}/chat", "post"): "application/x-ndjson",
}
"""(path, method) -> media type of the 2xx body. Errors on these routes stay JSON."""


def apply_streaming_media_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the success media type for each declared streaming endpoint, in place.

    Silently ignores entries whose route or 200 response is absent: a renamed path should
    surface as a failing contract test, not an exception during schema generation.
    """
    for (path, method), media_type in STREAMING_RESPONSES.items():
        operation = schema.get("paths", {}).get(path, {}).get(method)
        if operation is None:
            continue
        content = operation.get("responses", {}).get("200", {}).get("content")
        if not content or media_type in content:
            continue
        # Exactly one entry at this point — FastAPI emits a single default media type.
        (existing,) = content.keys()
        content[media_type] = content.pop(existing)
    return schema
