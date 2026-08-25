"""Cloud Storage adapter for the object-store seam.

Spoken over the GCS JSON API with `httpx`, which the project already depends on, rather
than `google-cloud-storage`. Three reasons: the client library is synchronous, so every
call would have to be pushed through `asyncio.to_thread` inside an otherwise async stack;
the surface used here is four HTTP requests, none of them subtle; and staying on `httpx`
means the adapter is testable with `respx`, exactly like the ORS and Google routing
adapters. If resumable uploads or customer-managed encryption ever become requirements,
that calculus changes and the library earns its place.

Two Cloud Storage properties the design leans on:

- **Object writes are atomic.** A reader gets the previous object or the new one, never a
  splice of both. That is why there is no write-then-swap here — which is just as well,
  since GCS has no rename, and the FUSE layer that pretends otherwise emulates it with a
  copy plus a delete.
- **`ifGenerationMatch` is evaluated server-side.** With `0` it succeeds only if nothing is
  live at that path, which is how `create` refuses to clobber without a check-then-write
  race. With a generation read earlier it succeeds only if nothing has changed since, which
  is what makes read-merge-write safe for a document two clients can edit at once.
"""

import asyncio
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from motorooter.clock import Clock, SystemClock
from motorooter.trips.objects import (
    MUST_NOT_EXIST,
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStoreUnavailable,
    ObjectVersionMismatch,
    StoredObject,
)

GCS_BASE_URL = "https://storage.googleapis.com"

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
"""Cloud Run's ambient service-account credentials. Plain HTTP, no client library needed."""

TOKEN_REFRESH_MARGIN_S = 60.0
"""Renew this far ahead of expiry so a token cannot die while a request is in flight."""

_JSON_CONTENT_TYPE = "application/json"


@runtime_checkable
class AccessTokenSource(Protocol):
    """Supplies an OAuth2 bearer token, or `None` for an unauthenticated endpoint."""

    async def token(self) -> str | None: ...


class StaticTokenSource:
    """A fixed token. For local development against a real bucket, and for tests."""

    def __init__(self, value: str) -> None:
        self._value = value

    async def token(self) -> str:
        return self._value


class AnonymousTokenSource:
    """No credentials at all — a storage emulator, or a world-readable bucket."""

    async def token(self) -> None:
        return None


class MetadataServerTokenSource:
    """Service-account tokens from the GCE/Cloud Run metadata server.

    Cached until shortly before expiry: the metadata server is cheap but not free, and it
    is on the critical path of every trip save.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self._clock = clock or SystemClock()
        self._client = client
        self._timeout_s = timeout_s
        self._cached: str | None = None
        self._expires_at = 0.0
        self._refreshing = asyncio.Lock()

    async def token(self) -> str:
        cached = self._fresh_token()
        if cached is not None:
            return cached

        async with self._refreshing:
            # Listing a bucket fans out a read per trip, so a cold cache would otherwise
            # mint one token per trip. Re-check inside the lock: whoever held it first has
            # already refreshed by the time the rest get in.
            cached = self._fresh_token()
            if cached is not None:
                return cached
            return await self._refresh()

    def _fresh_token(self) -> str | None:
        if self._cached is not None and self._clock.now() < self._expires_at:
            return self._cached
        return None

    async def _refresh(self) -> str:
        body = await self._fetch()
        try:
            value = str(body["access_token"])
            lifetime = float(body["expires_in"])
        except (TypeError, KeyError, ValueError) as exc:
            msg = f"unrecognized metadata server token response: {exc}"
            raise ObjectStoreUnavailable(msg) from exc

        self._cached = value
        self._expires_at = self._clock.now() + max(lifetime - TOKEN_REFRESH_MARGIN_S, 0.0)
        return value

    async def _fetch(self) -> Any:  # noqa: ANN401 -- raw JSON
        headers = {"Metadata-Flavor": "Google"}
        try:
            if self._client is not None:
                response = await self._client.get(
                    METADATA_TOKEN_URL, headers=headers, timeout=self._timeout_s
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.get(METADATA_TOKEN_URL, headers=headers)
        except httpx.HTTPError as exc:
            # Almost always "not running on Google Cloud". Say so rather than time out.
            msg = f"could not reach the metadata server for credentials: {exc}"
            raise ObjectStoreUnavailable(msg) from exc

        if not response.is_success:
            msg = f"metadata server returned HTTP {response.status_code} for an access token"
            raise ObjectStoreUnavailable(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = "metadata server returned a non-JSON access token response"
            raise ObjectStoreUnavailable(msg) from exc


class GcsObjectStore:
    """Objects in a Cloud Storage bucket, over the JSON API."""

    def __init__(
        self,
        bucket: str,
        *,
        token_source: AccessTokenSource,
        base_url: str = GCS_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
        content_type: str = _JSON_CONTENT_TYPE,
    ) -> None:
        """
        Args:
            bucket: bucket name, without a `gs://` scheme.
            token_source: how to authenticate. `MetadataServerTokenSource` on Cloud Run.
            base_url: override to point at a storage emulator.
            client: injectable HTTP client, so callers can share a connection pool.
            timeout_s: per-request timeout.
            content_type: stored on write. Everything here is JSON today.
        """
        self._bucket = bucket
        self._token_source = token_source
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s
        self._content_type = content_type

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def token_source(self) -> AccessTokenSource:
        return self._token_source

    @property
    def client(self) -> httpx.AsyncClient | None:
        """The shared connection pool, if one was injected."""
        return self._client

    async def read(self, path: str) -> StoredObject:
        response = await self._request("GET", self._object_url(path), params={"alt": "media"})
        self._raise_for_status(response, path)
        return StoredObject(data=response.content, generation=self._generation_of(response, path))

    async def exists(self, path: str) -> bool:
        # Metadata rather than media: a trip document carries full route geometry, and
        # probing for one should not pay to download it.
        response = await self._request("GET", self._object_url(path))
        if response.status_code == 404 and not self._is_missing_bucket(response):
            return False
        # A bucket-level 404 falls through to _raise_for_status deliberately. Answering
        # False here would report an entire unreachable bucket as "no trip at that slug",
        # and a caller checking whether a name is free would then create over live data.
        self._raise_for_status(response, path)
        return True

    async def write(self, path: str, data: bytes, *, if_generation_match: int | None = None) -> int:
        params = {"uploadType": "media", "name": path}
        if if_generation_match is not None:
            # Evaluated by GCS, so two racing writers cannot both pass it.
            params["ifGenerationMatch"] = str(if_generation_match)

        response = await self._request(
            "POST",
            f"{self._base_url}/upload/storage/v1/b/{self._bucket}/o",
            params=params,
            content=data,
            headers={"Content-Type": self._content_type},
        )
        self._raise_for_status(response, path, expected_generation=if_generation_match)
        return self._generation_in_body(response, path)

    async def delete(self, path: str) -> None:
        response = await self._request("DELETE", self._object_url(path))
        self._raise_for_status(response, path)

    async def list_prefix(self, prefix: str) -> list[str]:
        """Walk every page. Stopping at the first would silently hide trips."""
        paths: list[str] = []
        params = {"prefix": prefix}
        while True:
            response = await self._request(
                "GET", f"{self._base_url}/storage/v1/b/{self._bucket}/o", params=params
            )
            if response.status_code == 404:
                # Listing an existing bucket returns 200 with no items even when empty, so
                # a 404 here can only mean the bucket itself is gone. No ambiguity to weigh.
                msg = (
                    f"Cloud Storage bucket {self._bucket!r} does not exist or is not "
                    f"visible to this service account"
                )
                raise ObjectStoreUnavailable(msg)
            self._raise_for_status(response, prefix)
            body = self._json(response)

            # `or []` rather than a default: GCS omits `items`, but a proxy answering
            # `{"items": null}` would otherwise raise a raw TypeError through the seam.
            for item in body.get("items") or []:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str):
                    paths.append(name)

            token = body.get("nextPageToken")
            if not token:
                return paths
            params = {"prefix": prefix, "pageToken": str(token)}

    def _object_url(self, path: str) -> str:
        # `safe=""` is the point: an object name is one path segment, so its slashes must
        # be percent-encoded. Left bare they would address a different resource entirely.
        return f"{self._base_url}/storage/v1/b/{self._bucket}/o/{quote(path, safe='')}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        token = await self._token_source.token()
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"

        try:
            if self._client is not None:
                return await self._client.request(
                    method,
                    url,
                    params=params,
                    content=content,
                    headers=request_headers,
                    timeout=self._timeout_s,
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.request(
                    method, url, params=params, content=content, headers=request_headers
                )
        except httpx.HTTPError as exc:
            msg = f"Cloud Storage request failed: {exc}"
            raise ObjectStoreUnavailable(msg) from exc

    def _raise_for_status(
        self,
        response: httpx.Response,
        path: str,
        *,
        expected_generation: int | None = None,
    ) -> None:
        if response.is_success:
            return
        status = response.status_code

        if status == 404:
            if self._is_missing_bucket(response):
                # Not "no such trip". A whole unreachable bucket answering 404 per object
                # would let the API report every trip as deleted, and a client could then
                # create over data that is merely unreachable.
                msg = (
                    f"Cloud Storage bucket {self._bucket!r} does not exist or is not "
                    f"visible to this service account: {self._error_message(response)}"
                )
                raise ObjectStoreUnavailable(msg)
            raise ObjectNotFound(path)

        if status == 412:
            # Both preconditions fail with 412; only the request knows which was asked for.
            if expected_generation == MUST_NOT_EXIST:
                raise ObjectAlreadyExists(path)
            raise ObjectVersionMismatch(path, expected_generation or 0)

        # Everything else — including 401 and 403 — is unavailability rather than absence.
        msg = f"Cloud Storage returned HTTP {status} for {path!r}: {self._error_message(response)}"
        raise ObjectStoreUnavailable(msg)

    @staticmethod
    def _is_missing_bucket(response: httpx.Response) -> bool:
        """Whether a 404 is about the bucket rather than the object.

        GCS uses `reason: notFound` for both and distinguishes them only in prose, so this
        reads the message. Imprecise, and deliberately biased: misreading a missing object
        as a missing bucket costs a 503 on a request that should have been a 404, while the
        reverse direction reports live data as deleted.
        """
        try:
            body = response.json()
        except ValueError:
            return False
        error = body.get("error") if isinstance(body, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        return "bucket" in message.lower()

    def _generation_of(self, response: httpx.Response, path: str) -> int:
        """Generation from a media download, which reports it in a header."""
        raw = response.headers.get("x-goog-generation")
        if raw is None:
            msg = f"Cloud Storage omitted x-goog-generation reading {path!r}"
            raise ObjectStoreUnavailable(msg)
        try:
            return int(raw)
        except ValueError as exc:
            msg = f"Cloud Storage returned a non-numeric generation {raw!r} for {path!r}"
            raise ObjectStoreUnavailable(msg) from exc

    def _generation_in_body(self, response: httpx.Response, path: str) -> int:
        """Generation from an upload, which reports it in the object metadata."""
        raw = self._json(response).get("generation")
        try:
            return int(str(raw))
        except (TypeError, ValueError) as exc:
            msg = f"Cloud Storage returned no usable generation writing {path!r}: {raw!r}"
            raise ObjectStoreUnavailable(msg) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            msg = "Cloud Storage returned a non-JSON body where an object listing was expected"
            raise ObjectStoreUnavailable(msg) from exc
        if not isinstance(body, dict):
            msg = f"unrecognized Cloud Storage listing shape: {type(body).__name__}"
            raise ObjectStoreUnavailable(msg)
        return body

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message", error))
        return str(error or "")[:200]
