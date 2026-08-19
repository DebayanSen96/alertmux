from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.tsunami import CATEGORY_TO_EVENT, TsunamiAdapter
from alertmux.schema import NormalisedAlert

NTWC_FIXTURE = (Path(__file__).parent / "fixtures" / "tsunami_ntwc_atom.xml").read_bytes()
PTWC_FIXTURE = (Path(__file__).parent / "fixtures" / "tsunami_ptwc_atom.xml").read_bytes()
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _parse_ntwc():
    return TsunamiAdapter().parse(NTWC_FIXTURE, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)


def _parse_ptwc():
    return TsunamiAdapter().parse(PTWC_FIXTURE, NOW, "us-ptwc", TsunamiAdapter.PTWC_URL)


def test_parse_returns_the_entry_in_each_fixture():
    assert len(_parse_ntwc()) == 1
    assert len(_parse_ptwc()) == 1


def test_information_statement_never_produces_a_non_null_severity():
    """The regression guard that matters: an Information Statement is
    NOT a warning. Whatever else changes about this adapter, this must
    never start returning a severity for one. The category WAS stated
    (source_severity == "Information"), so the refusal to translate it
    belongs in unmapped_fields, not unavailable_fields."""
    for alerts in (_parse_ntwc(), _parse_ptwc()):
        for alert in alerts:
            assert alert.event == "Tsunami Information Statement"
            assert alert.severity is None
            assert "severity" in alert.unmapped_fields
            assert "severity" not in alert.unavailable_fields
            assert alert.source_severity == "Information"


def test_event_carries_the_level_verbatim_not_a_generic_tsunami():
    alert = _parse_ntwc()[0]
    assert alert.event == "Tsunami Information Statement"
    assert alert.event != "Tsunami"


def test_watch_advisory_warning_categories_map_through_the_explicit_table():
    """Not observed live (see module docstring) - these categories are
    exercised here against a modified copy of the real fixture, the
    same technique test_gdacs.py uses for its unknown-eventtype case."""
    for category, expected_event in CATEGORY_TO_EVENT.items():
        modified = NTWC_FIXTURE.replace(
            b"<strong>Category:</strong> Information<br/>",
            f"<strong>Category:</strong> {category}<br/>".encode(),
            1,
        )
        alert = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)[0]
        assert alert.event == expected_event
        assert alert.severity is None
        assert alert.source_severity == category
        assert "severity" in alert.unmapped_fields
        assert "severity" not in alert.unavailable_fields


def test_unmapped_category_is_quarantined_not_defaulted():
    """D3: a bulletin category this adapter has never seen is a
    malformed record for this feed, not something to default to
    'Information' or pass through raw."""
    modified = NTWC_FIXTURE.replace(
        b"<strong>Category:</strong> Information<br/>",
        b"<strong>Category:</strong> Bogus<br/>",
        1,
    )
    alerts = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)
    assert alerts.invalid_count == 1
    assert any("not a known bulletin level" in sample for sample in alerts.invalid_samples)
    assert len(alerts) == 0


def test_missing_category_is_quarantined():
    modified = NTWC_FIXTURE.replace(
        b"<strong>Category:</strong> Information<br/>", b""
    )
    alerts = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)
    assert alerts.invalid_count == 1
    assert any("no Category:" in sample for sample in alerts.invalid_samples)


def test_missing_entry_id_is_quarantined():
    modified = NTWC_FIXTURE.replace(
        b"<id>urn:uuid:3f6aa6dd-f007-48f2-8ff7-806d50e1da95</id>", b""
    )
    alerts = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)
    assert alerts.invalid_count == 1
    assert any("no <id>" in sample for sample in alerts.invalid_samples)


def test_id_is_namespaced_by_centre_authority():
    ntwc = _parse_ntwc()[0]
    ptwc = _parse_ptwc()[0]
    assert ntwc.id == "tsunami-gov:us-ntwc:urn:uuid:3f6aa6dd-f007-48f2-8ff7-806d50e1da95"
    assert ptwc.id == "tsunami-gov:us-ptwc:urn:uuid:b3ad61bd-2bc5-4a1c-ad6b-53a2dd052cbf"


def test_id_is_stable_across_two_independent_parses():
    """Verified against the live feed 17-18 Aug 2026: two independent
    fetches, seconds apart, returned byte-identical responses, entry
    <id> included. Mirrors D2's replacement test for GDACS/SWIC."""
    first = _parse_ntwc()
    second = _parse_ntwc()
    assert [a.id for a in first] == [a.id for a in second]


def test_provenance_authority_and_source_id():
    ntwc = _parse_ntwc()[0]
    assert ntwc.provenance.authority == "us-ntwc"
    assert ntwc.provenance.source_id == "tsunami-gov"
    assert ntwc.provenance.source_url == TsunamiAdapter.NTWC_URL

    ptwc = _parse_ptwc()[0]
    assert ptwc.provenance.authority == "us-ptwc"


def test_raw_reference_prefers_capxml_link():
    alert = _parse_ntwc()[0]
    assert alert.provenance.raw_reference == (
        "https://www.tsunami.gov/events/PAAQ/2026/08/17/tjxl6l/1/WEAK53/PAAQCAP.xml"
    )


def test_raw_reference_falls_back_to_bulletin_link_when_no_capxml():
    modified = NTWC_FIXTURE.replace(
        b'<link rel="related" title="CapXML document" '
        b'href="https://www.tsunami.gov/events/PAAQ/2026/08/17/tjxl6l/1/WEAK53/PAAQCAP.xml" '
        b'type="application/cap+xml" />',
        b"",
        1,
    )
    alert = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)[0]
    assert alert.provenance.raw_reference == (
        "https://www.tsunami.gov/events/PAAQ/2026/08/17/tjxl6l/1/WEAK53/WEAK53.txt"
    )


def test_geometry_from_geo_lat_long():
    alert = _parse_ntwc()[0]
    assert alert.geometry == {"type": "Point", "coordinates": [-154.8, 57.2]}


def test_area_description_is_the_entry_title():
    alert = _parse_ntwc()[0]
    assert alert.area_description == "100 miles SW of Kodiak City, Alaska"


def test_timestamps_are_parsed_as_iso_and_converted_to_utc():
    alert = _parse_ntwc()[0]
    assert alert.sent == datetime(2026, 8, 17, 20, 39, 52, tzinfo=timezone.utc)
    assert alert.onset is None


def test_naive_updated_is_quarantined_rather_than_assuming_utc():
    modified = NTWC_FIXTURE.replace(
        b"<updated>2026-08-17T20:39:52Z</updated>\n<geo:lat>",
        b"<updated>2026-08-17T20:39:52</updated>\n<geo:lat>",
        1,
    )
    alerts = TsunamiAdapter().parse(modified, NOW, "us-ntwc", TsunamiAdapter.NTWC_URL)
    assert alerts.invalid_count == 1
    assert any("no UTC offset" in sample for sample in alerts.invalid_samples)


def test_urgency_certainty_expires_onset_are_always_structurally_unavailable():
    for alert in _parse_ntwc() + _parse_ptwc():
        assert alert.urgency is None
        assert alert.certainty is None
        assert alert.expires is None
        assert alert.onset is None
        assert alert.source_urgency is None
        assert alert.source_certainty is None
        for name in ("urgency", "certainty", "expires", "onset", "instruction"):
            assert name in alert.unavailable_fields
        # severity is different -- the category IS supplied, so it is
        # declined (unmapped), not structurally absent.
        assert "severity" in alert.unmapped_fields
        assert "severity" not in alert.unavailable_fields


def test_unavailable_fields_is_exhaustive_in_both_directions():
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields", "unmapped_fields"}
    ]
    for alert in _parse_ntwc() + _parse_ptwc():
        for name in alert.unavailable_fields:
            assert getattr(alert, name) is None, name
        for name in alert.unmapped_fields:
            assert getattr(alert, name) is None, name
        for name in optional:
            if getattr(alert, name) is None:
                assert (
                    name in alert.unavailable_fields or name in alert.unmapped_fields
                ), name


def test_unavailable_and_unmapped_never_overlap():
    for alert in _parse_ntwc() + _parse_ptwc():
        overlap = set(alert.unavailable_fields) & set(alert.unmapped_fields)
        assert overlap == set(), overlap


def test_non_atom_200_raises_rather_than_reporting_zero_tsunamis():
    with pytest.raises(ValueError, match="Atom"):
        TsunamiAdapter().parse(b"<html><body>not a feed</body></html>", NOW, "us-ntwc", "x")


def test_unparseable_xml_raises():
    with pytest.raises(ValueError, match="not parseable XML"):
        TsunamiAdapter().parse(b"<feed><entry>", NOW, "us-ntwc", "x")


def test_empty_entry_list_is_zero_alerts_not_an_error():
    empty = (
        b'<feed xmlns="http://www.w3.org/2005/Atom">'
        b"<title>No active bulletins</title></feed>"
    )
    assert TsunamiAdapter().parse(empty, NOW, "us-ntwc", "x") == []


@respx.mock
def test_fetch_merges_both_centres():
    respx.get(TsunamiAdapter.NTWC_URL).mock(return_value=httpx.Response(200, content=NTWC_FIXTURE))
    respx.get(TsunamiAdapter.PTWC_URL).mock(return_value=httpx.Response(200, content=PTWC_FIXTURE))
    result = TsunamiAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "tsunami-gov"
    assert len(result.alerts) == 2
    assert {a.provenance.authority for a in result.alerts} == {"us-ntwc", "us-ptwc"}


@respx.mock
def test_fetch_survives_one_centre_failing():
    respx.get(TsunamiAdapter.NTWC_URL).mock(return_value=httpx.Response(200, content=NTWC_FIXTURE))
    respx.get(TsunamiAdapter.PTWC_URL).mock(return_value=httpx.Response(503))
    result = TsunamiAdapter().fetch()
    assert result.ok is True
    assert len(result.alerts) == 1
    assert result.alerts[0].provenance.authority == "us-ntwc"
    assert result.invalid_count >= 1
    assert any("us-ptwc" in sample for sample in result.invalid_samples)


@respx.mock
def test_fetch_reports_both_centres_down_as_a_failure():
    respx.get(TsunamiAdapter.NTWC_URL).mock(return_value=httpx.Response(503))
    respx.get(TsunamiAdapter.PTWC_URL).mock(return_value=httpx.Response(503))
    result = TsunamiAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "us-ntwc" in result.error
    assert "us-ptwc" in result.error


@respx.mock
def test_fetch_quarantines_one_bad_entry_and_still_returns_the_rest():
    modified = NTWC_FIXTURE.replace(
        b"<strong>Category:</strong> Information<br/>",
        b"<strong>Category:</strong> Bogus<br/>",
        1,
    )
    respx.get(TsunamiAdapter.NTWC_URL).mock(return_value=httpx.Response(200, content=modified))
    respx.get(TsunamiAdapter.PTWC_URL).mock(return_value=httpx.Response(200, content=PTWC_FIXTURE))
    result = TsunamiAdapter().fetch()
    assert result.ok is True
    assert len(result.alerts) == 1
    assert result.invalid_count == 1


@respx.mock
def test_fetch_reports_non_atom_200_as_a_centre_failure_not_zero_alerts():
    respx.get(TsunamiAdapter.NTWC_URL).mock(
        return_value=httpx.Response(200, content=b"<html>not a feed</html>")
    )
    respx.get(TsunamiAdapter.PTWC_URL).mock(
        return_value=httpx.Response(200, content=b"<html>not a feed</html>")
    )
    result = TsunamiAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []


@respx.mock
def test_fetch_sets_user_agent_header():
    ntwc_route = respx.get(TsunamiAdapter.NTWC_URL).mock(
        return_value=httpx.Response(200, content=NTWC_FIXTURE)
    )
    respx.get(TsunamiAdapter.PTWC_URL).mock(return_value=httpx.Response(200, content=PTWC_FIXTURE))
    TsunamiAdapter().fetch()
    assert ntwc_route.called
    request = ntwc_route.calls[0].request
    assert "alertmux" in request.headers["User-Agent"]
