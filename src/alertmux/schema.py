"""The contract every adapter satisfies.

A field a source does not supply is None. Values are never inferred,
defaulted, or guessed — fabricating a severity a source did not state
is the exact failure this project exists to avoid.

A None field is always explained by exactly one of two lists, never
both (see `NormalisedAlert`'s docstring for the distinction and
DECISIONS.md for the entry that introduced it).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# Stated in the body of every API response, not only in the OpenAPI
# description, so any downstream consumer (MCP server, notifier)
# inherits it automatically.
DISCLAIMER = (
    "Relayed from official alerting authorities. Not a substitute for "
    "official warnings from the issuing authority."
)


class Provenance(BaseModel):
    """Where an alert came from. Mandatory on every alert."""

    authority: str
    source_id: str
    source_url: str
    retrieved_at: datetime
    raw_reference: str | None = None


class NormalisedAlert(BaseModel):
    """One hazard alert, normalised across sources.

    Every optional field that comes back None is named in exactly one
    of two lists — never both, never neither:

    * `unavailable_fields` — the source supplied nothing for this
      field. There is no value to relay, mapped or otherwise.
    * `unmapped_fields` — the source supplied a value, but it was
      declined to translate because the mapping is unverified (e.g. a
      SWIC severity code outside the tables confirmed in
      DECISIONS.md D1) or is deliberately never attempted (e.g.
      GDACS's `alertlevel`, a tsunami bulletin category, or the USGS
      PAGER level — none of which are CAP severity). The raw value the
      source sent is preserved in the corresponding `source_*` field
      either way.

    A field that is structurally absent from a source's feed (it never
    supplies the concept at all, on any record) always lands in
    `unavailable_fields`, even when it happens to share a name with a
    field another source declines to map — structural absence is not a
    refusal to map.
    """

    id: str
    event: str

    headline: str | None = None
    description: str | None = None
    instruction: str | None = None
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
    unmapped_fields: list[str] = Field(default_factory=list)
