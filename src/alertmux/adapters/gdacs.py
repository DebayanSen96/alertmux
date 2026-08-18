"""GDACS (Global Disaster Alert and Coordination System) adapter.

The first RSS/XML adapter in alertmux; every other one parses JSON. Uses
only ``xml.etree.ElementTree`` from the standard library — no new
dependency. The feed carries a ``gdacs:`` namespace (and a handful of
others: ``geo:``, ``georss:``, ``dc:``, ``atom:``, ``glide:``); every tag
is resolved through the ``NS`` map below rather than string-hacked.

Two things make this source different from every JSON adapter already in
the codebase:

1. **``gdacs:alertlevel`` (Green/Orange/Red) is an expected-humanitarian-
   IMPACT score, not a CAP severity judgement.** GDACS never states how
   severe the hazard itself is — Green/Orange/Red describe how many
   people and how much infrastructure are expected to be affected, which
   is a materially different question from "how bad is this storm/quake."
   Mapping it onto CAP's Minor/Moderate/Severe/Extreme would assert a
   severity GDACS never gave — the exact failure alertmux exists to
   prevent (see the four principles in ``DECISIONS.md``). It stays in
   ``source_severity`` only; ``severity`` is always ``None`` and always
   in ``unavailable_fields``.
2. **``event`` comes from ``gdacs:eventtype`` through an explicit,
   deliberately incomplete table** (``EVENT_TYPES`` below), same
   discipline as SWIC's severity/urgency/certainty codes (DECISIONS.md
   D1): a code never observed raises rather than being defaulted or
   passed through raw.

Identity: GDACS items carry ``gdacs:eventid`` (the disaster) and
``gdacs:episodeid`` (the specific update/version of that disaster). The
RSS ``<guid>`` observed on this feed is only ``{eventtype}{eventid}``
(e.g. ``EQ1559738``) — stable, but coarser than an alertmux id needs to
be, since the same event gets multiple episodes as it evolves. The id is
built from eventid *and* episodeid instead, mirroring D2's rule for SWIC:
derive identity from something content-addressed, never from a value
that isn't guaranteed to track the actual episode. Verified stable by
fetching the live feed twice, 18 Aug 2026: all 369 (eventid, episodeid)
pairs were identical across both fetches.

GDACS supplies no ``urgency``, no ``certainty``, and no ``expires`` at
all — not even as unmapped source-native values. All three are
structurally unavailable on every alert, alongside ``severity``.

Timestamps (``pubDate``, ``gdacs:fromdate``) are RFC-822, not ISO, and
are parsed with ``email.utils.parsedate_to_datetime``. GDACS always sends
an explicit ``GMT``/offset, but a result that comes back naive is
refused rather than assumed to be UTC or local time.

Geometry: GDACS gives a point (``geo:Point``) and a bounding box
(``gdacs:bbox``, ``lonmin lonmax latmin latmax``). The point, where
present, is used as-is — it is the most precise thing the source states.
Where only the bbox is present, it is mapped to a rectangular Polygon,
which is a faithful (not approximated) reading of what a bounding box
means. Anything not literally expressible this way is left as ``None``
and marked unavailable rather than guessed at.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "gdacs"
USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "georss": "http://www.georss.org/georss",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
    "glide": "http://glidenumber.net",
}

# Verified against the live feed, 18 Aug 2026: EQ 16, WF 319 (largest
# family by far), FL 19, DR 12, TC 3. VO (volcano) was in the v0.1 design
# notes but not observed in this sample; kept in the table because GDACS
# documents it as a first-class event type, same as every other entry
# here. An eventtype outside this table has never been observed and is
# refused rather than passed through unmapped or defaulted - same
# discipline as the SWIC codes in DECISIONS.md D1.
EVENT_TYPES = {
    "EQ": "Earthquake",
    "TC": "Tropical Cyclone",
    "FL": "Flood",
    "VO": "Volcano",
    "DR": "Drought",
    "WF": "Wildfire",
}


def _text(elem: ET.Element, path: str) -> str | None:
    """Find a (possibly namespaced) child and return its stripped text.

    Treats a missing element and an empty/self-closing one
    (``<gdacs:country />``) the same way - both are "not supplied".
    """
    child = elem.find(path, NS)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _rfc822_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an RSS RFC-822 date and require it to carry an offset.

    RSS dates are RFC-822, not ISO - ``fromisoformat`` cannot read them.
    ``parsedate_to_datetime`` returns a naive datetime when the string
    carries no zone information. GDACS has always sent an explicit GMT
    on every item observed, but refuse rather than assume UTC (or,
    worse, local time) if that ever changes.
    """
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        raise ValueError(
            f"GDACS {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_rss_channel(root: ET.Element) -> ET.Element:
    """A 200 that isn't parseable RSS is an error, not zero alerts.

    Mirrors nws.py/swic.py's envelope check: reading zero <item>s out of
    something that was never a feed in the first place would report "no
    hazards anywhere" as a healthy result.
    """
    if root.tag != "rss":
        raise ValueError(f"GDACS response root is {root.tag!r}, not <rss>")
    channel = root.find("channel")
    if channel is None:
        raise ValueError("GDACS response has no <channel>")
    return channel


def _geometry(item: ET.Element) -> dict | None:
    """Map only what GDACS genuinely states, without approximating.

    Prefers the point (most precise) and falls back to the bbox as a
    Polygon (still exact - a bbox unambiguously denotes a rectangle).
    Anything that cannot be read this literally is left None.
    """
    lat = _text(item, "geo:Point/geo:lat")
    lon = _text(item, "geo:Point/geo:long")
    if lat is not None and lon is not None:
        try:
            return {"type": "Point", "coordinates": [float(lon), float(lat)]}
        except ValueError:
            pass

    bbox = _text(item, "gdacs:bbox")
    if bbox:
        parts = bbox.split()
        if len(parts) == 4:
            try:
                lonmin, lonmax, latmin, latmax = (float(p) for p in parts)
            except ValueError:
                return None
            return {
                "type": "Polygon",
                "coordinates": [
                    [
                        [lonmin, latmin],
                        [lonmax, latmin],
                        [lonmax, latmax],
                        [lonmin, latmax],
                        [lonmin, latmin],
                    ]
                ],
            }
    return None


class GdacsAdapter:
    """Fetches and normalises GDACS disaster alerts."""

    source_id = "gdacs"
    URL = "https://www.gdacs.org/xml/rss.xml"

    # GDACS structurally never supplies urgency, certainty or expires,
    # and severity is deliberately never derived from alertlevel (see
    # module docstring) -- all four are always unavailable regardless
    # of what a particular record carries. A class attribute so
    # /sources can report it without running a fetch.
    STRUCTURAL_GAPS: tuple[str, ...] = ("severity", "urgency", "certainty", "expires")

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0):
        self._client = client
        self._timeout = timeout

    def parse(self, payload: bytes | str, retrieved_at: datetime) -> list[NormalisedAlert]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ValueError(f"GDACS response is not parseable XML: {exc}") from exc

        channel = _require_rss_channel(root)

        alerts: list[NormalisedAlert] = []

        for item in channel.findall("item"):
            eventtype = _text(item, "gdacs:eventtype")
            if not eventtype:
                raise ValueError("GDACS item has no gdacs:eventtype")
            event = EVENT_TYPES.get(eventtype)
            if event is None:
                raise ValueError(
                    f"GDACS eventtype {eventtype!r} is not a known code; "
                    "refusing to pass it through unmapped or default it"
                )

            eventid = _text(item, "gdacs:eventid")
            episodeid = _text(item, "gdacs:episodeid")
            if not eventid or not episodeid:
                raise ValueError(
                    "GDACS item has no gdacs:eventid/gdacs:episodeid; "
                    "cannot derive a stable identity"
                )

            fields = {
                "headline": _text(item, "title"),
                "description": _text(item, "description"),
                "area_description": _text(item, "gdacs:country"),
                # gdacs:alertlevel (Green/Orange/Red) is an expected
                # HUMANITARIAN IMPACT score, not a statement of hazard
                # severity - GDACS never says how severe the earthquake,
                # flood, etc. itself is. Mapping Green/Orange/Red onto
                # CAP's Minor/Moderate/Severe/Extreme would assert a
                # severity the source never gave. Kept in
                # source_severity only; see module docstring and
                # DECISIONS.md.
                "severity": None,
                "urgency": None,
                "certainty": None,
                "source_severity": _text(item, "gdacs:alertlevel"),
                "source_urgency": None,
                "source_certainty": None,
                "sent": _rfc822_utc(_text(item, "pubDate"), "pubDate"),
                "onset": _rfc822_utc(_text(item, "gdacs:fromdate"), "fromdate"),
                "expires": None,
                "geometry": _geometry(item),
            }

            # Unioned with whatever else came back None on this record,
            # same derived-not-appended construction as nws.py/swic.py.
            unavailable = sorted(
                set(self.STRUCTURAL_GAPS) | {k for k, v in fields.items() if v is None}
            )

            # "Raw reference" is the CAP file GDACS generated for this
            # episode where one exists - that is the most literal "raw"
            # document behind the alert. The human-readable report page
            # (<link>) is the fallback when no CAP URL is given.
            raw_reference = _text(item, "gdacs:cap") or _text(item, "link")

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{eventtype}:{eventid}:{episodeid}",
                    event=event,
                    provenance=Provenance(
                        authority=AUTHORITY,
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=raw_reference,
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
            alerts = self.parse(response.content, retrieved_at)
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
