from datetime import datetime, timezone

from alertmux.adapters.base import FetchResult, RecordQuarantine


def test_ok_result_carries_alerts():
    result = FetchResult(
        source_id="usgs",
        ok=True,
        alerts=[],
        retrieved_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        latency_ms=42,
    )
    assert result.ok is True
    assert result.error is None


def test_failed_result_carries_error_and_no_alerts():
    result = FetchResult(
        source_id="usgs",
        ok=False,
        alerts=[],
        error="timeout after 30s",
        retrieved_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        latency_ms=30000,
    )
    assert result.ok is False
    assert "timeout" in result.error


def test_fetch_result_defaults_invalid_count_to_zero():
    result = FetchResult(
        source_id="usgs", ok=True, alerts=[],
        retrieved_at=datetime(2026, 8, 17, tzinfo=timezone.utc), latency_ms=1,
    )
    assert result.invalid_count == 0
    assert result.invalid_samples == []


def test_record_quarantine_behaves_like_a_list_of_alerts():
    """RecordQuarantine must be a drop-in replacement for a plain
    list[NormalisedAlert] everywhere adapter.parse() is called."""
    q = RecordQuarantine()
    assert q == []
    assert len(q) == 0
    q.append("alert-1")
    q.append("alert-2")
    assert len(q) == 2
    assert q[0] == "alert-1"
    assert list(q) == ["alert-1", "alert-2"]


def test_record_quarantine_counts_and_samples_failures():
    q = RecordQuarantine()
    q.quarantine(ValueError("no capurl"))
    q.quarantine(ValueError("no event"))
    assert q.invalid_count == 2
    assert q.invalid_samples == ["ValueError: no capurl", "ValueError: no event"]


def test_record_quarantine_caps_samples_but_not_the_count():
    """A mass failure must not produce a megabyte of near-identical
    errors, but the count itself must stay exact."""
    q = RecordQuarantine()
    for i in range(20):
        q.quarantine(ValueError(f"bad record {i}"))
    assert q.invalid_count == 20
    assert len(q.invalid_samples) == 5
