"""Brave search adapter, driven entirely by recorded response shapes. Never hits the network."""

import contextlib
from typing import Any

import httpx
import pytest
import respx

from motorooter.planning.discovery.errors import (
    DiscoveryError,
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.planning.discovery.queries import queries_for
from motorooter.planning.discovery.sources.brave import BRAVE_SEARCH_URL, BraveSearchSource
from motorooter.trips.models import PoiCategory
from tests.planning.discovery.source_contract import ANCHOR, SearchSourceContract


def result(title: str = "Bear Creek dispersed camping", **overrides: Any) -> dict[str, Any]:
    return {
        "title": title,
        "url": "https://example.test/bear-creek",
        "description": "A flat pull-off about 3 miles up the forest road. Washes out in spring.",
    } | overrides


def body(*results: dict[str, Any]) -> dict[str, Any]:
    return {"web": {"results": list(results or (result(),))}}


@pytest.fixture
def mock_brave():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=body(result("A"), result("B"), result("C")))
        )
        yield mock


class TestBraveContract(SearchSourceContract):
    @pytest.fixture
    def source(self, mock_brave):
        return BraveSearchSource(api_key="brave-test-key")


class TestTheRequest:
    async def test_the_query_text_is_sent(self, mock_brave):
        query = queries_for("Chinook Pass", [PoiCategory.WILD_CAMP])[0]
        await BraveSearchSource(api_key="k").search(query, near=ANCHOR)
        assert "Chinook" in mock_brave.calls.last.request.url.params["q"]

    async def test_the_key_goes_in_the_subscription_header(self, mock_brave):
        await BraveSearchSource(api_key="brave-test-key").search(
            queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
        )
        headers = mock_brave.calls.last.request.headers
        assert headers["x-subscription-token"] == "brave-test-key"

    async def test_the_key_is_never_a_query_parameter(self, mock_brave):
        """A key in the URL leaks into logs, referrers and error messages."""
        await BraveSearchSource(api_key="brave-test-key").search(
            queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
        )
        assert "brave-test-key" not in str(mock_brave.calls.last.request.url)

    async def test_the_result_count_is_requested_not_just_trimmed(self, mock_brave):
        """Asking for five and discarding fifteen wastes someone's bandwidth and our time."""
        await BraveSearchSource(api_key="k").search(
            queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR, limit=3
        )
        assert mock_brave.calls.last.request.url.params["count"] == "3"


class TestReadingResults:
    async def test_the_title_becomes_the_candidate_name(self, mock_brave):
        found = await BraveSearchSource(api_key="k").search(
            queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
        )
        assert found[0].name == "A"

    async def test_the_description_is_kept_as_the_snippet(self, mock_brave):
        """It is the judge's actual evidence — "washes out in spring" is not in any metric."""
        found = await BraveSearchSource(api_key="k").search(
            queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
        )
        assert "spring" in (found[0].snippet or "")

    async def test_the_url_is_kept_so_a_human_can_check(self, mock_brave):
        found = await BraveSearchSource(api_key="k").search(
            queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
        )
        assert found[0].url == "https://example.test/bear-creek"

    async def test_no_candidate_claims_a_coordinate(self):
        """Brave returns web pages. Any coordinate it implied would be a guess, and a guess
        that looked authoritative is exactly what the resolve stage exists to prevent."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body())
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert all(candidate.claimed_coordinate is None for candidate in found)

    async def test_markup_is_stripped_from_the_snippet(self):
        """Brave bolds matched terms with real HTML tags.

        Found by running the spike, not by a unit test. The snippet is the judge's evidence
        and is shown to a rider, so `<strong>Road to Snag Lake</strong>` reaching either is
        wrong — it is noise to a model and markup-as-text to a human.
        """
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=body(
                        result(description="the most popular is <strong>Snag Lake</strong> now")
                    ),
                )
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert found[0].snippet == "the most popular is Snag Lake now"

    async def test_html_entities_are_decoded(self):
        """`&amp;` and `&#x27;` read as garbage in a UI and as noise to a model."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, json=body(result(description="Bob&#x27;s Diner &amp; Fuel"))
                )
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
            )
        assert found[0].snippet == "Bob's Diner & Fuel"

    async def test_markup_is_stripped_from_the_title_too(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=body(result("<strong>Bear</strong> Creek")))
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert found[0].name == "Bear Creek"

    async def test_a_result_with_no_title_is_skipped_not_fatal(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, json={"web": {"results": [{"url": "https://x.test"}, result("Kept")]}}
                )
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert [candidate.name for candidate in found] == ["Kept"]

    async def test_no_results_is_an_empty_tuple(self):
        """Most corridor-and-category pairs genuinely find nothing."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"web": {"results": []}})
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert found == ()

    async def test_a_body_with_no_web_key_is_empty_not_an_error(self):
        """Brave omits `web` entirely when nothing matched."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"query": {"original": "x"}})
            )
            found = await BraveSearchSource(api_key="k").search(
                queries_for("Naches", [PoiCategory.WILD_CAMP])[0], near=ANCHOR
            )
        assert found == ()


MALFORMED: list[Any] = [
    {"web": None},
    {"web": {"results": None}},
    {"web": {"results": "not-a-list"}},
    {"web": {"results": [None]}},
    {"web": {"results": ["a string"]}},
    [],
    "a bare string",
    None,
]


class TestNothingButADiscoveryErrorEscapes:
    """The invariant, not a list of the exceptions I happened to think of."""

    @pytest.mark.parametrize("malformed", MALFORMED)
    async def test_a_malformed_body_never_raises_something_else(self, malformed):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=malformed)
            )
            # Either it degrades to an empty result or it raises a translated error.
            # Anything else — a TypeError from dict access, a raw ValidationError —
            # propagates and fails the test, which is the invariant being asserted.
            with contextlib.suppress(DiscoveryError):
                await BraveSearchSource(api_key="k").search(
                    queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
                )

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
    async def test_any_error_status_raises_only_a_discovery_error(self, status):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(
                return_value=httpx.Response(status, text="whatever")
            )
            with pytest.raises(DiscoveryError):
                await BraveSearchSource(api_key="k").search(
                    queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
                )

    @pytest.mark.parametrize("error", [httpx.ConnectError("no route"), httpx.ReadTimeout("slow")])
    async def test_any_transport_error_raises_only_a_discovery_error(self, error):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(side_effect=error)
            with pytest.raises(DiscoveryError):
                await BraveSearchSource(api_key="k").search(
                    queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
                )


class TestWhichDiscoveryError:
    """Callers act on the difference, so the specific translation matters too."""

    @staticmethod
    async def _raises(status: int, expected: type[DiscoveryError]) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=BRAVE_SEARCH_URL).mock(return_value=httpx.Response(status))
            with pytest.raises(expected):
                await BraveSearchSource(api_key="k").search(
                    queries_for("Naches", [PoiCategory.FOOD])[0], near=ANCHOR
                )

    async def test_rate_limiting_is_retryable(self):
        await self._raises(429, DiscoveryRateLimited)

    async def test_a_spent_plan_is_not(self):
        await self._raises(402, DiscoveryQuotaExceeded)

    async def test_a_bad_key_is_a_refusal(self):
        await self._raises(401, DiscoveryRefused)

    async def test_an_upstream_fault_is_unavailability(self):
        await self._raises(503, DiscoveryUnavailable)

    async def test_rate_limited_is_retryable_and_quota_is_not(self):
        assert DiscoveryRateLimited("x").retryable is True
        assert DiscoveryQuotaExceeded("x").retryable is False
