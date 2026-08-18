import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.usgs import UsgsAdapter

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "usgs_all_hour.json").read_text()
)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_parse_returns_one_alert_per_feature():
    alerts = UsgsAdapter().parse(FIXTURE, NOW)
    assert len(alerts) == 2


def test_parse_maps_identity_and_event():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == "usgs:ok2026qcji"
    assert alert.event == "earthquake"
    assert alert.headline == "M 2.3 - 6 km SE of Chickasha, Oklahoma"
    assert alert.area_description == "6 km SE of Chickasha, Oklahoma"


def test_parse_converts_epoch_millis_to_utc_datetime():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.sent == datetime.fromtimestamp(1787009589944 / 1000, tz=timezone.utc)


def test_parse_preserves_geometry():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry["type"] == "Point"
    assert alert.geometry["coordinates"][0] == pytest.approx(-97.88217163)


def test_parse_sets_provenance():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.authority == "us-usgs"
    assert alert.provenance.source_id == "usgs"
    assert alert.provenance.retrieved_at == NOW
    assert alert.provenance.raw_reference.endswith("ok2026qcji")


def test_null_alert_level_is_recorded_as_unavailable_not_guessed():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity is None
    assert alert.source_severity is None
    assert "severity" in alert.unavailable_fields


def test_present_alert_level_is_kept_as_source_severity_only():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[1]
    assert alert.source_severity == "green"
    assert alert.severity is None


def test_usgs_never_supplies_expiry():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.expires is None
    assert "expires" in alert.unavailable_fields


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(UsgsAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = UsgsAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "usgs"
    assert len(result.alerts) == 2
    assert result.error is None


@respx.mock
def test_fetch_reports_http_error_without_raising():
    respx.get(UsgsAdapter.URL).mock(return_value=httpx.Response(503))
    result = UsgsAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "503" in result.error


@respx.mock
def test_fetch_reports_timeout_without_raising():
    respx.get(UsgsAdapter.URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = UsgsAdapter().fetch()
    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()
