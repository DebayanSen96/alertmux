"""NOAA / National Weather Service adapter.

One endpoint, the whole United States. Unlike SWIC and USGS this feed
supplies onset/expires and headline/description directly — no separate
CAP file fetch is needed. Three quirks make this adapter different from
the other two:

1. ``?limit=N`` returns HTTP 400. The unparameterised endpoint is the
   only one that works; do not add query parameters here.
2. The live feed carries a persistent ``KEEPALIVE`` record with
   ``status: "Test"``, ``event: "Test Message"`` (observed 1 of 295).
   Only ``status == "Actual"`` may be relayed as a hazard warning —
   relaying a test message as real would violate the project's central
   promise. ``messageType == "Cancel"`` is excluded for the same reason,
   should it ever appear. Both are filtered in ``parse()``, silently:
   this is deliberate exclusion of traffic the source itself marks as
   not-a-warning, not the malformed-record case D3 in DECISIONS.md
   covers.
3. ``severity``/``urgency``/``certainty`` arrive as the named CAP values
   directly (``Minor``/``Moderate``/``Severe``/``Extreme``/``Unknown``,
   etc.) — there is no integer code to map, unlike SWIC. ``"Unknown"``
   is itself a value the source stated, not a missing field, so it is
   never treated as None.

Geometry: only ~48 of 295 live features carry a polygon; the remaining
majority are zone-referenced via ``affectedZones``/``geocode`` instead.
A null geometry here means "referenced by zone", not "unknown" — see
the comment in ``parse()`` for why it still lands in
``unavailable_fields`` regardless.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "us-noaa"
USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"


def _iso_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an offset.

    A naive value would be interpreted as server-local time by
    astimezone(), silently shifting a hazard timestamp by the deploying
    machine's UTC offset. Refuse instead of guessing. Mirrors
    swic._iso_utc exactly.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"NWS {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_feature_collection(payload: dict) -> None:
    """A 200 that is not a FeatureCollection is an error, not a quiet day.

    api.weather.gov can answer a bad request with a ProblemDetail JSON
    body at HTTP 200-adjacent statuses; guard the shape explicitly
    rather than reading `features` and getting zero alerts by accident.
    """
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
            or "features" not in payload:
        raise ValueError(
            "NWS response is not a GeoJSON FeatureCollection: "
            f"{repr(payload)[:400]}"
        )


class NwsAdapter:
    """Fetches and normalises NOAA/NWS active alerts for the US."""

    source_id = "us-noaa"
    URL = "https://api.weather.gov/alerts/active"

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
                    f"NWS feature {feature.get('id')!r} has no properties"
                )

            # Only relay real hazard warnings. The KEEPALIVE test record
            # (status: "Test", event: "Test Message") and any Cancel
            # message are excluded here, silently — this is the source
            # telling us "not a warning", not a malformed record.
            if props.get("status") != "Actual":
                continue
            if props.get("messageType") == "Cancel":
                continue

            event = props.get("event")
            if not event:
                raise ValueError(
                    f"NWS feature {feature.get('id')!r} has no event"
                )

            identifier = props.get("id")
            if not identifier:
                raise ValueError(
                    f"NWS feature {feature.get('id')!r} has no properties.id; "
                    "cannot derive a stable identity"
                )

            fields = {
                "headline": props.get("headline"),
                "description": props.get("description"),
                "area_description": props.get("areaDesc"),
                # Named CAP values straight from the source - no integer
                # code table exists or is needed for this feed.
                "severity": props.get("severity"),
                "urgency": props.get("urgency"),
                "certainty": props.get("certainty"),
                # Raw source-native values, preserved exactly like every
                # other adapter, even though here they equal the named
                # fields above (no code mapping to diverge from).
                "source_severity": props.get("severity"),
                "source_urgency": props.get("urgency"),
                "source_certainty": props.get("certainty"),
                "sent": _iso_utc(props.get("sent"), "sent"),
                "onset": _iso_utc(props.get("onset"), "onset"),
                "expires": _iso_utc(props.get("expires"), "expires"),
                # Zone-referenced alerts (the majority) carry no polygon
                # at all - affectedZones/geocode stand in for it. That is
                # a different situation from "the source didn't know",
                # but the schema has no third state, so it still lands
                # in unavailable_fields below like any other None.
                "geometry": feature.get("geometry"),
            }

            # Every Actual record observed on this feed supplies
            # headline, description, sent, onset and expires - nothing
            # is structurally absent the way onset/expires are for SWIC's
            # list view or severity is for USGS. unavailable_fields is
            # therefore built purely from what came back None on this
            # particular record (chiefly geometry).
            structural: tuple[str, ...] = ()
            unavailable = sorted(
                set(structural) | {k for k, v in fields.items() if v is None}
            )

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{identifier}",
                    event=event,
                    provenance=Provenance(
                        authority=AUTHORITY,
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=props.get("@id") or identifier,
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
            # No query parameters - api.weather.gov returns HTTP 400 for
            # ?limit=N (and, empirically, for other params tried against
            # this endpoint). The unparameterised URL is the contract.
            response = client.get(self.URL)
            response.raise_for_status()
            payload = response.json()
            alerts = self.parse(payload, retrieved_at)
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
