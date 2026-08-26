"""API contract.

These tests are the frozen seam between the two engineers. The backend engineer must keep
them passing; the frontend engineer can rely on every shape asserted here. Changing an
assertion means changing the contract, which is an integrator decision.
"""

import pytest
from fastapi.testclient import TestClient

from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings
from motorooter.routing.models import LegIntent


@pytest.fixture
def client():
    return TestClient(create_app(RoutingSettings(offline=True)))


@pytest.fixture
def trip(client):
    return client.post("/api/trips", json={"name": "Oregon Backcountry"}).json()


class TestHealth:
    def test_reports_registered_providers(self, client):
        assert client.get("/api/health").json() == {"status": "ok", "providers": ["fake"]}


class TestRoutingCapabilities:
    def test_lists_every_configured_intent(self, client):
        intents = client.get("/api/routing/capabilities").json()["intents"]
        assert set(intents) == {i.value for i in LegIntent}

    def test_exposes_the_drag_throttle_budget(self, client):
        """The frontend reads this instead of hardcoding a per-engine constant."""
        intents = client.get("/api/routing/capabilities").json()["intents"]
        assert intents[LegIntent.UNPAVED.value]["live_update_interval_ms"] == 0

    def test_exposes_provider_capabilities(self, client):
        providers = client.get("/api/routing/capabilities").json()["providers"]
        assert providers[0]["name"] == "fake"
        assert "prefers_unpaved" in providers[0]


class TestRouteLeg:
    @staticmethod
    def body(**overrides):
        return {
            "waypoints": [
                {"lat": 45.5152, "lon": -122.6784},
                {"lat": 45.3311, "lon": -121.7113},
            ],
            "intent": LegIntent.UNPAVED.value,
        } | overrides

    def test_routes_a_leg(self, client):
        response = client.post("/api/routing/leg", json=self.body())
        assert response.status_code == 200
        assert response.json()["leg"]["distance_m"] > 0

    def test_returns_geometry_as_lat_lon_objects(self, client):
        leg = client.post("/api/routing/leg", json=self.body()).json()["leg"]
        assert set(leg["geometry"][0]) == {"lat", "lon"}

    def test_echoes_the_requested_intent(self, client):
        leg = client.post("/api/routing/leg", json=self.body()).json()["leg"]
        assert leg["intent"] == LegIntent.UNPAVED.value

    def test_reports_the_throttle_budget_for_the_serving_engine(self, client):
        response = client.post("/api/routing/leg", json=self.body()).json()
        assert response["live_update_interval_ms"] == 0

    def test_surface_spans_are_present_in_the_schema(self, client):
        leg = client.post("/api/routing/leg", json=self.body()).json()["leg"]
        assert "surface_spans" in leg

    def test_rejects_a_single_waypoint(self, client):
        body = self.body(waypoints=[{"lat": 45.0, "lon": -121.0}])
        assert client.post("/api/routing/leg", json=body).status_code == 422

    def test_rejects_out_of_range_coordinates(self, client):
        body = self.body(waypoints=[{"lat": 91.0, "lon": 0.0}, {"lat": 45.0, "lon": -121.0}])
        assert client.post("/api/routing/leg", json=body).status_code == 422

    def test_unknown_provider_override_is_404(self, client):
        response = client.post("/api/routing/leg", json=self.body(provider_override="valhalla"))
        assert response.status_code == 404
        assert response.json()["code"] == "provider_not_found"


class TestTripCrud:
    def test_create_returns_201_and_a_derived_slug(self, client):
        response = client.post("/api/trips", json={"name": "Oregon Backcountry"})
        assert response.status_code == 201
        assert response.json()["slug"] == "oregon-backcountry"

    def test_create_accepts_an_explicit_slug(self, client):
        response = client.post("/api/trips", json={"name": "Trip", "slug": "wabdr-2026"})
        assert response.json()["slug"] == "wabdr-2026"

    def test_create_rejects_a_traversal_slug(self, client):
        """The security boundary: slugs become storage paths."""
        response = client.post("/api/trips", json={"name": "x", "slug": "../etc/passwd"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_slug"

    def test_create_rejects_a_duplicate_slug(self, client, trip):
        response = client.post("/api/trips", json={"name": "Oregon Backcountry"})
        assert response.status_code == 409
        assert response.json()["code"] == "trip_already_exists"

    def test_new_trip_starts_empty_and_needs_a_replan(self, client, trip):
        assert trip["waypoints"] == []
        assert trip["legs"] == []

    def test_get_returns_the_trip(self, client, trip):
        assert client.get(f"/api/trips/{trip['slug']}").json()["name"] == "Oregon Backcountry"

    def test_get_missing_is_404(self, client):
        response = client.get("/api/trips/no-such-trip")
        assert response.status_code == 404
        assert response.json()["code"] == "trip_not_found"

    @pytest.mark.parametrize("attack", ["..", "a%2Fb", "%2e%2e%2fetc"])
    def test_separators_and_dot_segments_never_reach_the_handler(self, client, attack):
        """First layer: URL routing refuses to bind these to a single path segment."""
        assert client.get(f"/api/trips/{attack}").status_code == 404

    @pytest.mark.parametrize("bad", ["trip.json", "UPPER", "-leading", "api"])
    def test_handler_rejects_slugs_that_are_valid_path_segments(self, client, bad):
        """Second layer: these do reach validate_slug, and must be refused there."""
        response = client.get(f"/api/trips/{bad}")
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_slug"

    def test_list_returns_summaries(self, client, trip):
        summaries = client.get("/api/trips").json()
        assert summaries[0]["slug"] == "oregon-backcountry"
        assert "legs" not in summaries[0], "index must not ship geometry"

    def test_summary_exposes_the_replan_flag(self, client, trip):
        assert client.get("/api/trips").json()[0]["needs_replan"] is True

    def test_delete_removes_the_trip(self, client, trip):
        assert client.delete(f"/api/trips/{trip['slug']}").status_code == 204
        assert client.get(f"/api/trips/{trip['slug']}").status_code == 404

    def test_delete_missing_is_404(self, client):
        assert client.delete("/api/trips/no-such-trip").status_code == 404


class TestTripUpdate:
    @staticmethod
    def waypoints():
        return [
            {"coordinate": {"lat": 45.0, "lon": -121.0}, "name": "Start", "pinned": True},
            {"coordinate": {"lat": 46.0, "lon": -121.0}, "name": None, "pinned": True},
        ]

    def test_updates_waypoints(self, client, trip):
        response = client.put(f"/api/trips/{trip['slug']}", json={"waypoints": self.waypoints()})
        assert response.status_code == 200
        assert len(response.json()["waypoints"]) == 2

    def test_renaming_does_not_mark_discovery_stale(self, client, trip):
        """edited_at drives the replan flag; a rename changes no geometry."""
        before = client.get(f"/api/trips/{trip['slug']}").json()["edited_at"]
        after = client.put(f"/api/trips/{trip['slug']}", json={"name": "New Name"}).json()
        assert after["edited_at"] == before
        assert after["name"] == "New Name"

    def test_changing_geometry_advances_edited_at(self, client, trip):
        before = client.get(f"/api/trips/{trip['slug']}").json()["edited_at"]
        after = client.put(f"/api/trips/{trip['slug']}", json={"waypoints": self.waypoints()})
        assert after.json()["edited_at"] > before

    def test_rejects_legs_referencing_missing_waypoints(self, client, trip):
        body = {
            "waypoints": self.waypoints(),
            "legs": [
                {
                    "intent": LegIntent.UNPAVED.value,
                    "start_waypoint_index": 0,
                    "end_waypoint_index": 9,
                }
            ],
        }
        assert client.put(f"/api/trips/{trip['slug']}", json=body).status_code == 422

    def test_rejects_non_contiguous_legs(self, client, trip):
        waypoints = [
            {"coordinate": {"lat": 45.0 + i, "lon": -121.0}, "pinned": True} for i in range(4)
        ]
        body = {
            "waypoints": waypoints,
            "legs": [
                {"intent": "unpaved", "start_waypoint_index": 0, "end_waypoint_index": 1},
                {"intent": "unpaved", "start_waypoint_index": 2, "end_waypoint_index": 3},
            ],
        }
        assert client.put(f"/api/trips/{trip['slug']}", json=body).status_code == 422

    def test_rejects_an_unverified_poi_pinned_to_the_route(self, client, trip):
        """LLM output is candidates only; unresolved ones must never reach the route."""
        body = {
            "pois": [
                {
                    "id": "p1",
                    "name": "Hallucinated camp",
                    "category": "wild_camp",
                    "coordinate": {"lat": 45.0, "lon": -121.0},
                    "source": "llm_suggested",
                    "on_route": True,
                }
            ]
        }
        assert client.put(f"/api/trips/{trip['slug']}", json=body).status_code == 422

    def test_accepts_a_verified_poi_pinned_to_the_route(self, client, trip):
        body = {
            "pois": [
                {
                    "id": "p1",
                    "name": "Real camp",
                    "category": "wild_camp",
                    "coordinate": {"lat": 45.0, "lon": -121.0},
                    "source": "llm_suggested",
                    "place_id": "ChIJ_real",
                    "on_route": True,
                }
            ]
        }
        assert client.put(f"/api/trips/{trip['slug']}", json=body).status_code == 200

    def test_update_missing_trip_is_404(self, client):
        assert client.put("/api/trips/no-such-trip", json={"name": "x"}).status_code == 404


class TestErrorEnvelope:
    """Every error body must match the declared `ErrorResponse` shape.

    Previously only status codes were asserted, which let FastAPI's built-in 422 handler —
    `{"detail": [ ... ]}`, no `code`, detail as a list — contradict the OpenAPI document
    unnoticed. Assert the shape, not just the status.
    """

    @staticmethod
    def assert_envelope(response, code: str):
        body = response.json()
        assert set(body) == {"code", "detail"}, f"unexpected keys: {sorted(body)}"
        assert body["code"] == code
        assert isinstance(body["detail"], str), "detail must be a string, never a list"

    def test_malformed_body_uses_the_envelope(self, client):
        response = client.post(
            "/api/routing/leg", json={"waypoints": [], "intent": LegIntent.UNPAVED.value}
        )
        assert response.status_code == 422
        self.assert_envelope(response, "validation_error")

    def test_domain_validator_failure_uses_the_envelope(self, client, trip):
        """Unverified POI pinned to the route — raised during body parsing, not after."""
        body = {
            "pois": [
                {
                    "id": "p1",
                    "name": "Hallucinated camp",
                    "category": "wild_camp",
                    "coordinate": {"lat": 45.0, "lon": -121.0},
                    "source": "llm_suggested",
                    "on_route": True,
                }
            ]
        }
        response = client.put(f"/api/trips/{trip['slug']}", json=body)
        assert response.status_code == 422
        self.assert_envelope(response, "validation_error")

    def test_not_found_uses_the_envelope(self, client):
        self.assert_envelope(client.get("/api/trips/no-such-trip"), "trip_not_found")

    def test_invalid_slug_uses_the_envelope(self, client):
        self.assert_envelope(client.get("/api/trips/UPPER"), "invalid_slug")

    def test_conflict_uses_the_envelope(self, client, trip):
        response = client.post("/api/trips", json={"name": "Oregon Backcountry"})
        self.assert_envelope(response, "trip_already_exists")

    def test_unimplemented_uses_the_envelope(self, client, trip):
        """501 carries a code too, so clients switch on `code` uniformly."""
        response = client.post(f"/api/trips/{trip['slug']}/replan", json={})
        assert response.status_code == 501
        self.assert_envelope(response, "not_implemented")


class TestReplanStreamContract:
    """Replan streams NDJSON, not SSE.

    It is a POST with a body, so `EventSource` cannot consume it; hand-parsing SSE framing
    over `fetch` would cost the framing overhead for none of the benefit.
    """

    @pytest.fixture
    def replan_spec(self, client):
        return client.get("/openapi.json").json()["paths"]["/api/trips/{slug}/replan"]["post"]

    def test_success_media_type_is_ndjson(self, replan_spec):
        assert list(replan_spec["responses"]["200"]["content"]) == ["application/x-ndjson"]

    def test_stream_lines_are_replan_events(self, replan_spec):
        schema = replan_spec["responses"]["200"]["content"]["application/x-ndjson"]["schema"]
        assert schema["$ref"].endswith("/ReplanEvent")

    def test_framing_is_documented(self, replan_spec):
        description = replan_spec["description"]
        assert "x-ndjson" in description
        assert "Not Server-Sent Events" in description

    def test_errors_on_this_route_are_json_not_ndjson(self, replan_spec):
        """response_class would otherwise make every response on the route a stream."""
        assert list(replan_spec["responses"]["501"]["content"]) == ["application/json"]

    def test_error_schema_is_a_clean_ref(self, replan_spec):
        """A `type` merged onto the $ref generates the wrong frontend type."""
        schema = replan_spec["responses"]["501"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ErrorResponse"}


class TestReservedEndpoints:
    """Stubs return 501 so the frontend can tell 'not built' from 'broken'."""

    def test_replan_is_501(self, client, trip):
        assert client.post(f"/api/trips/{trip['slug']}/replan", json={}).status_code == 501

    def test_replan_on_a_missing_trip_is_404_not_501(self, client):
        assert client.post("/api/trips/no-such-trip/replan", json={}).status_code == 404

    def test_place_detail_is_501(self, client):
        assert client.get("/api/places/ChIJ_example").status_code == 501


class TestOpenApiContract:
    """The generated TypeScript types come from this document."""

    @pytest.fixture
    def schema(self, client):
        return client.get("/openapi.json").json()

    def test_every_contract_endpoint_is_documented(self, schema):
        expected = {
            "/api/health",
            "/api/routing/capabilities",
            "/api/routing/leg",
            "/api/trips",
            "/api/trips/{slug}",
            "/api/trips/{slug}/replan",
            "/api/trips/{slug}/gpx",
            "/api/places/{place_id}",
        }
        assert expected <= set(schema["paths"])

    def test_unimplemented_endpoints_still_publish_their_schemas(self, schema):
        """This is what lets the frontend build the POI dialog before Places lands."""
        assert "PoiDetail" in schema["components"]["schemas"]
        assert "ReplanEvent" in schema["components"]["schemas"]

    def test_core_domain_types_are_published(self, schema):
        names = set(schema["components"]["schemas"])
        assert {"Trip", "TripSummary", "Poi", "RouteLeg", "Coordinate", "Waypoint"} <= names

    def test_spa_fallback_is_not_in_the_schema(self, schema):
        assert "/{full_path}" not in schema["paths"]


class TestLegDurationIsDerived:
    """The provider's duration is a bicycle's. The response carries ours.

    Per-leg rather than a trip total, because the frontend would otherwise reimplement
    surface-weighted speeds client-side — which is exactly the drift the generated contract
    exists to prevent, and it would land first on the surface question.
    """

    @pytest.fixture
    def routed(self, client):
        body = {
            "waypoints": [
                {"lat": 45.5152, "lon": -122.6784},
                {"lat": 45.3311, "lon": -121.7113},
            ],
            "intent": "unpaved",
        }
        return client.post("/api/routing/leg", json=body).json()

    def test_the_response_carries_an_estimate(self, routed):
        assert routed["estimated_duration_s"] > 0

    def test_it_is_not_the_providers_figure(self, routed):
        """`FakeProvider` assumes ~54 km/h flat; the estimate weights by surface."""
        assert routed["estimated_duration_s"] != routed["leg"]["duration_s"]

    def test_the_providers_figure_is_still_reported(self, routed):
        """Honest about what the engine said, even though nothing user-facing reads it."""
        assert routed["leg"]["duration_s"] > 0

    def test_a_longer_leg_takes_longer(self, client):
        def duration(lat: float) -> float:
            body = {
                "waypoints": [{"lat": 45.0, "lon": -121.0}, {"lat": lat, "lon": -121.0}],
                "intent": "unpaved",
            }
            estimate = client.post("/api/routing/leg", json=body).json()["estimated_duration_s"]
            return float(estimate)

        assert duration(46.0) < duration(48.0)


class TestALegCarriesTheRequestItCameFrom:
    """The wiring, not the type.

    `routed_from` existed, was contract-approved, had forty passing tests, and was `null` on
    every live response — because only the trip path attached it and the drag uses this one.
    Every test was a model test; nothing asserted that anything ever called it, and the
    field being optional is exactly what let it stay empty without complaint.

    The client treats a missing fingerprint as stale, correctly. So a drag routed on release,
    handed the leg back, and the hook immediately requested the same route again.
    """

    @pytest.fixture
    def body(self):
        return {
            "waypoints": [
                {"lat": 45.5152, "lon": -122.6784},
                {"lat": 45.3311, "lon": -121.7113},
            ],
            "intent": "unpaved",
        }

    def test_the_fast_path_returns_a_fingerprint(self, client, body):
        leg = client.post("/api/routing/leg", json=body).json()["leg"]
        assert leg["routed_from"] is not None

    def test_the_fingerprint_holds_the_requested_waypoints(self, client, body):
        leg = client.post("/api/routing/leg", json=body).json()["leg"]
        sent = [(point["lat"], point["lon"]) for point in body["waypoints"]]
        got = [(point["lat"], point["lon"]) for point in leg["routed_from"]["waypoints"]]
        assert got == pytest.approx(sent)

    def test_the_fingerprint_holds_the_requested_intent(self, client, body):
        leg = client.post("/api/routing/leg", json=body).json()["leg"]
        assert leg["routed_from"]["intent"] == "unpaved"

    def test_a_pinned_provider_is_recorded(self, client, body):
        """Repinning a leg changes what it should be routed by, so it changes freshness."""
        leg = client.post("/api/routing/leg", json={**body, "provider_override": "fake"}).json()
        assert leg["leg"]["routed_from"]["provider_override"] == "fake"

    def test_no_override_is_recorded_as_none(self, client, body):
        leg = client.post("/api/routing/leg", json=body).json()["leg"]
        assert leg["routed_from"]["provider_override"] is None

    def test_routing_the_same_request_twice_gives_the_same_fingerprint(self, client, body):
        """Which is what makes it usable as a staleness check at all."""
        first = client.post("/api/routing/leg", json=body).json()["leg"]["routed_from"]
        second = client.post("/api/routing/leg", json=body).json()["leg"]["routed_from"]
        assert first == second

    def test_a_moved_waypoint_gives_a_different_fingerprint(self, client, body):
        first = client.post("/api/routing/leg", json=body).json()["leg"]["routed_from"]
        moved = {**body, "waypoints": [body["waypoints"][0], {"lat": 46.0, "lon": -121.0}]}
        second = client.post("/api/routing/leg", json=moved).json()["leg"]["routed_from"]
        assert first != second


class TestGpxExport:
    """`GET /trips/{slug}/gpx`. Frontend's download has been waiting on this."""

    @staticmethod
    def _routed(client, trip):
        slug = trip["slug"]
        leg = client.post(
            "/api/routing/leg",
            json={
                "waypoints": [{"lat": 46.97, "lon": -121.53}, {"lat": 46.87, "lon": -121.52}],
                "intent": "unpaved",
            },
        ).json()["leg"]
        client.put(
            f"/api/trips/{slug}",
            json={
                "waypoints": [
                    {"coordinate": {"lat": 46.97, "lon": -121.53}, "name": "Start"},
                    {"coordinate": {"lat": 46.87, "lon": -121.52}, "name": "End"},
                ],
                "legs": [
                    {
                        "intent": "unpaved",
                        "start_waypoint_index": 0,
                        "end_waypoint_index": 1,
                        "routed": leg,
                    }
                ],
            },
        )
        return slug

    def test_it_answers_ok(self, client, trip):
        slug = self._routed(client, trip)
        assert client.get(f"/api/trips/{slug}/gpx").status_code == 200

    def test_the_content_type_is_gpx(self, client, trip):
        """Declared so a browser and a desktop tool both know what they have."""
        slug = self._routed(client, trip)
        response = client.get(f"/api/trips/{slug}/gpx")
        assert response.headers["content-type"].startswith("application/gpx+xml")

    def test_the_body_is_a_gpx_document(self, client, trip):
        import xml.etree.ElementTree as ElementTree

        slug = self._routed(client, trip)
        root = ElementTree.fromstring(client.get(f"/api/trips/{slug}/gpx").text)
        assert root.tag.endswith("gpx")
        assert root.findall(".//{http://www.topografix.com/GPX/1/1}trkpt")

    def test_an_unknown_trip_is_404(self, client):
        assert client.get("/api/trips/no-such-trip/gpx").status_code == 404

    def test_an_unrouted_trip_still_exports(self, client, trip):
        """A rider who has placed points but not routed them gets their waypoints. Refusing
        would be a worse answer than a file with no track in it."""
        response = client.get(f"/api/trips/{trip['slug']}/gpx")
        assert response.status_code == 200
