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
- **`ifGenerationMatch=0` succeeds only if the object does not exist.** That is what makes
  `create` refuse to clobber without a check-then-write race.
"""

import asyncio
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from motorooter.clock import Clock, SystemClock
from motorooter.trips.objects import (
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStoreUnavailable,
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

    async def read(self, path: str) -> bytes:
        response = await self._request("GET", self._object_url(path), params={"alt": "media"})
        self._raise_for_status(response, path)
        return response.content

    async def exists(self, path: str) -> bool:
        # Metadata rather than media: a trip document carries full route geometry, and
        # probing for one should not pay to download it.
        response = await self._request("GET", self._object_url(path))
        if response.status_code == 404:
            return False
        self._raise_for_status(response, path)
        return True

    async def write(self, path: str, data: bytes, *, if_absent: bool = False) -> None:
        params = {"uploadType": "media", "name": path}
        if if_absent:
            # Generation 0 means "no live object". The precondition is evaluated by GCS,
            # so two racing creates cannot both pass it.
            params["ifGenerationMatch"] = "0"

        response = await self._request(
            "POST",
            f"{self._base_url}/upload/storage/v1/b/{self._bucket}/o",
            params=params,
            content=data,
            headers={"Content-Type": self._content_type},
        )
        self._raise_for_status(response, path)

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
            self._raise_for_status(response, prefix)
            body = self._json(response)

            for item in body.get("items", []):
                name = item.get("name")
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

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status == 404:
            raise ObjectNotFound(path)
        if status == 412:
            raise ObjectAlreadyExists(path)
        # Everything else — including 401 and 403 — is unavailability rather than absence.
        # A misconfigured service account reading as "no such trip" would be a data-loss
        # bug: the API would answer 404, and a client could then create over live data.
        msg = f"Cloud Storage returned HTTP {status} for {path!r}: {self._error_message(response)}"
        raise ObjectStoreUnavailable(msg)

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
