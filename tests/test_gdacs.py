from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.gdacs import GdacsAdapter
from alertmux.schema import NormalisedAlert

FIXTURE = (Path(__file__).parent / "fixtures" / "gdacs_rss.xml").read_bytes()
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_parse_returns_all_items():
    alerts = GdacsAdapter().parse(FIXTURE, NOW)
    assert len(alerts) == 5
    assert {a.event for a in alerts} == {
        "Earthquake",
        "Drought",
        "Wildfire",
        "Tropical Cyclone",
        "Flood",
    }


def test_alertlevel_is_never_mapped_to_cap_severity():
    """gdacs:alertlevel (Green/Orange/Red) is an expected-humanitarian-
    impact score, not a CAP severity judgement - GDACS never states how
    severe the hazard itself is. Mapping it onto Minor/Moderate/Severe/
    Extreme would assert a severity the source never gave. This is the
    central discipline this adapter exists to test."""
    alerts = GdacsAdapter().parse(FIXTURE, NOW)
    assert len(alerts) > 0
    for alert in alerts:
        assert alert.severity is None
        assert "severity" in alert.unavailable_fields
        assert alert.source_severity in {"Green", "Orange", "Red"}


def test_event_comes_from_eventtype_via_explicit_table():
    alerts = GdacsAdapter().parse(FIXTURE, NOW)
    by_event = {a.event: a for a in alerts}
    assert by_event["Earthquake"].id.startswith("gdacs:EQ:")
    assert by_event["Drought"].id.startswith("gdacs:DR:")
    assert by_event["Wildfire"].id.startswith("gdacs:WF:")


def test_unknown_eventtype_raises():
    tree_bytes = FIXTURE.replace(b"<gdacs:eventtype>EQ</gdacs:eventtype>", b"<gdacs:eventtype>XX</gdacs:eventtype>")
    with pytest.raises(ValueError, match="not a known code"):
        GdacsAdapter().parse(tree_bytes, NOW)


def test_id_is_derived_from_eventid_and_episodeid():
    alert = GdacsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == "gdacs:EQ:1559738:1726926"


def test_id_is_stable_across_two_independent_parses():
    """Verified against the live feed on 18 Aug 2026: the same 369
    (eventid, episodeid) pairs were returned on two independent fetches
    seconds apart. This test asserts the id is a pure function of those
    two fields, mirroring D2's replacement test for SWIC."""
    first = GdacsAdapter().parse(FIXTURE, NOW)
    second = GdacsAdapter().parse(FIXTURE, NOW)
    assert [a.id for a in first] == [a.id for a in second]


def test_missing_eventid_or_episodeid_raises():
    tree_bytes = FIXTURE.replace(
        b"<gdacs:episodeid>1726926</gdacs:episodeid>", b""
    )
    with pytest.raises(ValueError, match="eventid/gdacs:episodeid"):
        GdacsAdapter().parse(tree_bytes, NOW)


def test_urgency_certainty_expires_are_always_structurally_unavailable():
    """GDACS supplies none of these three fields at all - not even as
    unmapped source-native values."""
    for alert in GdacsAdapter().parse(FIXTURE, NOW):
        assert alert.urgency is None
        assert alert.certainty is None
        assert alert.expires is None
        assert alert.source_urgency is None
        assert alert.source_certainty is None
        assert "urgency" in alert.unavailable_fields
        assert "certainty" in alert.unavailable_fields
        assert "expires" in alert.unavailable_fields


def test_unavailable_fields_is_exhaustive_in_both_directions():
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields"}
    ]
    for alert in GdacsAdapter().parse(FIXTURE, NOW):
        for name in alert.unavailable_fields:
            assert getattr(alert, name) is None, name
        for name in optional:
            if getattr(alert, name) is None:
                assert name in alert.unavailable_fields, name


def test_timestamps_are_parsed_as_rfc822_and_converted_to_utc():
    alert = GdacsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.sent == datetime(2026, 8, 18, 6, 26, 5, tzinfo=timezone.utc)
    assert alert.onset == datetime(2026, 8, 18, 6, 2, 20, tzinfo=timezone.utc)


def test_naive_pubdate_raises_rather_than_assuming_utc():
    tree_bytes = FIXTURE.replace(
        b"<pubDate>Tue, 18 Aug 2026 06:26:05 GMT</pubDate>",
        b"<pubDate>Tue, 18 Aug 2026 06:26:05</pubDate>",
        1,
    )
    with pytest.raises(ValueError, match="no UTC offset"):
        GdacsAdapter().parse(tree_bytes, NOW)


def test_geometry_prefers_point_over_bbox():
    alert = GdacsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry == {"type": "Point", "coordinates": [98.416, -0.6858]}


def test_geometry_falls_back_to_bbox_polygon_when_no_point():
    tree_bytes = FIXTURE.replace(
        b"<geo:Point>\n        <geo:lat>-0.6858</geo:lat>\n"
        b"        <geo:long>98.416</geo:long>\n      </geo:Point>",
        b"",
        1,
    )
    alert = GdacsAdapter().parse(tree_bytes, NOW)[0]
    assert alert.geometry["type"] == "Polygon"
    assert alert.geometry["coordinates"][0][0] == [94.416, -4.6858]


def test_non_rss_200_raises_rather_than_reporting_zero_alerts():
    with pytest.raises(ValueError, match="rss"):
        GdacsAdapter().parse(b"<html><body>not a feed</body></html>", NOW)


def test_unparseable_xml_raises():
    with pytest.raises(ValueError, match="not parseable XML"):
        GdacsAdapter().parse(b"<rss><channel><item>", NOW)


def test_provenance_authority_is_gdacs():
    alert = GdacsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.authority == "gdacs"
    assert alert.provenance.source_id == "gdacs"
    assert alert.provenance.raw_reference == (
        "https://www.gdacs.org/contentdata/resources/EQ/1559738/cap_1559738.xml"
    )


def test_provenance_falls_back_to_link_when_no_cap_url():
    tree_bytes = FIXTURE.replace(
        b"<gdacs:cap>https://www.gdacs.org/contentdata/resources/EQ/1559738/cap_1559738.xml</gdacs:cap>",
        b"",
        1,
    )
    alert = GdacsAdapter().parse(tree_bytes, NOW)[0]
    assert alert.provenance.raw_reference == (
        "https://www.gdacs.org/report.aspx?eventtype=EQ&eventid=1559738"
    )


def test_country_used_as_area_description_when_present():
    alerts = GdacsAdapter().parse(FIXTURE, NOW)
    eq = next(a for a in alerts if a.event == "Earthquake")
    assert eq.area_description == "Indonesia"


def test_empty_country_element_is_treated_as_unavailable():
    """The TC fixture item has a self-closing <gdacs:country />."""
    alerts = GdacsAdapter().parse(FIXTURE, NOW)
    tc = next(a for a in alerts if a.event == "Tropical Cyclone")
    assert tc.area_description is None
    assert "area_description" in tc.unavailable_fields


def test_empty_feature_list_is_zero_alerts_not_an_error():
    empty = (
        b'<rss version="2.0" xmlns:gdacs="http://www.gdacs.org">'
        b"<channel><title>x</title></channel></rss>"
    )
    assert GdacsAdapter().parse(empty, NOW) == []


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(GdacsAdapter.URL).mock(
        return_value=httpx.Response(200, content=FIXTURE)
    )
    result = GdacsAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "gdacs"
    assert len(result.alerts) == 5
    assert result.error is None


@respx.mock
def test_fetch_reports_http_error_without_raising():
    respx.get(GdacsAdapter.URL).mock(return_value=httpx.Response(503))
    result = GdacsAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "503" in result.error


@respx.mock
def test_fetch_reports_timeout_without_raising():
    respx.get(GdacsAdapter.URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = GdacsAdapter().fetch()
    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


@respx.mock
def test_fetch_reports_non_rss_200_as_a_failure():
    respx.get(GdacsAdapter.URL).mock(
        return_value=httpx.Response(200, content=b"<html>not a feed</html>")
    )
    result = GdacsAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []


@respx.mock
def test_fetch_sets_user_agent_header():
    route = respx.get(GdacsAdapter.URL).mock(
        return_value=httpx.Response(200, content=FIXTURE)
    )
    GdacsAdapter().fetch()
    assert route.called
    request = route.calls[0].request
    assert "alertmux" in request.headers["User-Agent"]
