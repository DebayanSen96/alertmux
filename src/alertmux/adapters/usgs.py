"""USGS earthquake feed adapter.

USGS publishes observed earthquakes, not forecast warnings, so it has no
expiry and no CAP severity. Both are recorded as unavailable rather than
invented. The `alert` field (PAGER level: green/yellow/orange/red) is kept
as a source-native value only — it is not CAP severity and must not be
mapped to one.

The feed also carries non-earthquake events (`quarry blast`, `explosion`,
`ice quake`, `sonic boom`, `mining explosion`), so `type` is never
defaulted to "earthquake" — a missing type is a malformed feature.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "us-usgs"
USER_AGENT = "alertmux/0.1 (+https://github.com/ADJ-HUB1/alertmux)"


def _epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _require_feature_collection(payload: dict) -> None:
    """A 200 that is not a FeatureCollection is an error, not a quiet hour."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
            or "features" not in payload:
        raise ValueError(
            "USGS response is not a GeoJSON FeatureCollection: "
            f"{repr(payload)[:400]}"
        )


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
        _require_feature_collection(payload)

        alerts: list[NormalisedAlert] = []

        for feature in payload["features"]:
            props = feature.get("properties")
            if not props:
                raise ValueError(
                    f"USGS feature {feature.get('id')!r} has no properties"
                )

            feature_id = feature.get("id")
            if not feature_id:
                raise ValueError("USGS feature has no id")

            # The feed carries quarry blasts and explosions too. Calling
            # one of those an earthquake would be an invented fact.
            event = props.get("type")
            if not event:
                raise ValueError(
                    f"USGS feature {feature_id!r} has no type"
                )

            # PAGER alert level, not CAP severity. Kept source-native only.
            pager = props.get("alert")

            fields = {
                "headline": props.get("title"),
                # USGS has no description field. None means "the source
                # has none" here exactly as it does for SWIC.
                "description": None,
                "area_description": props.get("place"),
                # USGS states no CAP severity/urgency/certainty at all.
                "severity": None,
                "urgency": None,
                "certainty": None,
                "source_severity": str(pager) if pager else None,
                "source_urgency": None,
                "source_certainty": None,
                "sent": _epoch_ms(props.get("time")),
                # Observed events: these concepts do not apply.
                "onset": None,
                "expires": None,
                "geometry": feature.get("geometry"),
            }

            structural = ("urgency", "certainty", "onset", "expires", "description")
            unavailable = sorted(
                set(structural) | {k for k, v in fields.items() if v is None}
            )

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{feature_id}",
                    event=event,
                    provenance=Provenance(
                        authority=AUTHORITY,
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=props.get("url"),
                    ),
                    unavailable_fields=unavailable,
                    **fields,
                )
            )

        return alerts

    def fetch(self) -> FetchResult:
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(
            timeout=self._timeout, headers={"User-Agent": USER_AGENT}
        )

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
