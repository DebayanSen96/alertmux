import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.eonet import EonetAdapter
from alertmux.schema import NormalisedAlert

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "eonet_events.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_parse_returns_all_events():
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    assert len(alerts) == 5
    assert {a.event for a in alerts} == {"Wildfires", "Severe Storms"}


def test_headline_is_the_real_event_title():
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    fire = next(a for a in alerts if a.id == "nasa-eonet:EONET_22798")
    assert fire.headline == "Wildfire Picture Rock, Lake, Oregon"


def test_event_comes_from_first_category_title():
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    by_id = {a.id: a for a in alerts}
    assert by_id["nasa-eonet:EONET_22798"].event == "Wildfires"
    assert by_id["nasa-eonet:EONET_22562"].event == "Severe Storms"


def test_empty_categories_is_quarantined():
    """D3: an event with no categories cannot yield an `event` field. It
    is skipped and counted, not allowed to abort the whole fetch."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["events"][0]["categories"] = []
    alerts = EonetAdapter().parse(payload, NOW)
    assert alerts.invalid_count == 1
    assert any("no categories" in sample for sample in alerts.invalid_samples)
    assert len(alerts) == 4


def test_missing_categories_key_is_quarantined():
    payload = json.loads(json.dumps(FIXTURE))
    del payload["events"][0]["categories"]
    alerts = EonetAdapter().parse(payload, NOW)
    assert alerts.invalid_count == 1
    assert any("no categories" in sample for sample in alerts.invalid_samples)
    assert len(alerts) == 4


def test_severity_urgency_certainty_expires_onset_are_always_none():
    """EONET is an observation feed, not a warning feed - none of these
    concepts apply to any event, regardless of category or geometry
    count. This is the central discipline this adapter exists to test."""
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    assert len(alerts) > 0
    for alert in alerts:
        assert alert.severity is None
        assert alert.urgency is None
        assert alert.certainty is None
        assert alert.expires is None
        assert alert.onset is None
        for name in ("severity", "urgency", "certainty", "expires", "onset"):
            assert name in alert.unavailable_fields


def test_severity_is_never_derived_from_category_or_geometry_count():
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    tracked = next(a for a in alerts if a.id == "nasa-eonet:EONET_22562")
    # 11 accumulated geometries - a lot of tracking data, but still no
    # basis for inventing a severity judgement.
    assert tracked.severity is None


def test_id_is_derived_from_the_stable_eonet_id():
    alert = EonetAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == "nasa-eonet:EONET_22798"


def test_id_is_stable_across_two_independent_parses():
    """Verified against the live endpoint, 18 Aug 2026: fetching
    /events?limit=200 twice returned the same 200 ids, in the same
    order, both times. This mirrors D2's replacement test for SWIC -
    the id must be a pure function of the event's own `id` field."""
    first = EonetAdapter().parse(FIXTURE, NOW)
    second = EonetAdapter().parse(FIXTURE, NOW)
    assert [a.id for a in first] == [a.id for a in second]


def test_missing_event_id_is_quarantined():
    payload = json.loads(json.dumps(FIXTURE))
    del payload["events"][0]["id"]
    alerts = EonetAdapter().parse(payload, NOW)
    assert alerts.invalid_count == 1
    assert any("no id" in sample for sample in alerts.invalid_samples)
    assert len(alerts) == 4


def test_geometry_uses_the_single_geometry_when_only_one_exists():
    alert = EonetAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == "nasa-eonet:EONET_22798"
    assert alert.geometry == {
        "type": "Point",
        "coordinates": [-120.774487, 43.00919],
    }


def test_geometry_uses_only_the_most_recent_point_for_a_tracked_event():
    """A tracked event (Cyclone Hernan, 11 accumulated geometries in
    this fixture) carries only its latest position, not the whole
    track - see the module docstring for why. This test pins that
    choice: change it to "all geometries" and this must fail."""
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    storm = next(a for a in alerts if a.id == "nasa-eonet:EONET_22562")
    raw_event = next(e for e in FIXTURE["events"] if e["id"] == "EONET_22562")
    assert len(raw_event["geometries"]) == 11
    latest = sorted(raw_event["geometries"], key=lambda g: g["date"])[-1]
    assert storm.geometry == {
        "type": latest["type"],
        "coordinates": latest["coordinates"],
    }
    # Not the first (oldest) geometry.
    assert storm.geometry["coordinates"] != raw_event["geometries"][0]["coordinates"]


def test_sent_matches_the_same_geometry_that_was_chosen():
    """`sent` must describe exactly what `geometry` describes - the
    latest geometry's own date, not some other timestamp on the event."""
    alerts = EonetAdapter().parse(FIXTURE, NOW)
    storm = next(a for a in alerts if a.id == "nasa-eonet:EONET_22562")
    raw_event = next(e for e in FIXTURE["events"] if e["id"] == "EONET_22562")
    latest = sorted(raw_event["geometries"], key=lambda g: g["date"])[-1]
    expected = datetime.fromisoformat(latest["date"].replace("Z", "+00:00"))
    assert storm.sent == expected


def test_naive_geometry_date_is_quarantined_rather_than_assuming_utc():
    payload = json.loads(json.dumps(FIXTURE))
    payload["events"][0]["geometries"][0]["date"] = "2026-08-16T15:55:00"
    alerts = EonetAdapter().parse(payload, NOW)
    assert alerts.invalid_count == 1
    assert any("no UTC offset" in sample for sample in alerts.invalid_samples)
    assert len(alerts) == 4


def test_one_malformed_event_among_valid_ones_is_quarantined_not_fatal():
    payload = json.loads(json.dumps(FIXTURE))
    payload["events"][0]["categories"] = []
    alerts = EonetAdapter().parse(payload, NOW)
    assert len(alerts) == 4
    assert alerts.invalid_count == 1


def test_all_events_malformed_yields_empty_list_and_full_invalid_count():
    payload = json.loads(json.dumps(FIXTURE))
    for event in payload["events"]:
        event["categories"] = []
    alerts = EonetAdapter().parse(payload, NOW)
    assert alerts == []
    assert alerts.invalid_count == len(payload["events"])


@respx.mock
def test_fetch_quarantines_one_bad_record_and_still_returns_the_rest():
    payload = json.loads(json.dumps(FIXTURE))
    payload["events"][0]["categories"] = []
    respx.get(EonetAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = EonetAdapter().fetch()
    assert result.ok is True
    assert len(result.alerts) == 4
    assert result.invalid_count == 1
    assert len(result.invalid_samples) == 1


def test_missing_events_list_raises_rather_than_reporting_zero_alerts():
    """Quarantine must not weaken the envelope check."""
    with pytest.raises(ValueError, match="no events list"):
        EonetAdapter().parse({"title": "x"}, NOW)


def test_events_not_a_list_raises():
    with pytest.raises(ValueError, match="no events list"):
        EonetAdapter().parse({"events": "nope"}, NOW)


def test_unavailable_fields_is_exhaustive_in_both_directions():
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields", "unmapped_fields"}
    ]
    for alert in EonetAdapter().parse(FIXTURE, NOW):
        for name in alert.unavailable_fields:
            assert getattr(alert, name) is None, name
        for name in alert.unmapped_fields:
            assert getattr(alert, name) is None, name
        for name in optional:
            if getattr(alert, name) is None:
                assert (
                    name in alert.unavailable_fields or name in alert.unmapped_fields
                ), name


def test_eonet_never_supplies_unmapped_fields():
    """EONET has no severity/urgency/certainty concepts at all -- those
    gaps are structural, never a declined mapping, so unmapped_fields
    must always be empty here."""
    for alert in EonetAdapter().parse(FIXTURE, NOW):
        assert alert.unmapped_fields == []


def test_unavailable_and_unmapped_never_overlap():
    for alert in EonetAdapter().parse(FIXTURE, NOW):
        overlap = set(alert.unavailable_fields) & set(alert.unmapped_fields)
        assert overlap == set(), overlap


def test_provenance_authority_is_nasa_eonet():
    alert = EonetAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.authority == "nasa-eonet"
    assert alert.provenance.source_id == "nasa-eonet"
    assert alert.provenance.raw_reference == (
        "https://eonet.gsfc.nasa.gov/api/v2.1/events/EONET_22798"
    )


def test_empty_events_list_is_zero_alerts_not_an_error():
    assert EonetAdapter().parse({"events": []}, NOW) == []


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(EonetAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    result = EonetAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "nasa-eonet"
    assert len(result.alerts) == 5
    assert result.error is None


@respx.mock
def test_fetch_reports_http_error_without_raising():
    respx.get(EonetAdapter.URL).mock(return_value=httpx.Response(503))
    result = EonetAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "503" in result.error


@respx.mock
def test_fetch_reports_timeout_without_raising():
    respx.get(EonetAdapter.URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = EonetAdapter().fetch()
    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


@respx.mock
def test_fetch_reports_malformed_envelope_as_a_failure():
    respx.get(EonetAdapter.URL).mock(
        return_value=httpx.Response(200, json={"not": "events"})
    )
    result = EonetAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []


@respx.mock
def test_fetch_sets_user_agent_header():
    route = respx.get(EonetAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    EonetAdapter().fetch()
    assert route.called
    request = route.calls[0].request
    assert "alertmux" in request.headers["User-Agent"]


@respx.mock
def test_fetch_always_sends_limit_param():
    route = respx.get(EonetAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    EonetAdapter().fetch()
    request = route.calls[0].request
    assert request.url.params["limit"] == "200"


@respx.mock
def test_fetch_sends_custom_limit_when_given():
    route = respx.get(EonetAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    EonetAdapter(limit=50).fetch()
    request = route.calls[0].request
    assert request.url.params["limit"] == "50"
