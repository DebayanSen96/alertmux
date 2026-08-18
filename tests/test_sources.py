from datetime import datetime, timezone

from fastapi.testclient import TestClient

from alertmux.adapters.base import FetchResult
from alertmux.api import app, clear_cache, get_adapters
from alertmux.query import collect
from alertmux.schema import NormalisedAlert, Provenance
from alertmux.sources import classify_hazard

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _alert(alert_id: str, source_id: str, authority: str, event: str) -> NormalisedAlert:
    return NormalisedAlert(
        id=alert_id,
        event=event,
        provenance=Provenance(
            authority=authority,
            source_id=source_id,
            source_url="https://example.test",
            retrieved_at=NOW,
        ),
    )


class FakeAdapter:
    source_id = "fake"
    URL = "https://example.test/feed"
    STRUCTURAL_GAPS = ("onset", "expires")

    def __init__(self, source_id, ok=True, alerts=None, error=None):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id,
            ok=ok,
            alerts=alerts or [],
            error=error,
            retrieved_at=NOW,
            latency_ms=5,
        )

    def fetch(self):
        return self._result


def _client(adapters):
    app.dependency_overrides[get_adapters] = lambda: adapters
    return TestClient(app)


def setup_function():
    clear_cache()


def teardown_function():
    app.dependency_overrides.clear()
    clear_cache()


def test_sources_lists_every_configured_adapter_including_a_failing_one():
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert("a:1", "wmo-swic", "ng-nimet", "Wildfire")]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    body = client.get("/sources").json()
    ids = {s["source_id"] for s in body["sources"]}
    assert ids == {"wmo-swic", "usgs"}


def test_a_failing_source_still_appears_with_ok_false():
    client = _client([FakeAdapter("usgs", ok=False, error="timeout")])
    body = client.get("/sources").json()
    usgs = next(s for s in body["sources"] if s["source_id"] == "usgs")
    assert usgs["ok"] is False
    assert usgs["alert_count"] == 0


def test_structural_gaps_matches_the_adapter_class_attribute():
    client = _client([FakeAdapter("wmo-swic")])
    body = client.get("/sources").json()
    swic = next(s for s in body["sources"] if s["source_id"] == "wmo-swic")
    assert swic["structural_gaps"] == sorted(FakeAdapter.STRUCTURAL_GAPS)


def test_authorities_reflects_alerts_actually_returned():
    client = _client([
        FakeAdapter(
            "wmo-swic",
            alerts=[
                _alert("a:1", "wmo-swic", "ng-nimet", "Wildfire"),
                _alert("a:2", "wmo-swic", "gh-gmet", "Flood"),
            ],
        ),
    ])
    body = client.get("/sources").json()
    swic = next(s for s in body["sources"] if s["source_id"] == "wmo-swic")
    assert swic["authorities"] == ["gh-gmet", "ng-nimet"]
    assert swic["authority_count"] == 2


def test_hazard_coverage_maps_a_known_event_string_to_the_right_family():
    assert classify_hazard("Wildfire") == "wildfire"
    assert classify_hazard("EARTHQUAKE") == "earthquake"
    assert classify_hazard("Heat Advisory") == "heat"

    client = _client([
        FakeAdapter("gdacs", alerts=[_alert("g:1", "gdacs", "gdacs", "Wildfire")]),
    ])
    body = client.get("/sources").json()
    assert body["hazard_coverage"]["wildfire"] == ["gdacs"]


def test_a_family_with_no_alerts_appears_in_uncovered_hazards():
    client = _client([
        FakeAdapter("gdacs", alerts=[_alert("g:1", "gdacs", "gdacs", "Wildfire")]),
    ])
    body = client.get("/sources").json()
    assert "tsunami" in body["uncovered_hazards"]
    assert "wildfire" not in body["uncovered_hazards"]


def test_sources_does_not_mutate_the_cached_response():
    """A later /alerts call must still return everything -- /sources is
    read-only over the shared cached object, per D9."""
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert("a:1", "wmo-swic", "ng-nimet", "Wildfire")]),
    ])
    client.get("/sources")
    body = client.get("/alerts").json()
    assert len(body["alerts"]) == 1


def test_sources_carries_the_relay_disclaimer():
    client = _client([FakeAdapter("wmo-swic")])
    assert "not a substitute" in client.get("/sources").json()["disclaimer"].lower()


def test_unclassifiable_event_lands_in_no_hazard_family():
    assert classify_hazard("Special Weather Statement") is None
    client = _client([
        FakeAdapter(
            "wmo-swic",
            alerts=[_alert("a:1", "wmo-swic", "ng-nimet", "Special Weather Statement")],
        ),
    ])
    body = client.get("/sources").json()
    assert all(
        "wmo-swic" not in sources for sources in body["hazard_coverage"].values()
    )
