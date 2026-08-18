"""The adapter contract.

An adapter knows its own source's quirks and nothing about any other
adapter, the query layer, or the API. It never raises on a source
failure — it returns ok=False with an error, so one broken source can
never take down a response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from alertmux.schema import NormalisedAlert


class FetchResult(BaseModel):
    """One adapter's answer, including how it failed if it did."""

    source_id: str
    ok: bool
    alerts: list[NormalisedAlert] = Field(default_factory=list)
    error: str | None = None
    retrieved_at: datetime
    latency_ms: int

    # The source had more alerts in force than it returned. ok stays True
    # — the alerts present are correct — but the answer is incomplete, so
    # this must reach AlertsResponse.partial. Silent partial success is a
    # bug, and a dropped warning is the worst kind of missing data.
    truncated: bool = False
    matched: int | None = None
    returned: int | None = None

    # Per-record quarantine (D3, superseded 2026-08-18). A record that
    # cannot be parsed is skipped rather than aborting the whole fetch --
    # never invented, always counted. invalid_count > 0 forces `partial`
    # exactly like `truncated` does: a source that quarantined records
    # answered, but incompletely, and that must never be silent.
    invalid_count: int = 0
    # Capped (see RECORD_SAMPLE_CAP) so a mass failure -- e.g. a source
    # reshaping its schema -- cannot produce a megabyte of near-identical
    # errors. Exception type + message for the first few quarantined
    # records, enough for an operator to diagnose *why* without a fetch
    # of their own.
    invalid_samples: list[str] = Field(default_factory=list)

    # A paginating adapter (see adapters/swic.py) can see the same
    # record twice if the server's ordering is unstable between
    # requests. Deduplicated by id before entering `alerts`, and
    # counted here rather than silently -- a duplicate never inflates
    # the alert count, but disappearing without a trace would still be
    # the kind of silent adjustment principle 4 forbids.
    duplicate_count: int = 0


@runtime_checkable
class Adapter(Protocol):
    """What every source adapter must provide."""

    source_id: str

    def fetch(self) -> FetchResult:
        """Fetch, parse and normalise. Never raises on source failure."""
        ...


# Cap on FetchResult.invalid_samples / SourceStatus.invalid_samples. A
# mass quarantine is itself a diagnostic signal (the source has probably
# changed shape) but a megabyte of identical exception strings is not
# more diagnosable than five of them -- it is just noise.
RECORD_SAMPLE_CAP = 5


class RecordQuarantine(list):
    """A list of successfully parsed records, carrying the stats for the
    records that were quarantined instead of raising.

    Subclassing list is deliberate: every existing adapter.parse() caller
    treats the return value as a plain list[NormalisedAlert] (len(),
    indexing, iteration, ``== []``). Wrapping the alerts in a subclass
    keeps every one of those call sites working unchanged, while callers
    that need the quarantine stats read them off the same object via
    ``.invalid_count`` / ``.invalid_samples``.
    """

    def __init__(self, alerts: list[NormalisedAlert] | None = None) -> None:
        super().__init__(alerts or [])
        self.invalid_count = 0
        self.invalid_samples: list[str] = []

    def quarantine(self, exc: Exception) -> None:
        """Record a record-level parse failure. Never invents a value,
        never re-raises -- the record is simply dropped."""
        self.invalid_count += 1
        if len(self.invalid_samples) < RECORD_SAMPLE_CAP:
            self.invalid_samples.append(f"{type(exc).__name__}: {exc}")
