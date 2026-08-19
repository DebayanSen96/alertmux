"""Aggregation across adapters.

One source being down must never take down a response. Whenever any
source fails — or returns a truncated answer — the response is labelled
partial: returning incomplete hazard data as if it were complete is the
worst thing this system can do.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from alertmux.dedupe import DuplicateGroup, summarise_duplicates
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
    # Per-record quarantine (D3, superseded 2026-08-18). Records that
    # could not be parsed were skipped rather than aborting the whole
    # fetch; invalid_count > 0 still forces `partial` on AlertsResponse,
    # exactly like `truncated` -- a source that quarantined records
    # answered, but incompletely.
    invalid_count: int = 0
    invalid_samples: list[str] = Field(default_factory=list)
    # See FetchResult.duplicate_count -- a paginating adapter's own
    # cross-page duplicates, never a claim about cross-source overlap
    # (that is dedupe.py's job).
    duplicate_count: int = 0


class AlertsResponse(BaseModel):
    alerts: list[NormalisedAlert] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)
    partial: bool = False
    retrieved_at: datetime
    # Populated only when a requested ?authority= matched nothing, so a
    # typo is distinguishable from a genuinely quiet day.
    available_authorities: list[str] | None = None
    # True exactly when `available_authorities` was built from a partial
    # fetch (D7, extended). The list is only ever assembled from alerts
    # this fetch actually returned -- a source that was down during that
    # fetch contributes nothing to it, so a perfectly valid authority can
    # be missing from the list for a reason that has nothing to do with
    # whether the authority exists. This flag makes that distinction
    # explicit instead of leaving the caller to notice `partial` was also
    # true. Always `None` when `available_authorities` itself is `None`.
    available_authorities_partial: bool | None = None
    disclaimer: str = DISCLAIMER
    # Reports cross-source duplication (e.g. SWIC's us-noaa slice vs a
    # direct NWS fetch). Never changes `alerts` -- see dedupe.py and
    # DECISIONS.md D13.
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)
    # Candidate groups found by (event, area_description) but rejected
    # because one source contributed two or more of the records --
    # meaning the source's own ids disagree with the key about whether
    # the records are distinct events, so no confident pairing could be
    # made. Not silently dropped (principle 4): this count is the only
    # trace of them in the response. See dedupe.py's summarise_duplicates
    # and DECISIONS.md D13.
    ambiguous_duplicate_groups: int = 0


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
                invalid_count=result.invalid_count,
                invalid_samples=result.invalid_samples,
                duplicate_count=result.duplicate_count,
            )
        )

    duplicate_groups, ambiguous_duplicate_groups = summarise_duplicates(alerts)

    return AlertsResponse(
        alerts=alerts,
        sources=statuses,
        # A truncated source, or one that quarantined records, is
        # incomplete data even though ok is True. Silent partial success
        # is a bug (DECISIONS.md principle 4 / D3).
        partial=any((not s.ok) or s.truncated or s.invalid_count > 0 for s in statuses),
        retrieved_at=datetime.now(tz=timezone.utc),
        duplicate_groups=duplicate_groups,
        ambiguous_duplicate_groups=ambiguous_duplicate_groups,
    )
