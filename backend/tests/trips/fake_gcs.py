"""A Cloud Storage JSON API good enough to drive the adapter against. Never hits network.

This is the emulator seam. `InMemoryObjectStore` proves the *trip* logic; this proves the
*HTTP* logic — URL construction, `%2F` encoding of object names, generation preconditions,
and page-token iteration — none of which an in-process fake would exercise.

It models only what `GcsObjectStore` uses. Where real GCS has behaviour worth asserting on
(atomic writes, `ifGenerationMatch=0`, paginated listings) it is faithful; everything else
is absent rather than approximated.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

BUCKET = "motorooter-trips-test"
BASE_URL = "https://storage.example.test"

PAGE_SIZE = 2
"""Small on purpose: any listing of three or more objects forces the adapter to paginate."""


class FakeGcs:
    """Object bytes plus a generation counter, served over the GCS JSON API shape."""

    def __init__(self, bucket: str = BUCKET) -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.requests: list[httpx.Request] = []
        self._next_generation = 1
        self._upload_prefix = f"/upload/storage/v1/b/{bucket}/o"
        self._object_prefix = f"/storage/v1/b/{bucket}/o"

    # -- respx wiring ----------------------------------------------------------------

    def install(self, mock: Any) -> None:
        """Route every request to this host here, so an unexpected URL fails loudly."""
        mock.route(host=httpx.URL(BASE_URL).host).mock(side_effect=self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # `url.path` percent-decodes, which would turn `%2F` back into a path separator and
        # hide an encoding bug. The raw form is what the adapter actually sent.
        raw = request.url.raw_path.decode()
        path, _, query = raw.partition("?")
        params = parse_qs(query)

        if request.method == "POST" and path == self._upload_prefix:
            return self._upload(request, params)
        if request.method == "GET" and path == self._object_prefix:
            return self._list(params)
        if path.startswith(f"{self._object_prefix}/"):
            name = unquote(path[len(self._object_prefix) + 1 :])
            if request.method == "GET":
                return self._get(name, params)
            if request.method == "DELETE":
                return self._delete(name)
        return self._error(400, f"fake GCS has no route for {request.method} {path}")

    # -- operations ------------------------------------------------------------------

    def _upload(self, request: httpx.Request, params: dict[str, list[str]]) -> httpx.Response:
        name = params.get("name", [""])[0]
        if not name:
            return self._error(400, "missing object name")
        if params.get("uploadType", [""])[0] != "media":
            return self._error(400, "fake GCS only implements uploadType=media")

        precondition = params.get("ifGenerationMatch")
        if precondition is not None:
            expected = int(precondition[0])
            actual = self.objects.get(name, (b"", 0))[1]
            if expected != actual:
                # Real GCS rejects before writing anything; the old object survives intact.
                return self._error(412, f"generation mismatch: expected {expected}")

        generation = self._next_generation
        self._next_generation += 1
        self.objects[name] = (request.content, generation)
        return httpx.Response(200, json=self._metadata(name))

    def _get(self, name: str, params: dict[str, list[str]]) -> httpx.Response:
        if name not in self.objects:
            return self._error(404, f"no such object: {name}")
        if params.get("alt", [""])[0] == "media":
            data, generation = self.objects[name]
            # Real GCS reports the generation of a media download in this header; without
            # it the adapter has no version to hand back for a conditional write.
            return httpx.Response(200, content=data, headers={"x-goog-generation": str(generation)})
        return httpx.Response(200, json=self._metadata(name))

    def _delete(self, name: str) -> httpx.Response:
        if name not in self.objects:
            return self._error(404, f"no such object: {name}")
        del self.objects[name]
        return httpx.Response(204)

    def _list(self, params: dict[str, list[str]]) -> httpx.Response:
        prefix = params.get("prefix", [""])[0]
        names = sorted(name for name in self.objects if name.startswith(prefix))

        start = int(params.get("pageToken", ["0"])[0])
        page = names[start : start + PAGE_SIZE]
        body: dict[str, Any] = {"items": [self._metadata(name) for name in page]}
        if start + PAGE_SIZE < len(names):
            body["nextPageToken"] = str(start + PAGE_SIZE)
        return httpx.Response(200, json=body)

    # -- helpers ---------------------------------------------------------------------

    def _metadata(self, name: str) -> dict[str, Any]:
        data, generation = self.objects[name]
        return {
            "kind": "storage#object",
            "bucket": self.bucket,
            "name": name,
            "generation": str(generation),
            "size": str(len(data)),
        }

    @staticmethod
    def _error(status: int, message: str) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": status, "message": message, "errors": []}}
        )


def written_trip(fake: FakeGcs, slug: str) -> dict[str, Any]:
    """The trip document the adapter actually put in the bucket, decoded."""
    data, _ = fake.objects[f"trips/{slug}/trip.json"]
    parsed: dict[str, Any] = json.loads(data)
    return parsed


def upload_count(fake: FakeGcs, name: str) -> int:
    """How many uploads targeted `name` — a write-then-swap would show up as two."""
    pattern = re.compile(r"[?&]name=([^&]*)")
    count = 0
    for request in fake.requests:
        if request.method != "POST":
            continue
        match = pattern.search(request.url.raw_path.decode())
        if match and unquote(match.group(1)) == name:
            count += 1
    return count
