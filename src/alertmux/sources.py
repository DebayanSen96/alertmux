"""The discovery report: what alertmux covers, and what it misses.

`/health` answers "is it working" for a monitor. This module answers a
different question: "what does this system actually cover, and what does
it miss" -- for a human deciding whether to rely on it for a given hazard
or region.

**Hazard classification here is heuristic and must never be mistaken for
authoritative.** `classify_hazard` maps a source's free-text `event`
field to one of a fixed set of hazard families using an explicit keyword
table (see `HAZARD_KEYWORDS` below). `event` text varies by authority --
"THUNDERSTORMS", "Heat Advisory", "Wildfire" -- so this WILL misfile some
alerts (e.g. an event whose text matches no keyword lands in no family
at all, and a family whose keyword also appears in an unrelated event's
wording would be misclassified). This is a summary aid only. It is used
solely to build `hazard_coverage` for `/sources`; it never touches
`NormalisedAlert.event` or any other field a consumer of `/alerts` sees,
per principle 1 (relay, never issue) and principle 2 (nothing is ever
inferred onto the alert data itself).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, Field

from alertmux.query import AlertsResponse
from alertmux.schema import DISCLAIMER, NormalisedAlert

# Deliberately explicit and small. Each family maps to substrings looked
# up in the case-folded `event` string. Order does not matter: an event
# can only land in the first family whose keyword list matches (see
# classify_hazard), so a family list here is a statement of "these
# strings mean this family," not a priority ranking.
#
# This is heuristic, not authoritative -- see the module docstring. It
# will misclassify or fail to classify an event whose wording this table
# has not seen. Extend it only from real observed `event` strings (the
# same evidence bar as DECISIONS.md D1), never a guess at future wording.
HAZARD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earthquake": ("earthquake", "quake"),
    "tsunami": ("tsunami",),
    "volcano": ("volcano", "volcanic"),
    "wildfire": ("wildfire", "fire"),
    "drought": ("drought",),
    "flood": ("flood",),
    "storm/wind": (
        "storm",
        "wind",
        "cyclone",
        "hurricane",
        "typhoon",
        "tornado",
        "thunderstorm",
    ),
    "heat": ("heat",),
    "rain": ("rain",),
    "snow/ice": ("snow", "ice", "blizzard", "frost", "freeze"),
}


def classify_hazard(event: str | None) -> str | None:
    """Map a free-text `event` string to a hazard family, or None.

    Heuristic only -- see the module docstring. Never call this on data
    that ends up back in a `NormalisedAlert`; it exists purely to build
    the `/sources` summary.
    """
    if not event:
        return None
    folded = event.casefold()
    for family, keywords in HAZARD_KEYWORDS.items():
        if any(keyword in folded for keyword in keywords):
            return family
    return None


class SourceSummary(BaseModel):
    """One adapter's identity, structural shape, and this fetch's outcome."""

    source_id: str
    endpoint: str
    # Authorities actually observed in this fetch's alerts -- not a
    # declared/static list. A source that returned nothing this fetch
    # (down, or genuinely quiet) reports an empty list here even if it
    # normally carries many.
    authorities: list[str] = Field(default_factory=list)
    authority_count: int = 0
    # The adapter's own STRUCTURAL_GAPS -- fields this source can never
    # supply, independent of any particular fetch. Reportable without
    # running a fetch at all.
    structural_gaps: list[str] = Field(default_factory=list)
    ok: bool
    alert_count: int = 0
    latency_ms: int | None = None
    truncated: bool = False


class SourcesResponse(BaseModel):
    """`GET /sources` -- coverage, not health.

    `hazard_coverage` and `uncovered_hazards` are built from
    `classify_hazard`, a heuristic keyword match over each alert's free
    -text `event` field (see module docstring). Everything else here is
    measured directly from this fetch's `AlertsResponse`.
    """

    sources: list[SourceSummary] = Field(default_factory=list)
    # Hazard family -> sorted list of source_ids that returned at least
    # one alert classified into that family this fetch.
    hazard_coverage: dict[str, list[str]] = Field(default_factory=dict)
    # Families with zero contributing sources this fetch. Listed
    # explicitly rather than omitted -- surfacing the gap is the point
    # of this endpoint (see module docstring: an authority count alone
    # hides a missing hazard family).
    uncovered_hazards: list[str] = Field(default_factory=list)
    retrieved_at: datetime
    disclaimer: str = DISCLAIMER


def _adapter_endpoint(adapter) -> str:
    """Best-effort endpoint URL for display. Every current adapter
    exposes `URL`; fall back to an empty string rather than raising for
    a hypothetical adapter that does not."""
    return getattr(adapter, "URL", "")


def _adapter_structural_gaps(adapter) -> list[str]:
    """Reported without running a fetch -- see the adapter class
    attributes hoisted for exactly this purpose."""
    return sorted(getattr(adapter, "STRUCTURAL_GAPS", ()))


def build_sources_response(
    adapters, response: AlertsResponse
) -> SourcesResponse:
    """Build the discovery report from an already-collected `AlertsResponse`.

    Purely a read: never mutates `response` or anything reachable from
    it (including `response.alerts`), so it is safe to call with the
    shared cached object, per D9.
    """
    alerts_by_source: dict[str, list[NormalisedAlert]] = defaultdict(list)
    for alert in response.alerts:
        alerts_by_source[alert.provenance.source_id].append(alert)

    status_by_source = {s.source_id: s for s in response.sources}

    hazard_coverage: dict[str, set[str]] = {family: set() for family in HAZARD_KEYWORDS}

    sources: list[SourceSummary] = []
    for adapter in adapters:
        source_id = getattr(adapter, "source_id", "unknown")
        status = status_by_source.get(source_id)
        source_alerts = alerts_by_source.get(source_id, [])

        authorities = sorted({a.provenance.authority for a in source_alerts})

        for alert in source_alerts:
            family = classify_hazard(alert.event)
            if family is not None:
                hazard_coverage[family].add(source_id)

        sources.append(
            SourceSummary(
                source_id=source_id,
                endpoint=_adapter_endpoint(adapter),
                authorities=authorities,
                authority_count=len(authorities),
                structural_gaps=_adapter_structural_gaps(adapter),
                ok=status.ok if status is not None else False,
                alert_count=status.alert_count if status is not None else 0,
                latency_ms=status.latency_ms if status is not None else None,
                truncated=status.truncated if status is not None else False,
            )
        )

    coverage_sorted = {
        family: sorted(source_ids) for family, source_ids in hazard_coverage.items()
    }
    uncovered = sorted(
        family for family, source_ids in coverage_sorted.items() if not source_ids
    )

    return SourcesResponse(
        sources=sources,
        hazard_coverage=coverage_sorted,
        uncovered_hazards=uncovered,
        # The timestamp of the underlying fetch, same as /health's
        # checked_at -- this report describes that fetch, not the
        # instant this summary was assembled from it.
        retrieved_at=response.retrieved_at,
    )
