from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from alertmux.schema import NormalisedAlert, Provenance


def _provenance() -> Provenance:
    return Provenance(
        authority="ng-nimet",
        source_id="wmo-swic",
        source_url="https://severeweather.wmo.int/g/wfs",
        retrieved_at=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
        raw_reference="ng-nimet-en/2026/08/17/14/50/16-e28.xml",
    )


def test_alert_requires_provenance():
    with pytest.raises(ValidationError):
        NormalisedAlert(id="x", event="THUNDERSTORMS")


def test_alert_defaults_unknown_fields_to_none():
    alert = NormalisedAlert(
        id="wmo-swic:abc",
        event="THUNDERSTORMS",
        provenance=_provenance(),
    )
    assert alert.headline is None
    assert alert.severity is None
    assert alert.geometry is None
    assert alert.unavailable_fields == []


def test_unavailable_fields_records_what_source_omitted():
    alert = NormalisedAlert(
        id="wmo-swic:abc",
        event="THUNDERSTORMS",
        provenance=_provenance(),
        unavailable_fields=["headline", "description", "geometry"],
    )
    assert "geometry" in alert.unavailable_fields


def test_severity_stays_none_when_only_source_severity_known():
    alert = NormalisedAlert(
        id="wmo-swic:abc",
        event="THUNDERSTORMS",
        provenance=_provenance(),
        source_severity="3",
        unavailable_fields=["severity"],
    )
    assert alert.source_severity == "3"
    assert alert.severity is None
