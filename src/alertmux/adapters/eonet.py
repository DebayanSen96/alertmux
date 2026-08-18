"""NASA EONET adapter.

EONET (Earth Observatory Natural Event Tracker) publishes what NASA's
satellites have *observed* — it is not an alerting authority and issues
no warnings. That single fact drives every other decision below.

**No severity, urgency, certainty, expiry or onset.** These are all
warning-authority concepts: how bad an authority judges a hazard to be,
how fast it says to act, how confident it is, when its warning lapses.
NASA states none of that — it reports a location and a time a sensor
saw something. All five are always ``None`` and always in
``unavailable_fields``, unconditionally, the same discipline GDACS uses
for urgency/certainty/expires (see gdacs.py and DECISIONS.md's four
principles, principle 2 in particular). No severity is ever derived
from ``categories`` or from how many geometries an event has
accumulated — both were explicitly ruled out because either would
assert a judgement EONET never made.

**``headline`` is the real event ``title``.** Unlike SWIC, where a
"headline" would have had to be fabricated from an event code, EONET
supplies an actual human-readable string ("Wildfire Picture Rock, Lake,
Oregon"). Using it verbatim is a straightforward relay, not an
inference.

**``event`` comes from ``categories[0].title``.** Every event observed
via the live API (200 events, 18 Aug 2026) carries exactly one category
— ``Wildfires`` or ``Severe Storms``. EONET's schema permits more than
one, so the first category is taken deliberately rather than by
accident: category order is API-controlled and, empirically, the first
entry is the event's primary classification. An event with zero
categories has nothing to derive ``event`` from and raises, the same
discipline GDACS uses for an unmapped ``eventtype`` (DECISIONS.md D1).

**Geometry: the most recent point/polygon only, not the whole track.**
A tracked event (a cyclone, say) accumulates one geometry per
observation as it is followed — 36 for one live Aug-2026 storm. The
schema (`NormalisedAlert.geometry`) holds a single GeoJSON dict, the
same shape every other adapter fills, so the choice is between "one,
the latest" and "bundle all into a GeometryCollection". The latest
point is chosen: it is what "where is this event now" means for a
relay, it keeps the geometry field literally a Point/Polygon like every
other adapter instead of a shape unique to this source, and a consumer
that wants the full track already has it at ``provenance.raw_reference``
(the EONET event's own API link, which serves the complete geometry
history). ``geometries`` is defensively sorted by date before taking the
last entry — the live sample happened to arrive in chronological order,
but that is not documented as a guarantee. ``sent`` is set from that
same latest geometry's ``date``, so it is the one dated field on the
alert that describes exactly what ``geometry`` describes.

Identity: EONET's ``id`` field (e.g. ``EONET_22798``) is a stable,
server-assigned identifier for the event itself, unrelated to how many
geometries or categories it has accumulated — confirmed by fetching the
live endpoint twice, 18 Aug 2026: all 200 ids were identical, in the
same order, across both fetches. This is the opposite of the SWIC
defect in DECISIONS.md D2 (a synthetic id that changed on every
request); EONET's id needs no extra derivation.

Timestamps: EONET geometry ``date`` values are ISO-8601 with a ``Z``
suffix. Parsed the same way as every other ISO adapter (swic.py,
nws.py) — a result that comes back naive is refused rather than assumed
to be UTC.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "nasa-eonet"
USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

# Always sent explicitly rather than relying on the API's own default,
# per the task brief - large enough to capture the live event backlog
# (~200 open events observed 18 Aug 2026) without being unbounded.
DEFAULT_LIMIT = 200


def _iso_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an offset.

    Mirrors swic._iso_utc / nws._iso_utc exactly: a naive result would
    be silently reinterpreted as local time by astimezone(), which is
    worse than refusing outright.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"EONET {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_events_envelope(payload: dict) -> None:
    """A 200 that isn't the events envelope is an error, not zero alerts.

    Mirrors nws.py/gdacs.py's envelope check (DECISIONS.md's "Things we
    got wrong" note on a GeoServer error parsing as 200 empty output).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError(
            f"EONET response has no events list: {repr(payload)[:400]}"
        )


def _event_category(event: dict) -> str:
    """Derive `event` from categories[0].title.

    Every category observed on the live feed is `Wildfires` or `Severe
    Storms`, always exactly one per event. categories[0] is taken
    deliberately (see module docstring) rather than joining/choosing
    among multiples. An event with no categories at all has nothing to
    derive `event` from and must raise rather than default - same
    discipline as GDACS's unmapped eventtype (DECISIONS.md D1).
    """
    categories = event.get("categories")
    if not categories:
        raise ValueError(
            f"EONET event {event.get('id')!r} has no categories; "
            "cannot derive event"
        )
    title = categories[0].get("title")
    if not title:
        raise ValueError(
            f"EONET event {event.get('id')!r} has a category with no title"
        )
    return title


def _latest_geometry(event: dict) -> dict | None:
    """The most recent geometry only - see module docstring for why.

    Sorted defensively by `date` before taking the last entry; the live
    feed happens to return geometries in chronological order but that
    is not a documented guarantee.
    """
    geometries = event.get("geometries")
    if not geometries:
        return None
    latest = sorted(geometries, key=lambda g: g.get("date") or "")[-1]
    geom_type = latest.get("type")
    coordinates = latest.get("coordinates")
    if not geom_type or coordinates is None:
        return None
    return {"type": geom_type, "coordinates": coordinates}


def _latest_geometry_date(event: dict) -> str | None:
    """The date on the same geometry `_latest_geometry` picked, so
    `sent` always describes exactly what `geometry` describes."""
    geometries = event.get("geometries")
    if not geometries:
        return None
    latest = sorted(geometries, key=lambda g: g.get("date") or "")[-1]
    return latest.get("date")


class EonetAdapter:
    """Fetches and normalises NASA EONET observed events."""

    source_id = "nasa-eonet"
    URL = "https://eonet.gsfc.nasa.gov/api/v2.1/events"

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        limit: int = DEFAULT_LIMIT,
    ):
        self._client = client
        self._timeout = timeout
        self._limit = limit

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        _require_events_envelope(payload)

        alerts: list[NormalisedAlert] = []

        for event in payload["events"]:
            event_id = event.get("id")
            if not event_id:
                raise ValueError("EONET event has no id; cannot derive identity")

            title = event.get("title")
            if not title:
                raise ValueError(f"EONET event {event_id!r} has no title")

            event_type = _event_category(event)

            # EONET's `sources` lists upstream providers (IRWIN, JTWC,
            # etc.) that fed this observation. There is no field on
            # NormalisedAlert shaped for a list of contributing sources
            # - source_severity/source_urgency/source_certainty are
            # CAP source-native *values*, not provider names, and
            # stuffing provider ids into one would misuse the field and
            # overstate what was preserved. `sources` is left out
            # structurally; a consumer that needs it can follow
            # `provenance.raw_reference` (the event's own EONET API
            # page), which lists the same providers itself.
            fields = {
                "headline": title,
                "description": event.get("description") or None,
                "area_description": None,
                # Observations, not warnings - none of these concepts
                # apply to a satellite sighting. Never derived from
                # categories or geometry count. See module docstring.
                "severity": None,
                "urgency": None,
                "certainty": None,
                "source_severity": None,
                "source_urgency": None,
                "source_certainty": None,
                "sent": _iso_utc(_latest_geometry_date(event), "geometries[].date"),
                "onset": None,
                "expires": None,
                "geometry": _latest_geometry(event),
            }

            # severity/urgency/certainty/expires/onset are structurally
            # absent from every EONET event - the source is an
            # observation feed, not a warning feed, so no record could
            # ever carry them. Unioned with whatever else
            # came back None on this particular event, same
            # derived-not-appended construction as gdacs.py/nws.py.
            structural = ("severity", "urgency", "certainty", "expires", "onset")
            unavailable = sorted(
                set(structural) | {k for k, v in fields.items() if v is None}
            )

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{event_id}",
                    event=event_type,
                    provenance=Provenance(
                        authority=AUTHORITY,
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=event.get("link"),
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
            response = client.get(self.URL, params={"limit": self._limit})
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
