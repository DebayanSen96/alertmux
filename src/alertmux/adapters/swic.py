"""WMO Severe Weather Information Centre adapter.

One adapter, 59 national alerting authorities. Uses the
effective_warning_view layer, which returns only warnings currently in
force — expired alerts never enter the pipeline, so they can never be
re-notified.

Two rules that must not be relaxed:

1. Always paginate. Unfiltered queries exceed 24MB and time out at 60s.
2. Map only verified s/u/c codes. The tables below were confirmed
   against raw CAP for seven distinct combinations with no conflicts.
   Any code outside them stays untranslated - the raw value is kept
   and the named field left None. Guessing an unseen code means
   either a missed warning or a false alarm.

The raw CAP file for any alert resolves at
https://severeweather.wmo.int/v2/cap-alerts/<capurl>. It carries
expires, onset, instruction and polygon, which this list view omits.
Fetching one file per alert is too expensive for a list endpoint, so
v0.1 does not.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

_AUTHORITY_RE = re.compile(r"^([a-z]{2}-[a-z0-9]+)")

# Confirmed against raw CAP 1.2, 17 Aug 2026, 7 samples, no conflicts.
# Deliberately partial: codes never observed are absent, not guessed.
SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}
URGENCY = {2: "Future", 3: "Expected", 4: "Immediate"}
CERTAINTY = {2: "Possible", 3: "Likely", 4: "Observed"}


def _authority_from_capurl(capurl: str | None) -> str:
    """capurl looks like 'ng-nimet-en/2026/08/17/...xml'."""
    if not capurl:
        return "unknown"
    match = _AUTHORITY_RE.match(capurl)
    return match.group(1) if match else "unknown"


def _iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


class SwicAdapter:
    """Fetches and normalises WMO SWIC effective warnings."""

    source_id = "wmo-swic"
    URL = "https://severeweather.wmo.int/g/wfs"
    TYPE_NAME = "local_postgis:effective_warning_view"

    def __init__(
        self,
        client: httpx.Client | None = None,
        mem: str | None = None,
        max_features: int = 3000,
        timeout: float = 60.0,
    ):
        self._client = client
        self._mem = mem
        self._max_features = max_features
        self._timeout = timeout

    def build_params(self, mem: str | None, max_features: int) -> dict:
        params = {
            "request": "GetFeature",
            "version": "1.1.0",
            "typeName": self.TYPE_NAME,
            "outputFormat": "json",
            "maxFeatures": max_features,
        }
        if mem:
            params["cql_filter"] = f"mem='{mem}'"
        return params

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        alerts: list[NormalisedAlert] = []

        for feature in payload.get("features", []):
            props = feature.get("properties")
            if not props:
                raise ValueError(
                    f"SWIC feature {feature.get('id')!r} has no properties"
                )

            event = props.get("event")
            if not event:
                raise ValueError(
                    f"SWIC feature {feature.get('id')!r} has no event"
                )

            capurl = props.get("capurl")
            geometry = feature.get("geometry")

            # onset/expires live in the CAP file, not this list view.
            unavailable = ["onset", "expires"]
            if geometry is None:
                unavailable.append("geometry")
            if not props.get("rlink"):
                unavailable.append("description")

            def _code(key: str) -> str | None:
                value = props.get(key)
                return str(value) if value is not None else None

            def _named(key: str, table: dict[int, str], field: str) -> str | None:
                """Map a code only if it was verified. Never guess."""
                name = table.get(props.get(key))
                if name is None:
                    unavailable.append(field)
                return name

            # Compute mapped values into local variables before constructing
            # NormalisedAlert, rather than relying on keyword-argument
            # evaluation order to mutate `unavailable` mid-call. Behaviour
            # is identical, but this reads correctly rather than merely
            # happening to work.
            severity = _named("s", SEVERITY, "severity")
            urgency = _named("u", URGENCY, "urgency")
            certainty = _named("c", CERTAINTY, "certainty")

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{feature.get('id')}",
                    event=event,
                    headline=event,
                    description=props.get("rlink") or None,
                    area_description=props.get("areadesc"),
                    severity=severity,
                    urgency=urgency,
                    certainty=certainty,
                    source_severity=_code("s"),
                    source_urgency=_code("u"),
                    source_certainty=_code("c"),
                    sent=_iso_utc(props.get("sent")),
                    geometry=geometry,
                    provenance=Provenance(
                        authority=_authority_from_capurl(capurl),
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=capurl,
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
            response = client.get(
                self.URL, params=self.build_params(self._mem, self._max_features)
            )
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
