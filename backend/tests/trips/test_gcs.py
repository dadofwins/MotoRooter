"""Cloud Storage adapter, driven entirely against a fake JSON API. Never hits the network."""

import asyncio
from typing import Any

import httpx
import pytest
import respx

from motorooter.clock import FakeClock
from motorooter.trips.gcs import (
    METADATA_TOKEN_URL,
    AnonymousTokenSource,
    GcsObjectStore,
    MetadataServerTokenSource,
    StaticTokenSource,
)
from motorooter.trips.objects import (
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStoreUnavailable,
)
from tests.trips.fake_gcs import BASE_URL, BUCKET, FakeGcs
from tests.trips.object_store_contract import ObjectStoreContract


@pytest.fixture
def fake_gcs():
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeGcs()
        fake.install(mock)
        yield fake


def build_store(**overrides: Any) -> GcsObjectStore:
    kwargs: dict[str, Any] = {
        "bucket": BUCKET,
        "base_url": BASE_URL,
        "token_source": StaticTokenSource("test-token"),
    }
    return GcsObjectStore(**(kwargs | overrides))


class TestGcsObjectStoreContract(ObjectStoreContract):
    @pytest.fixture
    def objects(self, fake_gcs):
        return build_store()


class TestRequestShape:
    async def test_object_name_slashes_are_percent_encoded(self, fake_gcs):
        """`/` unencoded in the path would address a different resource and 404."""
        await build_store().write("trips/a/trip.json", b"x")
        await build_store().read("trips/a/trip.json")
        get = next(r for r in fake_gcs.requests if r.method == "GET")
        assert "trips%2Fa%2Ftrip.json" in get.url.raw_path.decode()

    async def test_create_sends_the_if_generation_match_precondition(self, fake_gcs):
        """`ifGenerationMatch=0` is what makes create-if-absent atomic instead of racy."""
        await build_store().write("trips/a/trip.json", b"x", if_absent=True)
        post = next(r for r in fake_gcs.requests if r.method == "POST")
        assert "ifGenerationMatch=0" in post.url.raw_path.decode()

    async def test_overwrite_sends_no_precondition(self, fake_gcs):
        await build_store().write("trips/a/trip.json", b"x")
        post = next(r for r in fake_gcs.requests if r.method == "POST")
        assert "ifGenerationMatch" not in post.url.raw_path.decode()

    async def test_bearer_token_is_attached(self, fake_gcs):
        await build_store().write("trips/a/trip.json", b"x")
        assert fake_gcs.requests[0].headers["authorization"] == "Bearer test-token"

    async def test_anonymous_source_sends_no_authorization(self, fake_gcs):
        """Emulators and public buckets take no credentials; an empty header is not one."""
        await build_store(token_source=AnonymousTokenSource()).write("trips/a/trip.json", b"x")
        assert "authorization" not in fake_gcs.requests[0].headers

    async def test_exists_does_not_download_the_object(self, fake_gcs):
        """A trip document carries full geometry; probing for one must not pay that egress."""
        await build_store().write("trips/a/trip.json", b"x" * 1000)
        fake_gcs.requests.clear()
        assert await build_store().exists("trips/a/trip.json") is True
        assert all("alt=media" not in r.url.raw_path.decode() for r in fake_gcs.requests)

    async def test_content_type_is_json(self, fake_gcs):
        await build_store().write("trips/a/trip.json", b"{}")
        assert fake_gcs.requests[0].headers["content-type"] == "application/json"


class TestErrorTranslation:
    """Upstream failure modes become store-neutral errors, never raw httpx exceptions."""

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
    async def test_transient_upstream_failures_are_unavailable(self, status):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(return_value=httpx.Response(status))
            with pytest.raises(ObjectStoreUnavailable):
                await build_store().read("trips/a/trip.json")

    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failures_are_unavailable_not_missing(self, status):
        """A misconfigured service account must not read as 'this trip does not exist'."""
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(return_value=httpx.Response(status))
            with pytest.raises(ObjectStoreUnavailable):
                await build_store().read("trips/a/trip.json")

    async def test_missing_object_is_not_found(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(return_value=httpx.Response(404))
            with pytest.raises(ObjectNotFound):
                await build_store().read("trips/a/trip.json")

    async def test_precondition_failure_is_already_exists(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(return_value=httpx.Response(412))
            with pytest.raises(ObjectAlreadyExists):
                await build_store().write("trips/a/trip.json", b"x", if_absent=True)

    async def test_transport_error_is_unavailable(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(
                side_effect=httpx.ConnectError("no route to host")
            )
            with pytest.raises(ObjectStoreUnavailable):
                await build_store().read("trips/a/trip.json")

    async def test_unparseable_listing_is_unavailable(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(
                return_value=httpx.Response(200, content=b"<html>proxy error</html>")
            )
            with pytest.raises(ObjectStoreUnavailable):
                await build_store().list_prefix("trips/")

    async def test_exists_propagates_real_failures(self):
        """Treating a 503 as absence would let `create` overwrite a live trip."""
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host=httpx.URL(BASE_URL).host).mock(return_value=httpx.Response(503))
            with pytest.raises(ObjectStoreUnavailable):
                await build_store().exists("trips/a/trip.json")


class TestMetadataServerTokenSource:
    """Cloud Run's ambient credentials, fetched over plain HTTP with no client library."""

    @pytest.fixture
    def metadata(self):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(METADATA_TOKEN_URL).mock(
                return_value=httpx.Response(
                    200, json={"access_token": "minted-1", "expires_in": 3600}
                )
            )
            yield route

    async def test_fetches_a_token(self, metadata):
        source = MetadataServerTokenSource(clock=FakeClock())
        assert await source.token() == "minted-1"

    async def test_sends_the_metadata_flavor_header(self, metadata):
        """Without it the metadata server refuses, as a DNS-rebinding defence."""
        await MetadataServerTokenSource(clock=FakeClock()).token()
        assert metadata.calls.last.request.headers["metadata-flavor"] == "Google"

    async def test_caches_until_the_token_nears_expiry(self, metadata):
        clock = FakeClock()
        source = MetadataServerTokenSource(clock=clock)
        await source.token()
        clock.advance(3000)
        await source.token()
        assert metadata.call_count == 1

    async def test_refreshes_before_the_token_actually_expires(self, metadata):
        """Refreshing early avoids a race where the token dies mid-flight."""
        clock = FakeClock()
        source = MetadataServerTokenSource(clock=clock)
        await source.token()
        metadata.mock(
            return_value=httpx.Response(200, json={"access_token": "minted-2", "expires_in": 3600})
        )
        clock.advance(3599)
        assert await source.token() == "minted-2"
        assert metadata.call_count == 2

    async def test_concurrent_callers_mint_only_one_token(self):
        """Listing fans out a read per trip; a cold cache must not fan out a token each.

        The side effect suspends before answering. Without that, the first caller runs to
        completion before the second starts and the test would pass with no cache at all.
        """
        calls = 0

        async def slow_metadata_server(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "minted", "expires_in": 3600})

        with respx.mock(assert_all_called=False) as mock:
            mock.get(METADATA_TOKEN_URL).mock(side_effect=slow_metadata_server)
            source = MetadataServerTokenSource(clock=FakeClock())
            tokens = await asyncio.gather(*(source.token() for _ in range(8)))

        assert tokens == ["minted"] * 8
        assert calls == 1

    async def test_metadata_server_failure_is_unavailable(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(METADATA_TOKEN_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(ObjectStoreUnavailable):
                await MetadataServerTokenSource(clock=FakeClock()).token()

    async def test_unreachable_metadata_server_is_unavailable(self):
        """Running the GCS store off Cloud Run should fail with a clear cause, not a hang."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(METADATA_TOKEN_URL).mock(side_effect=httpx.ConnectError("no metadata server"))
            with pytest.raises(ObjectStoreUnavailable):
                await MetadataServerTokenSource(clock=FakeClock()).token()
