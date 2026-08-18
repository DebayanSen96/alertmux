"""Hits the real endpoints. Run with: pytest -m live

Excluded from the default run so CI never depends on third-party uptime.
"""

import pytest

from alertmux.adapters.gdacs import GdacsAdapter
from alertmux.adapters.nws import NwsAdapter
from alertmux.adapters.swic import SwicAdapter
from alertmux.adapters.usgs import UsgsAdapter

pytestmark = pytest.mark.live


def test_usgs_live_returns_alerts():
    result = UsgsAdapter().fetch()
    assert result.ok is True, result.error
    for alert in result.alerts:
        assert alert.provenance.authority == "us-usgs"
        assert alert.event


def test_swic_live_returns_effective_warnings():
    result = SwicAdapter(max_features=50).fetch()
    assert result.ok is True, result.error
    assert len(result.alerts) > 0
    for alert in result.alerts:
        assert alert.provenance.raw_reference
        assert alert.severity in {None, "Minor", "Moderate", "Severe", "Extreme"}


def test_swic_live_can_filter_to_nigeria():
    """mem=075 is ng-nimet. Returns 0 when Nigeria has no warning in force."""
    result = SwicAdapter(mem="075", max_features=50).fetch()
    assert result.ok is True, result.error
    for alert in result.alerts:
        assert alert.provenance.authority == "ng-nimet"


def test_nws_live_returns_alerts_and_never_relays_test_status():
    """The live feed carries a persistent KEEPALIVE test record - it
    must never survive parse() into the returned alerts."""
    result = NwsAdapter().fetch()
    assert result.ok is True, result.error
    assert len(result.alerts) > 0
    for alert in result.alerts:
        assert alert.provenance.authority == "us-noaa"
        assert alert.event != "Test Message"


def test_gdacs_live_returns_events_and_never_states_a_severity():
    """gdacs:alertlevel is an impact score, not a CAP severity judgement
    - GDACS never states hazard severity, so no live alert should ever
    carry a non-null `severity`."""
    result = GdacsAdapter().fetch()
    assert result.ok is True, result.error
    assert len(result.alerts) > 0
    for alert in result.alerts:
        assert alert.provenance.authority == "gdacs"
        assert alert.event
        assert alert.severity is None
