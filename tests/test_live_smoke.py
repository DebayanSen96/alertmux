"""Hits the real endpoints. Run with: pytest -m live

Excluded from the default run so CI never depends on third-party uptime.
"""

import pytest

from alertmux.adapters.eonet import EonetAdapter
from alertmux.adapters.gdacs import GdacsAdapter
from alertmux.adapters.nws import NwsAdapter
from alertmux.adapters.swic import SwicAdapter
from alertmux.adapters.tsunami import TsunamiAdapter
from alertmux.adapters.usgs import UsgsAdapter
from alertmux.registry import get_register

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


def test_eonet_live_returns_events_and_never_states_warning_concepts():
    """EONET publishes what satellites observed, not what an authority
    is warning about - severity/urgency/certainty/expires genuinely do
    not apply to any live event, regardless of category or how many
    geometries it has accumulated while being tracked."""
    result = EonetAdapter().fetch()
    assert result.ok is True, result.error
    assert len(result.alerts) > 0
    for alert in result.alerts:
        assert alert.provenance.authority == "nasa-eonet"
        assert alert.event
        assert alert.severity is None
        assert alert.urgency is None
        assert alert.certainty is None
        assert alert.expires is None


def test_tsunami_live_never_states_a_severity_for_an_information_statement():
    """The central safety guard for this source: whatever tsunami.gov's
    NTWC/PTWC feeds hold right now (zero alerts is the normal state of
    the world), no live alert whose event is an Information Statement
    may ever carry a non-null `severity`. Both feeds must at least
    answer (ok=True), even if neither has an active bulletin."""
    result = TsunamiAdapter().fetch()
    assert result.ok is True, result.error
    for alert in result.alerts:
        assert alert.provenance.authority in {"us-ntwc", "us-ptwc"}
        assert alert.event in {
            "Tsunami Information Statement",
            "Tsunami Watch",
            "Tsunami Advisory",
            "Tsunami Warning",
        }
        assert alert.severity is None
        if alert.event == "Tsunami Information Statement":
            assert alert.source_severity == "Information"


def test_wmo_register_live_returns_at_least_250_authorities():
    """Measured 18 Aug 2026: 300 items. A wide floor rather than an
    exact count -- WMO adds/removes entries over time and this is a
    smoke test, not a pin on their register's exact size."""
    authorities, fetched_at, age, error = get_register()
    assert error is None
    assert len(authorities) >= 250
    assert fetched_at is not None
    for authority in authorities:
        assert authority.title
