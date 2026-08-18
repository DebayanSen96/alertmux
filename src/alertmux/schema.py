"""The contract every adapter satisfies.

A field a source does not supply is None and its name is listed in
unavailable_fields. Values are never inferred, defaulted, or guessed —
fabricating a severity a source did not state is the exact failure this
project exists to avoid.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    """Where an alert came from. Mandatory on every alert."""

    authority: str
    source_id: str
    source_url: str
    retrieved_at: datetime
    raw_reference: str | None = None


class NormalisedAlert(BaseModel):
    """One hazard alert, normalised across sources."""

    id: str
    event: str

    headline: str | None = None
    description: str | None = None
    area_description: str | None = None

    # CAP named levels. None until a source's codes are confirmed.
    severity: str | None = None
    urgency: str | None = None
    certainty: str | None = None

    # Raw source-native values, always preserved.
    source_severity: str | None = None
    source_urgency: str | None = None
    source_certainty: str | None = None

    sent: datetime | None = None
    onset: datetime | None = None
    expires: datetime | None = None

    geometry: dict | None = None

    provenance: Provenance
    unavailable_fields: list[str] = Field(default_factory=list)
