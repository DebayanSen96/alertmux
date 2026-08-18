"""Aggregation across adapters.

One source being down must never take down a response. Whenever any
source fails — or returns a truncated answer — the response is labelled
partial: returning incomplete hazard data as if it were complete is the
worst thing this system can do.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from alertmux.schema import DISCLAIMER, NormalisedAlert


class SourceStatus(BaseModel):
    source_id: str
    ok: bool
    error: str | None = None
    latency_ms: int | None = None
    alert_count: int = 0
    # The source answered, but with fewer alerts than it holds.
    truncated: bool = False
    matched: int | None = None
    returned: int | None = None


class AlertsResponse(BaseModel):
    alerts: list[NormalisedAlert] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)
    partial: bool = False
    retrieved_at: datetime
    # Populated only when a requested ?authority= matched nothing, so a
    # typo is distinguishable from a genuinely quiet day.
    available_authorities: list[str] | None = None
    disclaimer: str = DISCLAIMER


def collect(adapters) -> AlertsResponse:
    """Fetch every adapter and merge, never raising on a source failure."""
    alerts: list[NormalisedAlert] = []
    statuses: list[SourceStatus] = []

    for adapter in adapters:
        try:
            result = adapter.fetch()
        except Exception as exc:  # noqa: BLE001 - a broken adapter is a status
            statuses.append(
                SourceStatus(
                    source_id=getattr(adapter, "source_id", "unknown"),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        alerts.extend(result.alerts)
        statuses.append(
            SourceStatus(
                source_id=result.source_id,
                ok=result.ok,
                error=result.error,
                latency_ms=result.latency_ms,
                alert_count=len(result.alerts),
                truncated=result.truncated,
                matched=result.matched,
                returned=result.returned,
            )
        )

    return AlertsResponse(
        alerts=alerts,
        sources=statuses,
        # A truncated source is incomplete data even though ok is True.
        partial=any((not s.ok) or s.truncated for s in statuses),
        retrieved_at=datetime.now(tz=timezone.utc),
    )
