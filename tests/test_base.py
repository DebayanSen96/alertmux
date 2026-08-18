from datetime import datetime, timezone

from alertmux.adapters.base import FetchResult


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
