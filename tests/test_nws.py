import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.nws import NwsAdapter
from alertmux.schema import NormalisedAlert

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "nws_active.json").read_text()
)
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_fetch_uses_the_unparameterised_url():
    """?limit=N returns HTTP 400 on this endpoint - never add params."""

    @respx.mock
    def run():
        route = respx.get("https://api.weather.gov/alerts/active").mock(
            return_value=httpx.Response(200, json=FIXTURE)
        )
        NwsAdapter().fetch()
        assert route.called
        request = route.calls[0].request
        assert request.url.params == httpx.QueryParams()

    run()


def test_parse_filters_out_test_status_and_keeps_actual_only():
    """The KEEPALIVE record (status: Test, event: Test Message) must
    never be relayed as a real hazard warning. This is the central
    promise the project exists to keep."""
    alerts = NwsAdapter().parse(FIXTURE, NOW)
    assert len(alerts) == 4
    assert all(alert.event != "Test Message" for alert in alerts)
    assert not any(
        "KEEPALIVE" in alert.id for alert in alerts
    )
    assert all(
        f["properties"]["status"] == "Actual"
        for f in FIXTURE["features"]
        if any(f["properties"]["id"] in alert.id for alert in alerts)
    )


def test_parse_excludes_cancel_message_type():
    feature = dict(FIXTURE["features"][0])
    feature["properties"] = dict(feature["properties"])
    feature["properties"]["messageType"] = "Cancel"
    collection = {"type": "FeatureCollection", "features": [feature]}
    assert NwsAdapter().parse(collection, NOW) == []


def test_parse_maps_identity_and_event():
    alert = NwsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == (
        "us-noaa:urn:oid:2.49.0.1.840.0.caf99f41e8e25784bfbc48eedb293d4791144aa6.001.1"
    )
    assert alert.event == "Flood Advisory"
    assert alert.headline.startswith("Flood Advisory issued August 18")


def test_parse_uses_named_cap_values_directly_no_code_table():
    alert = NwsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity == "Minor"
    assert alert.urgency == "Expected"
    assert alert.certainty == "Likely"
    assert alert.source_severity == "Minor"
    assert alert.source_urgency == "Expected"
    assert alert.source_certainty == "Likely"


def test_unknown_is_a_real_cap_value_not_a_missing_field():
    alerts = NwsAdapter().parse(FIXTURE, NOW)
    air_quality = next(a for a in alerts if a.event == "Air Quality Alert")
    assert air_quality.severity == "Unknown"
    assert air_quality.source_severity == "Unknown"
    assert "severity" not in air_quality.unavailable_fields


def test_parse_maps_expires_and_onset_with_utc_offset():
    alert = NwsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.onset == datetime.fromisoformat(
        "2026-08-18T05:19:00-05:00"
    ).astimezone(timezone.utc)
    assert alert.expires == datetime.fromisoformat(
        "2026-08-18T08:15:00-05:00"
    ).astimezone(timezone.utc)
    assert alert.onset is not None
    assert alert.expires is not None


def test_naive_timestamp_raises_rather_than_assuming_utc():
    feature = dict(FIXTURE["features"][0])
    feature["properties"] = dict(feature["properties"])
    feature["properties"]["expires"] = "2026-08-18T08:15:00"  # no offset
    collection = {"type": "FeatureCollection", "features": [feature]}
    with pytest.raises(ValueError, match="no UTC offset"):
        NwsAdapter().parse(collection, NOW)


def test_geometry_present_alert_keeps_polygon():
    alert = NwsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry["type"] == "Polygon"


def test_null_geometry_means_zone_referenced_not_unknown():
    """Only ~48 of 295 live features carry geometry; the rest are
    zone-referenced via affectedZones/geocode. A null geometry here
    still goes in unavailable_fields, but it does not mean the source
    failed to report a location."""
    alerts = NwsAdapter().parse(FIXTURE, NOW)
    small_craft = next(a for a in alerts if a.event == "Small Craft Advisory")
    assert small_craft.geometry is None
    assert "geometry" in small_craft.unavailable_fields


def test_missing_id_raises():
    feature = dict(FIXTURE["features"][0])
    feature["properties"] = dict(feature["properties"])
    del feature["properties"]["id"]
    collection = {"type": "FeatureCollection", "features": [feature]}
    with pytest.raises(ValueError, match="no properties.id"):
        NwsAdapter().parse(collection, NOW)


def test_missing_event_raises():
    feature = dict(FIXTURE["features"][0])
    feature["properties"] = dict(feature["properties"])
    feature["properties"]["event"] = ""
    collection = {"type": "FeatureCollection", "features": [feature]}
    with pytest.raises(ValueError, match="no event"):
        NwsAdapter().parse(collection, NOW)


def test_non_featurecollection_200_raises_rather_than_reporting_zero_alerts():
    with pytest.raises(ValueError, match="FeatureCollection"):
        NwsAdapter().parse({"correlationId": "x", "detail": "..."}, NOW)


def test_unavailable_fields_is_exhaustive_in_both_directions():
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields"}
    ]
    for alert in NwsAdapter().parse(FIXTURE, NOW):
        for name in alert.unavailable_fields:
            assert getattr(alert, name) is None, name
        for name in optional:
            if getattr(alert, name) is None:
                assert name in alert.unavailable_fields, name


def test_empty_feature_list_is_zero_alerts_not_an_error():
    assert NwsAdapter().parse({"type": "FeatureCollection", "features": []}, NOW) == []


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(NwsAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = NwsAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "us-noaa"
    assert len(result.alerts) == 4
    assert result.error is None


@respx.mock
def test_fetch_reports_http_error_without_raising():
    respx.get(NwsAdapter.URL).mock(return_value=httpx.Response(503))
    result = NwsAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "503" in result.error


@respx.mock
def test_fetch_reports_timeout_without_raising():
    respx.get(NwsAdapter.URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = NwsAdapter().fetch()
    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


@respx.mock
def test_fetch_reports_non_geojson_200_as_a_failure():
    respx.get(NwsAdapter.URL).mock(
        return_value=httpx.Response(200, json={"detail": "not a collection"})
    )
    result = NwsAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []


def test_provenance_authority_is_us_noaa():
    alert = NwsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.authority == "us-noaa"
    assert alert.provenance.source_id == "us-noaa"
    assert alert.provenance.raw_reference
