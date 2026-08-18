import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import respx

from alertmux.adapters.swic import SwicAdapter

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "swic_effective.json").read_text()
)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_params_always_request_the_effective_view():
    params = SwicAdapter().build_params(mem=None, max_features=3000)
    assert params["typeName"] == "local_postgis:effective_warning_view"
    assert params["request"] == "GetFeature"
    assert params["outputFormat"] == "json"


def test_params_always_paginate():
    """Unfiltered SWIC queries exceed 24MB and time out."""
    params = SwicAdapter().build_params(mem=None, max_features=500)
    assert params["maxFeatures"] == 500


def test_params_filter_by_authority_when_mem_given():
    params = SwicAdapter().build_params(mem="075", max_features=3000)
    assert params["cql_filter"] == "mem='075'"


def test_params_omit_filter_when_no_mem():
    assert "cql_filter" not in SwicAdapter().build_params(mem=None, max_features=10)


def test_parse_returns_one_alert_per_feature():
    assert len(SwicAdapter().parse(FIXTURE, NOW)) == 3


def test_parse_derives_authority_from_capurl_prefix():
    alerts = SwicAdapter().parse(FIXTURE, NOW)
    assert alerts[0].provenance.authority == "ng-nimet"
    assert alerts[1].provenance.authority == "in-ndma"


def test_parse_keeps_capurl_as_raw_reference():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.raw_reference.startswith("ng-nimet-en/")


def test_parse_maps_event_and_area_verbatim():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.event == "THUNDERSTORMS"
    assert alert.area_description == "Some states in Nigeria will be affected."


def test_parse_reads_sent_as_utc():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.sent == datetime(2026, 8, 17, 6, 50, 16, tzinfo=timezone.utc)


def test_verified_codes_map_to_cap_names():
    """Confirmed against raw CAP: s=3 Severe, u=3 Expected, c=4 Observed."""
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity == "Severe"
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"


def test_raw_codes_are_always_preserved_even_when_mapped():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.source_severity == "3"
    assert alert.source_urgency == "3"
    assert alert.source_certainty == "4"


def test_extreme_code_maps():
    alert = SwicAdapter().parse(FIXTURE, NOW)[2]
    assert alert.source_severity == "4"
    assert alert.severity == "Extreme"
    assert alert.urgency == "Immediate"


def test_unverified_codes_stay_untranslated():
    """Codes 0, and 1 for urgency/certainty, were never observed.

    Guessing them would risk a missed warning or a false alarm.
    """
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "x", "geometry": None,
            "properties": {
                "capurl": "xx-test-en/2026/08/17/00/00/00-a.xml",
                "sent": "2026-08-17T00:00:00Z", "event": "TEST",
                "s": 0, "u": 1, "c": 1, "mem": "999",
                "areadesc": "nowhere", "rlink": "",
            },
        }],
    }
    alert = SwicAdapter().parse(payload, NOW)[0]
    assert alert.source_severity == "0"
    assert alert.source_urgency == "1"
    assert alert.source_certainty == "1"
    assert alert.severity is None
    assert alert.urgency is None
    assert alert.certainty is None
    assert "severity" in alert.unavailable_fields
    assert "urgency" in alert.unavailable_fields
    assert "certainty" in alert.unavailable_fields


def test_null_geometry_is_recorded_as_unavailable():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry is None
    assert "geometry" in alert.unavailable_fields


def test_id_is_stable_and_source_prefixed():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id.startswith("wmo-swic:")
    assert alert.id == SwicAdapter().parse(FIXTURE, NOW)[0].id


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = SwicAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "wmo-swic"
    assert len(result.alerts) == 3


@respx.mock
def test_fetch_passes_mem_filter_to_the_request():
    route = respx.route(url__startswith=SwicAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    SwicAdapter(mem="075").fetch()
    assert "mem%3D%27075%27" in str(route.calls[0].request.url) or (
        "mem='075'" in str(route.calls[0].request.url)
    )


@respx.mock
def test_fetch_reports_error_without_raising():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(500))
    result = SwicAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "500" in result.error


def test_malformed_feature_fails_loudly_rather_than_dropping_silently():
    bad = {"type": "FeatureCollection", "features": [{"type": "Feature"}]}
    try:
        SwicAdapter().parse(bad, NOW)
    except Exception as exc:
        assert "properties" in str(exc).lower() or "event" in str(exc).lower()
    else:
        raise AssertionError("malformed feature must not be silently dropped")
