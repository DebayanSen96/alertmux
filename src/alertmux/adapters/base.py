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


@runtime_checkable
class Adapter(Protocol):
    """What every source adapter must provide."""

    source_id: str

    def fetch(self) -> FetchResult:
        """Fetch, parse and normalise. Never raises on source failure."""
        ...
