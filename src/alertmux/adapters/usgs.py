"""USGS earthquake feed adapter.

USGS publishes observed earthquakes, not forecast warnings, so it has no
expiry and no CAP severity. Both are recorded as unavailable rather than
invented. The `alert` field (PAGER level: green/yellow/orange/red) is kept
as a source-native value only — it is not CAP severity and must not be
mapped to one.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "us-usgs"


def _epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


class UsgsAdapter:
    """Fetches and normalises the USGS all-hour earthquake summary."""

    source_id = "usgs"
    URL = (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
        "summary/all_hour.geojson"
    )

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0):
        self._client = client
        self._timeout = timeout

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        alerts: list[NormalisedAlert] = []

        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            unavailable: list[str] = []

            # PAGER alert level, not CAP severity. Kept source-native only.
            pager = props.get("alert")
            source_severity = str(pager) if pager else None
            if source_severity is None:
                unavailable.append("severity")

            # USGS reports observed events; these concepts do not apply.
            unavailable.extend(["urgency", "certainty", "onset", "expires"])

            description = props.get("title")
            if description is None:
                unavailable.append("description")

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{feature.get('id')}",
                    event=props.get("type") or "earthquake",
                    headline=props.get("title"),
                    description=description,
                    area_description=props.get("place"),
                    source_severity=source_severity,
                    sent=_epoch_ms(props.get("time")),
                    geometry=feature.get("geometry"),
                    provenance=Provenance(
                        authority=AUTHORITY,
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=props.get("url"),
                    ),
                    unavailable_fields=unavailable,
                )
            )

        return alerts

    def fetch(self) -> FetchResult:
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(timeout=self._timeout)

        try:
            response = client.get(self.URL)
            response.raise_for_status()
            alerts = self.parse(response.json(), retrieved_at)
        except Exception as exc:  # noqa: BLE001 - adapters never raise outward
            return FetchResult(
                source_id=self.source_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                retrieved_at=retrieved_at,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if self._client is None:
                client.close()

        return FetchResult(
            source_id=self.source_id,
            ok=True,
            alerts=alerts,
            retrieved_at=retrieved_at,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
