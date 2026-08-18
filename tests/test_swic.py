import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.swic import SwicAdapter
from alertmux.schema import NormalisedAlert

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "swic_effective.json").read_text()
)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_params_always_request_the_effective_view():
    params = SwicAdapter().build_params(mem=None, max_features=3000)
    assert params["typeName"] == "local_postgis:effective_warning_view"
    assert params["request"] == "GetFeature"
    assert params["outputFormat"] == "json"


def test_params_always_send_maxfeatures():
    """Unfiltered SWIC queries exceed 24MB and time out.

    This only checks that maxFeatures is always present in the request -
    it does not, and cannot, prove the whole result set was retrieved.
    Truncation is caught by test_truncated_response_is_flagged below.
    """
    assert "maxFeatures" in SwicAdapter().build_params(mem=None, max_features=500)
    assert SwicAdapter().build_params(mem=None, max_features=500)["maxFeatures"] == 500


def test_params_always_send_sort_by_for_deterministic_paging():
    """Verified live, 18 Aug 2026: this GeoServer answers ANY request
    carrying startIndex with a bare "Err" body (HTTP 200, not a WFS
    exception report) unless sortBy is also present -- WFS 1.1.0 does
    not mandate a feature order, so startIndex is only well-defined
    paired with a sort key. Without this, real pagination breaks
    completely despite every mocked unit test passing."""
    params = SwicAdapter().build_params(mem=None, max_features=500)
    assert params["sortBy"] == "capurl"


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


def _feature(fid: str, capurl: str, **prop_overrides) -> dict:
    props = {
        "capurl": capurl,
        "sent": "2026-08-17T00:00:00Z",
        "event": "TEST",
        "s": 3, "u": 3, "c": 4, "mem": "999",
        "areadesc": "nowhere", "rlink": "",
    }
    props.update(prop_overrides)
    if capurl is None:
        props.pop("capurl")
    return {"type": "Feature", "id": fid, "geometry": None, "properties": props}


def _collection(*features) -> dict:
    return {
        "type": "FeatureCollection",
        "numberMatched": len(features),
        "numberReturned": len(features),
        "features": list(features),
    }


def test_id_is_derived_from_capurl_not_the_geoserver_fid():
    """GeoServer synthetic fids embed a REQUEST timestamp and change on
    every fetch. The same alert must keep the same id, or the notifier's
    dedupe key breaks and every poll re-notifies.
    """
    capurl = "ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml"
    first = SwicAdapter().parse(
        _collection(_feature("effective_warning_view.fid--7463f54d_x_2d5b", capurl)), NOW
    )[0]
    second = SwicAdapter().parse(
        _collection(_feature("effective_warning_view.fid--7463f54d_x_2d5c", capurl)), NOW
    )[0]
    assert first.id == second.id
    assert first.id == f"wmo-swic:{capurl}"


def test_feature_without_capurl_is_quarantined_rather_than_using_the_fid():
    """D3: a malformed record is skipped and counted, not allowed to
    abort the whole fetch. capurl is never invented from the fid."""
    payload = _collection(_feature("effective_warning_view.fid--abc", None))
    alerts = SwicAdapter().parse(payload, NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("capurl" in sample for sample in alerts.invalid_samples)


def test_unparseable_capurl_is_quarantined_rather_than_guessing_authority():
    payload = _collection(_feature("f1", "NOT-A-CAP-PATH/2026/08/17/x.xml"))
    alerts = SwicAdapter().parse(payload, NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("authority" in sample for sample in alerts.invalid_samples)


def test_naive_sent_timestamp_raises_rather_than_assuming_local_time():
    """astimezone() on a naive value silently applies the SERVER's offset,
    shifting a hazard timestamp differently on every machine."""
    from alertmux.adapters.swic import _iso_utc

    with pytest.raises(ValueError, match="offset"):
        _iso_utc("2026-08-17T06:50:16", "sent")
    assert _iso_utc("2026-08-17T06:50:16Z", "sent") == datetime(
        2026, 8, 17, 6, 50, 16, tzinfo=timezone.utc
    )


def test_headline_and_description_are_never_fabricated():
    """The WFS list view carries neither. `rlink` points at a RELATED CAP
    file, so surfacing it as a description would relay a filename."""
    alerts = SwicAdapter().parse(FIXTURE, NOW)
    for alert in alerts:
        assert alert.headline is None
        assert alert.description is None
        assert "headline" in alert.unavailable_fields
        assert "description" in alert.unavailable_fields

    with_rlink = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", rlink="xx-test-en/other.xml")),
        NOW,
    )[0]
    assert with_rlink.description is None


def test_unavailable_fields_is_exhaustive_in_both_directions():
    payloads = [FIXTURE, _collection(_feature("f1", "xx-test-en/a.xml", areadesc=None))]
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields"}
    ]
    for payload in payloads:
        for alert in SwicAdapter().parse(payload, NOW):
            for name in alert.unavailable_fields:
                assert getattr(alert, name) is None, name
            for name in optional:
                if getattr(alert, name) is None:
                    assert name in alert.unavailable_fields, name


def test_empty_feature_list_is_zero_alerts_not_an_error():
    """A legitimately quiet feed must stay distinguishable from the
    non-GeoJSON-200 case below."""
    assert SwicAdapter().parse(_collection(), NOW) == []


def test_non_featurecollection_200_raises_rather_than_reporting_zero_hazards():
    exception_report = {"exceptions": [{"exceptionCode": "InvalidParameterValue"}]}
    with pytest.raises(ValueError, match="FeatureCollection"):
        SwicAdapter().parse(exception_report, NOW)


@respx.mock
def test_fetch_reports_ows_exception_at_200_as_a_failure():
    """GeoServer answers a bad cql_filter with HTTP 200 and no features.
    Parsing that as 0 alerts would report 'no hazards worldwide, healthy'.
    """
    respx.get(SwicAdapter.URL).mock(
        return_value=httpx.Response(
            200, json={"exceptions": [{"exceptionCode": "InvalidParameterValue"}]}
        )
    )
    result = SwicAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []


@respx.mock
def test_truncated_response_is_flagged_while_staying_ok():
    payload = dict(FIXTURE, numberMatched=2133, numberReturned=3)
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = SwicAdapter(max_features=3).fetch()
    assert result.ok is True
    assert result.truncated is True
    assert result.matched == 2133
    assert result.returned == 3


@respx.mock
def test_complete_response_is_not_flagged_as_truncated():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    assert SwicAdapter().fetch().truncated is False


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


def test_feature_without_properties_is_quarantined():
    bad = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "f1"}]}
    alerts = SwicAdapter().parse(bad, NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("no properties" in sample for sample in alerts.invalid_samples)


def test_feature_without_event_is_quarantined():
    bad = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "f1", "geometry": None,
            "properties": {"capurl": "xx-test-en/a.xml", "sent": "2026-08-17T00:00:00Z"},
        }],
    }
    alerts = SwicAdapter().parse(bad, NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("no event" in sample for sample in alerts.invalid_samples)


def test_one_malformed_feature_among_valid_ones_is_quarantined_not_fatal():
    """D3: one bad record must never discard the good ones in the same
    fetch. The valid features are returned; the bad one is counted and
    sampled."""
    good1 = _feature("f1", "xx-test-en/a.xml")
    bad = _feature("f2", None)
    good2 = _feature("f3", "xx-test-en/b.xml")
    alerts = SwicAdapter().parse(_collection(good1, bad, good2), NOW)
    assert len(alerts) == 2
    assert alerts.invalid_count == 1
    assert any("capurl" in sample for sample in alerts.invalid_samples)


def test_all_features_malformed_yields_empty_list_and_full_invalid_count():
    bad1 = _feature("f1", None)
    bad2 = _feature("f2", None)
    alerts = SwicAdapter().parse(_collection(bad1, bad2), NOW)
    assert alerts == []
    assert alerts.invalid_count == 2


@respx.mock
def test_fetch_quarantines_one_bad_record_and_still_returns_the_rest():
    good1 = _feature("f1", "xx-test-en/a.xml")
    bad = _feature("f2", None)
    good2 = _feature("f3", "xx-test-en/b.xml")
    payload = _collection(good1, bad, good2)
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = SwicAdapter().fetch()
    assert result.ok is True
    assert len(result.alerts) == 2
    assert result.invalid_count == 1
    assert len(result.invalid_samples) == 1


def test_malformed_envelope_still_fails_the_whole_fetch():
    """Quarantine must not weaken the envelope check: a response that is
    not a valid GeoJSON FeatureCollection at all still fails loudly."""
    with pytest.raises(ValueError, match="FeatureCollection"):
        SwicAdapter().parse({"exceptions": [{"exceptionCode": "X"}]}, NOW)


def test_severity_codes_as_digit_strings_still_map():
    """Some SWIC authorities emit s/u/c as "3" instead of 3. A string key
    against the int table used to miss silently and leave the named field
    null. Digit-string codes must map to the same CAP names as their int
    twins, with the raw string preserved in source_severity/urgency/certainty.
    """
    alert = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", s="3", u="3", c="4")),
        NOW,
    )[0]
    assert alert.severity == "Severe"
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"
    assert alert.source_severity == "3"
    assert alert.source_urgency == "3"
    assert alert.source_certainty == "4"
    assert "severity" not in alert.unavailable_fields
    assert "urgency" not in alert.unavailable_fields
    assert "certainty" not in alert.unavailable_fields


def test_non_digit_string_code_stays_unmapped():
    """A string that is not a digit (e.g. free-text severity) must not be
    forced through the table; it stays unmapped and is recorded as
    unavailable, exactly as an unverified int code would. Int codes on
    the same feature are unaffected and still map.
    """
    alert = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", s="high", u=3, c=4)),
        NOW,
    )[0]
    assert alert.severity is None
    assert alert.source_severity == "high"
    assert "severity" in alert.unavailable_fields
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"


@respx.mock
def test_full_page_is_truncated_even_when_matched_is_unknown():
    """GeoServer WFS 1.1.0 can answer "unknown" for numberMatched. The
    matched > returned comparison cannot fire on a string, so without the
    maxFeatures rule a capped page would be reported as complete."""
    payload = dict(FIXTURE, numberMatched="unknown", numberReturned=3)
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = SwicAdapter(max_features=3).fetch()
    assert result.ok is True
    assert result.truncated is True
    assert result.matched is None
    assert result.returned == 3


@respx.mock
def test_short_page_with_unknown_matched_is_not_truncated():
    """Control for the test above: the same unusable numberMatched must not
    flag truncation when the page came back under the cap."""
    payload = dict(FIXTURE, numberMatched="unknown", numberReturned=3)
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = SwicAdapter(max_features=3000).fetch()
    assert result.ok is True
    assert result.truncated is False
    assert result.matched is None


def _page(features: list[dict], matched, returned) -> dict:
    return {
        "type": "FeatureCollection",
        "numberMatched": matched,
        "numberReturned": returned,
        "features": features,
    }


@respx.mock
def test_two_page_fetch_assembles_all_records():
    """A first page filled to maxFeatures must trigger a second request,
    and the records from both pages must all end up in the result."""
    page1 = _page(
        [_feature("f1", "xx-test-en/a.xml"), _feature("f2", "xx-test-en/b.xml")],
        matched=3, returned=2,
    )
    page2 = _page([_feature("f3", "xx-test-en/c.xml")], matched=3, returned=1)
    route = respx.get(SwicAdapter.URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    result = SwicAdapter(max_features=2).fetch()
    assert result.ok is True
    assert {a.provenance.raw_reference for a in result.alerts} == {
        "xx-test-en/a.xml", "xx-test-en/b.xml", "xx-test-en/c.xml",
    }
    assert len(route.calls) == 2


@respx.mock
def test_third_page_not_requested_when_second_is_short():
    """The second page came back short of maxFeatures with numberMatched
    satisfied -- that is exhaustion, and a third request must never fire."""
    page1 = _page(
        [_feature("f1", "xx-test-en/a.xml"), _feature("f2", "xx-test-en/b.xml")],
        matched=3, returned=2,
    )
    page2 = _page([_feature("f3", "xx-test-en/c.xml")], matched=3, returned=1)
    route = respx.get(SwicAdapter.URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    result = SwicAdapter(max_features=2).fetch()
    assert len(route.calls) == 2
    assert result.truncated is False


@respx.mock
def test_max_pages_ceiling_leaves_truncated_true():
    """An endlessly-full feed (or one whose startIndex is not being
    honoured) must never be polled forever. Hitting the ceiling before
    exhaustion keeps truncated True as the "we stopped early" signal."""
    counter = {"n": 0}

    def _respond(request):
        counter["n"] += 1
        n = counter["n"]
        feature = _feature(f"f{n}", f"xx-test-en/{n}.xml")
        return httpx.Response(200, json=_page([feature], matched="unknown", returned=1))

    route = respx.get(SwicAdapter.URL).mock(side_effect=_respond)
    result = SwicAdapter(max_features=1, max_pages=3).fetch()
    assert result.ok is True
    assert result.truncated is True
    assert len(route.calls) == 3
    assert len(result.alerts) == 3


@respx.mock
def test_unknown_number_matched_still_paginates_via_returned_rule():
    """Belt-and-braces (D4): numberMatched "unknown" must not stop the
    adapter trusting a short page falsely -- returned >= max_features
    alone must keep requesting the next page."""
    page1 = _page(
        [_feature("f1", "xx-test-en/a.xml"), _feature("f2", "xx-test-en/b.xml")],
        matched="unknown", returned=2,
    )
    page2 = _page([_feature("f3", "xx-test-en/c.xml")], matched="unknown", returned=1)
    route = respx.get(SwicAdapter.URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    result = SwicAdapter(max_features=2).fetch()
    assert len(route.calls) == 2
    assert len(result.alerts) == 3
    assert result.matched is None
    assert result.truncated is False


@respx.mock
def test_duplicate_ids_across_pages_are_collapsed_and_counted():
    """If the server's ordering is unstable, the same capurl can appear
    on two pages. It must not be counted twice, and the collapse must
    be visible in duplicate_count rather than silent."""
    page1 = _page(
        [_feature("f1", "xx-test-en/a.xml"), _feature("f2", "xx-test-en/b.xml")],
        matched=3, returned=2,
    )
    # f2 repeats (same capurl -> same id) alongside one genuinely new record.
    page2 = _page(
        [_feature("f2b", "xx-test-en/b.xml"), _feature("f3", "xx-test-en/c.xml")],
        matched=3, returned=1,
    )
    respx.get(SwicAdapter.URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    result = SwicAdapter(max_features=2).fetch()
    assert {a.provenance.raw_reference for a in result.alerts} == {
        "xx-test-en/a.xml", "xx-test-en/b.xml", "xx-test-en/c.xml",
    }
    assert len(result.alerts) == 3
    assert result.duplicate_count == 1


@respx.mock
def test_quarantine_still_works_on_second_page():
    """D3's per-record quarantine must survive across pages: a malformed
    record on page two is skipped and counted, not fatal to the fetch."""
    page1 = _page(
        [_feature("f1", "xx-test-en/a.xml"), _feature("f2", "xx-test-en/b.xml")],
        matched=3, returned=2,
    )
    bad = _feature("f3", None)
    page2 = _page([bad], matched=3, returned=1)
    respx.get(SwicAdapter.URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    result = SwicAdapter(max_features=2).fetch()
    assert result.ok is True
    assert len(result.alerts) == 2
    assert result.invalid_count == 1
    assert any("capurl" in sample for sample in result.invalid_samples)


def test_max_pages_ceiling_is_a_reasonable_default():
    assert SwicAdapter()._max_pages == 20


def test_one_page_still_works_no_second_request():
    """Constraint 5: if the first page returns everything, a second
    request must never be issued."""
    import respx as _respx

    with _respx.mock:
        route = _respx.get(SwicAdapter.URL).mock(
            return_value=httpx.Response(200, json=FIXTURE)
        )
        result = SwicAdapter().fetch()
    assert len(route.calls) == 1
    assert len(result.alerts) == 3
    assert result.truncated is False


# ---------------------------------------------------------------------------
# CAP detail fetch (issue #4)
# ---------------------------------------------------------------------------

CAP_DETAIL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "swic_cap_detail.xml"
).read_bytes()


@pytest.fixture(autouse=True)
def _clear_cap_cache():
    from alertmux.adapters.swic import clear_cap_cache

    clear_cap_cache()
    yield
    clear_cap_cache()


def test_parse_cap_detail_reads_namespaced_tags():
    from alertmux.adapters.swic import parse_cap_detail

    detail = parse_cap_detail(CAP_DETAIL_FIXTURE)
    assert detail.headline.startswith("Light to Moderate Rain")
    assert detail.instruction == "Please follow SDMA guidelines."
    assert detail.severity == "Moderate"
    assert detail.urgency == "Expected"
    assert detail.certainty == "Likely"


def test_parse_cap_detail_reads_onset_and_expires_with_offsets():
    from alertmux.adapters.swic import parse_cap_detail

    detail = parse_cap_detail(CAP_DETAIL_FIXTURE)
    # Source carries +05:30; both fields must come back converted to UTC.
    assert detail.onset == datetime(2026, 8, 18, 16, 22, 31, tzinfo=timezone.utc)
    assert detail.expires == datetime(2026, 8, 18, 19, 30, tzinfo=timezone.utc)


def test_parse_cap_detail_empty_description_is_none_not_empty_string():
    from alertmux.adapters.swic import parse_cap_detail

    detail = parse_cap_detail(CAP_DETAIL_FIXTURE)
    assert detail.description is None


def test_parse_cap_detail_raises_on_non_cap_xml():
    from alertmux.adapters.swic import parse_cap_detail

    with pytest.raises(ValueError, match="CAP 1.2"):
        parse_cap_detail(b"<html><body>not cap</body></html>")


def test_parse_cap_detail_raises_when_no_info_block():
    from alertmux.adapters.swic import parse_cap_detail

    xml = (
        '<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">'
        "<cap:identifier>x</cap:identifier></cap:alert>"
    )
    with pytest.raises(ValueError, match="info"):
        parse_cap_detail(xml)


def test_parse_cap_detail_prefers_english_info_block():
    from alertmux.adapters.swic import parse_cap_detail

    xml = """<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
      <cap:info>
        <cap:language>zh-CN</cap:language>
        <cap:headline>中文标题</cap:headline>
      </cap:info>
      <cap:info>
        <cap:language>en-US</cap:language>
        <cap:headline>English headline</cap:headline>
      </cap:info>
    </cap:alert>"""
    detail = parse_cap_detail(xml)
    assert detail.headline == "English headline"
    assert detail.language == "en-US"


@respx.mock
def test_enriched_alert_shrinks_unavailable_fields():
    from alertmux.adapters.swic import enrich_with_detail, parse_cap_detail

    detail = parse_cap_detail(CAP_DETAIL_FIXTURE)
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert "headline" in alert.unavailable_fields
    assert "instruction" in alert.unavailable_fields

    enriched = enrich_with_detail(alert, detail)
    assert enriched.headline == detail.headline
    assert enriched.instruction == "Please follow SDMA guidelines."
    assert enriched.expires == detail.expires
    assert "headline" not in enriched.unavailable_fields
    assert "instruction" not in enriched.unavailable_fields
    assert "expires" not in enriched.unavailable_fields
    # description stayed absent on both sides -- still unavailable.
    assert "description" in enriched.unavailable_fields


def test_enrich_with_detail_never_mutates_the_original_alert():
    from alertmux.adapters.swic import enrich_with_detail, parse_cap_detail

    detail = parse_cap_detail(CAP_DETAIL_FIXTURE)
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    enrich_with_detail(alert, detail)
    assert alert.headline is None
    assert "headline" in alert.unavailable_fields


def test_named_cap_severity_wins_over_list_view_integer_code():
    """The CAP file is the authority's own record; it outranks the list
    view's integer-code mapping when both are present. The raw integer
    stays put in source_severity either way."""
    from alertmux.adapters.swic import CapDetail, enrich_with_detail

    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity == "Severe"  # from s=3
    assert alert.source_severity == "3"

    detail = CapDetail(severity="Extreme", urgency="Immediate", certainty="Observed")
    enriched = enrich_with_detail(alert, detail)
    assert enriched.severity == "Extreme"
    assert enriched.source_severity == "3"


@respx.mock
def test_fetch_detail_parses_and_returns_cap_detail():
    capurl = "in-ndma-xx/2026/08/18/16/51/58-detail.xml"
    respx.get(f"https://severeweather.wmo.int/v2/cap-alerts/{capurl}").mock(
        return_value=httpx.Response(200, content=CAP_DETAIL_FIXTURE)
    )
    detail = SwicAdapter().fetch_detail(capurl)
    assert detail.severity == "Moderate"
    assert detail.expires == datetime(2026, 8, 18, 19, 30, tzinfo=timezone.utc)


@respx.mock
def test_fetch_detail_caches_and_never_fetches_twice():
    capurl = "in-ndma-xx/2026/08/18/16/51/58-detail.xml"
    route = respx.get(f"https://severeweather.wmo.int/v2/cap-alerts/{capurl}").mock(
        return_value=httpx.Response(200, content=CAP_DETAIL_FIXTURE)
    )
    adapter = SwicAdapter()
    first = adapter.fetch_detail(capurl)
    second = adapter.fetch_detail(capurl)
    assert first == second
    assert route.call_count == 1


@respx.mock
def test_fetch_detail_missing_cap_file_raises_clear_error():
    from alertmux.adapters.swic import CapDetailError

    capurl = "xx-nowhere/does-not-exist.xml"
    respx.get(f"https://severeweather.wmo.int/v2/cap-alerts/{capurl}").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(CapDetailError, match="404|Error"):
        SwicAdapter().fetch_detail(capurl)


@respx.mock
def test_fetch_detail_malformed_xml_raises_clear_error():
    from alertmux.adapters.swic import CapDetailError

    capurl = "xx-broken/broken.xml"
    respx.get(f"https://severeweather.wmo.int/v2/cap-alerts/{capurl}").mock(
        return_value=httpx.Response(200, content=b"not xml at all")
    )
    with pytest.raises(CapDetailError):
        SwicAdapter().fetch_detail(capurl)
