from datetime import datetime, timezone

from alertmux.dedupe import event_key, group_duplicates, summarise_duplicates
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _alert(
    alert_id: str,
    source_id: str,
    event: str = "Heat Advisory",
    area_description: str | None = "Cook County, IL",
    **extra,
) -> NormalisedAlert:
    return NormalisedAlert(
        id=alert_id,
        event=event,
        area_description=area_description,
        provenance=Provenance(
            authority="us-noaa",
            source_id=source_id,
            source_url="https://example.test",
            retrieved_at=NOW,
        ),
        **extra,
    )


def test_two_records_same_event_and_area_form_one_group():
    a = _alert("swic:1", "wmo-swic")
    b = _alert("nws:1", "nws")
    groups = group_duplicates([a, b])
    assert len(groups) == 1
    assert set(groups[0].alert_ids) == {"swic:1", "nws:1"}


def test_richer_record_is_preferred_and_reason_names_count():
    poor = _alert("swic:1", "wmo-swic")
    rich = _alert(
        "nws:1",
        "nws",
        headline="Heat Advisory issued",
        description="Dangerous heat expected.",
        severity="Moderate",
        urgency="Expected",
        certainty="Likely",
        source_severity="Moderate",
        source_urgency="Expected",
        source_certainty="Likely",
        sent=NOW,
        onset=NOW,
        expires=NOW,
    )
    groups = group_duplicates([poor, rich])
    assert len(groups) == 1
    group = groups[0]
    assert group.preferred_id == "nws:1"
    # 14, not 13: schema.py gained `instruction` (issue #4), which
    # _OPTIONAL_FIELDS counts alongside every other optional field.
    assert "12 of 14" in group.reason


def test_same_event_different_area_not_grouped():
    a = _alert("a:1", "src-a", area_description="Cook County, IL")
    b = _alert("b:1", "src-b", area_description="Lake County, IL")
    assert group_duplicates([a, b]) == []


def test_same_area_different_event_not_grouped():
    a = _alert("a:1", "src-a", event="Heat Advisory")
    b = _alert("b:1", "src-b", event="Flood Warning")
    assert group_duplicates([a, b]) == []


def test_case_and_whitespace_differences_still_group():
    a = _alert("a:1", "src-a", event="Heat Advisory")
    b = _alert("b:1", "src-b", event="heat advisory ")
    groups = group_duplicates([a, b])
    assert len(groups) == 1
    assert set(groups[0].alert_ids) == {"a:1", "b:1"}


def test_missing_area_description_excluded_from_grouping():
    a = _alert("a:1", "src-a", area_description=None)
    assert event_key(a) is None
    b = _alert("b:1", "src-b", area_description=None)
    assert group_duplicates([a, b]) == []


def test_tie_break_is_deterministic_across_runs():
    a = _alert("z:1", "src-a")
    b = _alert("a:1", "src-b")
    first = group_duplicates([a, b])[0].preferred_id
    second = group_duplicates([a, b])[0].preferred_id
    assert first == second == "a:1"


def test_single_record_is_not_a_group():
    a = _alert("a:1", "src-a")
    assert group_duplicates([a]) == []


def test_two_records_same_key_same_source_not_grouped():
    """The 98-Angola-wildfire case: GDACS sets area_description to just
    the country name, so 98 distinct fires (each with its own
    gdacs:eventid) all key to `wildfire|angola`. GDACS's own distinct
    ids are authoritative -- this must never be reported as a group."""
    a = _alert(
        "gdacs:WF:1030038:17",
        "gdacs",
        event="Wildfire",
        area_description="Angola",
    )
    b = _alert(
        "gdacs:WF:1030084:21",
        "gdacs",
        event="Wildfire",
        area_description="Angola",
    )
    assert group_duplicates([a, b]) == []


def test_three_records_two_from_one_source_group_rejected_and_counted():
    a = _alert("gdacs:1", "gdacs", event="Wildfire", area_description="Angola")
    b = _alert("gdacs:2", "gdacs", event="Wildfire", area_description="Angola")
    c = _alert("nws:1", "nws", event="Wildfire", area_description="Angola")
    groups, ambiguous = summarise_duplicates([a, b, c])
    assert groups == []
    assert ambiguous == 1


def test_two_records_same_key_different_sources_still_grouped():
    a = _alert("a:1", "src-a")
    b = _alert("b:1", "src-b")
    groups, ambiguous = summarise_duplicates([a, b])
    assert len(groups) == 1
    assert set(groups[0].alert_ids) == {"a:1", "b:1"}
    assert ambiguous == 0


def test_four_source_group_one_record_each_still_valid():
    members = [
        _alert("a:1", "src-a"),
        _alert("b:1", "src-b"),
        _alert("c:1", "src-c"),
        _alert("d:1", "src-d"),
    ]
    groups, ambiguous = summarise_duplicates(members)
    assert len(groups) == 1
    assert set(groups[0].alert_ids) == {"a:1", "b:1", "c:1", "d:1"}
    assert ambiguous == 0


def test_no_duplicates_yields_empty_list():
    a = _alert("a:1", "src-a", area_description="Cook County, IL")
    b = _alert("b:1", "src-b", area_description="Lake County, IL")
    assert group_duplicates([a, b]) == []
