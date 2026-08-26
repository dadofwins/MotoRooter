"""Turning a corridor anchor into a name something can search for.

The missing stage. Brave cannot search "46.87,-121.52", so until now the spike used
hand-picked place names — which is also why its search geography and its corridor geography
disagreed, and why places 100 km away kept turning up and being correctly discarded.

Naming the anchor closes that: the query is built from the same coordinate the distance
filter measures against, so the two agree by construction rather than by me choosing names
that happen to be near the route.
"""

from typing import Any

import httpx
import pytest
import respx

from motorooter.planning.discovery.errors import DiscoveryError, DiscoveryRefused
from motorooter.planning.discovery.naming import GEOCODE_URL, PlaceNamer
from motorooter.routing.models import Coordinate

ANCHOR = Coordinate(lat=46.8722, lon=-121.5165)


def result(*components: dict[str, Any], formatted: str = "Chinook Pass, WA 98937, USA"):
    return {
        "status": "OK",
        "results": [{"formatted_address": formatted, "address_components": list(components)}],
    }


def component(name: str, *types: str) -> dict[str, Any]:
    return {"long_name": name, "short_name": name, "types": list(types)}


@pytest.fixture
def mock_geocode():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=GEOCODE_URL).mock(
            return_value=httpx.Response(
                200,
                json=result(
                    component("Chinook Pass", "natural_feature"),
                    component("Yakima County", "administrative_area_level_2"),
                    component("Washington", "administrative_area_level_1"),
                ),
            )
        )
        yield mock


class TestNamingAnAnchor:
    async def test_it_returns_a_searchable_name(self, mock_geocode):
        assert await PlaceNamer(api_key="k").name_for(ANCHOR) == "Chinook Pass"

    async def test_it_prefers_a_natural_feature_over_a_county(self):
        """A pass or a lake is what a rider searches; a county is not."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=result(
                        component("Yakima County", "administrative_area_level_2"),
                        component("Chinook Pass", "natural_feature"),
                    ),
                )
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) == "Chinook Pass"

    async def test_it_prefers_the_road_over_a_distant_town(self):
        """What a rural coordinate actually returns, and the bug it caused.

        Reverse geocoding a point on Chinook Pass gives the road as `route` and `Enumclaw` as
        the locality — fifty kilometres away over a mountain. Anchoring on the town searched
        the wrong place, and the distance filter then correctly discarded everything it
        found. The road is nearer and a better search term.
        """
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=result(
                        component("Mather Memorial Parkway", "route"),
                        component("Enumclaw", "locality"),
                    ),
                )
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) == "Mather Memorial Parkway"

    async def test_a_named_feature_still_beats_the_road(self):
        """ "Chinook Pass" is better than the highway number that crosses it."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=result(
                        component("Washington 410", "route"),
                        component("Chinook Pass", "natural_feature"),
                    ),
                )
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) == "Chinook Pass"

    async def test_it_falls_back_to_a_locality(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(200, json=result(component("Naches", "locality")))
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) == "Naches"

    async def test_it_falls_back_to_the_formatted_address(self):
        """Better a rough name than no search at all for that stretch of route."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(
                    200, json=result(formatted="Mount Rainier National Park, WA, USA")
                )
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) is not None

    async def test_nothing_found_is_none_not_a_coordinate(self):
        """A coordinate in a query is worse than no query — it matches nothing and costs a
        metered search to find that out."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) is None


class TestTheRegion:
    async def test_it_reports_the_state_for_disambiguation(self, mock_geocode):
        """ "Cayuse" matched Oregon on a Washington corridor. This is what fixes that."""
        assert await PlaceNamer(api_key="k").region_for(ANCHOR) == "Washington"

    async def test_no_region_is_none(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(200, json=result(component("X", "locality")))
            )
            assert await PlaceNamer(api_key="k").region_for(ANCHOR) is None


class TestItDoesNotPayTwice:
    async def test_the_same_anchor_is_looked_up_once(self, mock_geocode):
        namer = PlaceNamer(api_key="k")
        await namer.name_for(ANCHOR)
        await namer.name_for(ANCHOR)
        assert mock_geocode.calls.call_count == 1

    async def test_the_region_reuses_the_same_lookup(self, mock_geocode):
        """One request answers both questions; asking twice would double a metered call."""
        namer = PlaceNamer(api_key="k")
        await namer.name_for(ANCHOR)
        await namer.region_for(ANCHOR)
        assert mock_geocode.calls.call_count == 1

    async def test_nearly_identical_anchors_share_a_lookup(self, mock_geocode):
        """Rounded, like the routing cache key: two points a metre apart are one place."""
        namer = PlaceNamer(api_key="k")
        await namer.name_for(ANCHOR)
        await namer.name_for(Coordinate(lat=ANCHOR.lat + 1e-9, lon=ANCHOR.lon))
        assert mock_geocode.calls.call_count == 1


class TestFailure:
    async def test_a_denied_request_is_a_refusal(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(200, json={"status": "REQUEST_DENIED"})
            )
            with pytest.raises(DiscoveryRefused):
                await PlaceNamer(api_key="k").name_for(ANCHOR)

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    async def test_no_error_status_escapes_untranslated(self, status):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(return_value=httpx.Response(status))
            with pytest.raises(DiscoveryError):
                await PlaceNamer(api_key="k").name_for(ANCHOR)

    async def test_a_malformed_body_is_no_name_rather_than_a_crash(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(
                return_value=httpx.Response(200, json={"results": "not-a-list"})
            )
            assert await PlaceNamer(api_key="k").name_for(ANCHOR) is None

    async def test_the_key_is_never_logged_into_the_error(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(DiscoveryError) as caught:
                await PlaceNamer(api_key="super-secret-key").name_for(ANCHOR)
        assert "super-secret-key" not in str(caught.value)


class TestItNeverSearchesForACoordinate:
    """A plus code is a coordinate wearing a name, and the fallback was handing them out.

    Measured live: an anchor in national forest between Cle Elum and Cashmere reverse-geocodes
    to `formatted_address: "84VX9FP2+WM"` with `types: ['plus_code']` and no other component.
    The fallback took the first comma-separated field and searched for it — exactly what
    `name_for`'s own docstring forbids: "a coordinate in a web query matches nothing and costs
    a metered search to discover that".

    That anchor is not nameable. `None` says so, and the pipeline already hands back the
    search budget for an anchor it could not name.
    """

    @staticmethod
    async def _name(body):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=GEOCODE_URL).mock(return_value=httpx.Response(200, json=body))
            return await PlaceNamer(api_key="k").name_for(ANCHOR)

    async def test_a_plus_code_formatted_address_is_not_a_name(self):
        assert await self._name(result(formatted="84VX9FP2+WM")) is None

    async def test_a_plus_code_component_is_not_a_name(self):
        """`plus_code` is not in `_NAME_TYPES`, so the component is never selected and the
        fallback is what has to refuse it. Kept as a separate case because that reasoning is
        a property of the type list, not of this function — reorder the list and it changes.
        """
        body = result(component("84VX9FP2+WM", "plus_code"), formatted="84VX9FP2+WM")
        assert await self._name(body) is None

    async def test_a_plus_code_with_a_locality_uses_the_locality(self):
        """Google prefixes plus codes onto real addresses: "CFC5+X5 Cashmere, WA"."""
        body = result(
            component("CFC5+X5", "plus_code"),
            component("Cashmere", "locality", "political"),
            formatted="CFC5+X5 Cashmere, WA, USA",
        )
        assert await self._name(body) == "Cashmere"

    async def test_an_ordinary_fallback_still_works(self):
        """Only plus codes are refused. A real formatted address is still better than nothing
        for a stretch of route with no matching component."""
        assert await self._name(result(formatted="Blewett Pass, Washington, USA")) == "Blewett Pass"
