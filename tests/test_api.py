from datetime import datetime, timezone

from fastapi.testclient import TestClient

from alertmux.adapters.base import FetchResult
from alertmux.api import app, get_adapters
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _alert():
    return NormalisedAlert(
        id="wmo-swic:1",
        event="THUNDERSTORMS",
        area_description="Some states in Nigeria will be affected.",
        source_severity="3",
        provenance=Provenance(
            authority="ng-nimet",
            source_id="wmo-swic",
            source_url="https://severeweather.wmo.int/g/wfs",
            retrieved_at=NOW,
        ),
        unavailable_fields=["severity"],
    )


class FakeAdapter:
    def __init__(self, source_id, ok=True, alerts=None, error=None):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id, ok=ok, alerts=alerts or [],
            error=error, retrieved_at=NOW, latency_ms=5,
        )

    def fetch(self):
        return self._result


def _client(adapters):
    app.dependency_overrides[get_adapters] = lambda: adapters
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_alerts_returns_normalised_alerts():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    body = client.get("/alerts").json()
    assert body["alerts"][0]["event"] == "THUNDERSTORMS"
    assert body["partial"] is False


def test_alerts_always_includes_provenance():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    provenance = client.get("/alerts").json()["alerts"][0]["provenance"]
    assert provenance["authority"] == "ng-nimet"
    assert provenance["source_url"].startswith("https://")
    assert provenance["retrieved_at"]


def test_alerts_never_reports_a_severity_it_was_not_given():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    alert = client.get("/alerts").json()["alerts"][0]
    assert alert["severity"] is None
    assert alert["source_severity"] == "3"


def test_alerts_flags_partial_when_a_source_fails():
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="503"),
    ])
    body = client.get("/alerts").json()
    assert body["partial"] is True
    assert len(body["alerts"]) == 1


def test_alerts_filter_by_authority():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert len(client.get("/alerts?authority=ng-nimet").json()["alerts"]) == 1
    assert len(client.get("/alerts?authority=us-noaa").json()["alerts"]) == 0


def test_health_reports_every_source():
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    body = client.get("/health").json()
    assert body["ok"] is False
    by_id = {s["source_id"]: s for s in body["sources"]}
    assert by_id["wmo-swic"]["ok"] is True
    assert by_id["usgs"]["error"] == "timeout"


def test_health_ok_when_all_sources_healthy():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/health").json()["ok"] is True


def test_health_does_not_return_alert_bodies():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert "alerts" not in client.get("/health").json()
