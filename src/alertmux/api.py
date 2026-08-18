"""HTTP presentation of the query layer.

This service relays alerts published by official authorities. It never
originates, edits, or rewords hazard content, and it is not a substitute
for official warnings.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from alertmux.adapters import default_adapters
from alertmux.query import AlertsResponse, collect

DISCLAIMER = (
    "Relayed from official alerting authorities. Not a substitute for "
    "official warnings from the issuing authority."
)

app = FastAPI(
    title="alertmux",
    description=DISCLAIMER,
    version="0.1.0",
)


def get_adapters():
    """Overridable in tests via app.dependency_overrides."""
    return default_adapters()


@app.get("/alerts")
def alerts(authority: str | None = None, adapters=Depends(get_adapters)) -> AlertsResponse:
    response = collect(adapters)
    if authority:
        response.alerts = [
            a for a in response.alerts if a.provenance.authority == authority
        ]
    return response


@app.get("/health")
def health(adapters=Depends(get_adapters)) -> dict:
    response = collect(adapters)
    return {
        "ok": not response.partial,
        "checked_at": response.retrieved_at.isoformat(),
        "sources": [s.model_dump() for s in response.sources],
    }
