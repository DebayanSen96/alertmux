"""tsunami.gov adapter (NTWC + PTWC).

Two Atom feeds, one per US tsunami warning centre, both keyless:

* ``https://www.tsunami.gov/events/xml/PAAQAtom.xml`` — National Tsunami
  Warning Center (Palmer, AK). Covers Alaska, Canada and the US East/
  Gulf coasts.
* ``https://www.tsunami.gov/events/xml/PHEBAtom.xml`` — Pacific Tsunami
  Warning Center (Honolulu, HI). Covers Hawaii and the wider Pacific.

**Atom, not RSS.** ``<entry>``, not ``<item>``, under the default
namespace ``http://www.w3.org/2005/Atom``; geometry via ``geo:lat`` /
``geo:long`` (``http://www.w3.org/2003/01/geo/wgs84_pos#``). Both fetched
and merged by ``fetch()``; ``fetch()`` is the only place that combines
them, so the two centres can fail independently (D3 does not apply
across centres — a broken NTWC feed must never take PTWC's alerts down
with it, mirroring how one bad record never takes a whole feed down).

**The one rule this adapter exists to enforce: a Tsunami Information
Statement is NOT a warning.** tsunami.gov's own bulletin hierarchy,
ascending:

    Information Statement  <  Watch  <  Advisory  <  Warning

An Information Statement typically means an earthquake occurred and
*no destructive tsunami is expected* — it is the routine, most common
case, not an escalation. Every entry observed live (both feeds, 17-18
Aug 2026) was an Information Statement; this project has never seen a
live Watch/Advisory/Warning to verify against. Consequently:

1. **The bulletin category is never mapped to CAP ``severity``.** Same
   discipline as ``gdacs:alertlevel`` (see ``gdacs.py`` and
   DECISIONS.md): ``severity`` is always ``None`` and always in
   ``unavailable_fields``. The raw category goes in ``source_severity``
   only.
2. **``event`` carries the level verbatim, mapped through an explicit
   table** (``CATEGORY_TO_EVENT`` below) from the per-entry ``Category:``
   field embedded in the entry's ``<summary>`` — the same
   never-guess-an-unseen-code discipline as GDACS's ``EVENT_TYPES``
   (DECISIONS.md D1). Only ``"Information"`` is verified against a live
   feed; ``"Watch"``, ``"Advisory"`` and ``"Warning"`` are included on
   the strength of tsunami.gov's own published bulletin terminology
   (mirrored in every "Definition:" text these bulletins carry) but were
   not observed live at the time this adapter was built — same caveat
   GDACS's ``EVENT_TYPES`` carries for ``VO``. A category outside this
   table is refused rather than passed through unmapped or defaulted,
   because guessing wrong here is exactly the failure mode this source
   exists to prevent.
3. If an entry's ``Category:`` cannot be found or does not match the
   table, that entry is quarantined (D3) — never defaulted to
   "Information" or any other level.

Why the category comes from the entry, not the feed. The *feed*-level
``<title>`` also states the level in full ("Tsunami Information
Statement Number 1"), but it describes only the feed's current/latest
bulletin, not necessarily every ``<entry>`` inside it. Reading level
per-entry is the only choice that stays correct if a feed is ever
observed carrying more than the single entry seen in this research
(the entry's ``<title>`` is the affected region, e.g. "100 miles SW of
Kodiak City, Alaska" — never the level, so it cannot substitute).

Identity: each ``<entry><id>`` is a ``urn:uuid:...`` verified stable
across two independent fetches of both feeds, seconds apart, 18 Aug
2026 (byte-identical responses, including the uuid). The alert id is
``tsunami-gov:{authority}:{entry-id}``, namespaced by centre so NTWC and
PTWC entry ids (drawn from different uuid spaces) can never collide.

Timestamps: entry ``<updated>`` is ISO-8601 with an explicit ``Z``
(UTC) on every entry observed; parsed and required to carry an offset,
same discipline as every other adapter — never assumed to be UTC if
that ever changes.

Structurally never supplied by this feed: ``urgency``, ``certainty``,
``expires``, ``instruction``, and a distinct ``onset`` (the only
timestamp is the bulletin's issue/update time, used as ``sent``).
``severity`` is never supplied either, in the sense that means: it is
never derived, on principle, even though the source states a category —
see point 1 above.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import RECORD_SAMPLE_CAP, FetchResult, RecordQuarantine
from alertmux.schema import NormalisedAlert, Provenance

USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

ATOM_NS = "http://www.w3.org/2005/Atom"
GEO_NS = "http://www.w3.org/2003/01/geo/wgs84_pos#"
NS = {"atom": ATOM_NS, "geo": GEO_NS}

_FEED_TAG = f"{{{ATOM_NS}}}feed"

# Verified live, 17-18 Aug 2026: both NTWC and PTWC entries carried
# "Category: Information". Watch/Advisory/Warning are tsunami.gov's own
# documented bulletin categories (see module docstring) but unobserved
# live at build time -- kept in the table on the same documentation-only
# basis as GDACS's "VO" (gdacs.py EVENT_TYPES), not as a guess. A
# category outside this table is refused rather than defaulted (D1/D3
# discipline) -- getting this wrong in either direction (silently
# dropping a real Warning, or inventing "Warning" for something else)
# is the single most dangerous failure mode this adapter exists to
# avoid.
CATEGORY_TO_EVENT = {
    "Information": "Tsunami Information Statement",
    "Watch": "Tsunami Watch",
    "Advisory": "Tsunami Advisory",
    "Warning": "Tsunami Warning",
}

_CATEGORY_RE = re.compile(r"Category:\s*([A-Za-z]+)")


def _text(elem: ET.Element | None, path: str) -> str | None:
    """Find a (possibly namespaced) child and return its stripped text.

    Treats a missing element and an empty one the same way — both are
    "not supplied". Mirrors gdacs.py's ``_text`` exactly.
    """
    if elem is None:
        return None
    child = elem.find(path, NS)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _iso_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an offset.

    Every ``<updated>`` observed on both live feeds carries an explicit
    ``Z``. A naive value is refused rather than assumed to be UTC or
    local time — mirrors nws.py/swic.py exactly.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"tsunami.gov {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_atom_feed(root: ET.Element) -> None:
    """A 200 that isn't parseable Atom is an error, not zero tsunamis.

    Mirrors gdacs.py's ``_require_rss_channel`` / nws.py's
    ``_require_feature_collection``: reading zero ``<entry>``s out of
    something that was never an Atom feed would report "no tsunami
    activity" as a healthy result instead of a broken source.
    """
    if root.tag != _FEED_TAG:
        raise ValueError(
            f"tsunami.gov response root is {root.tag!r}, not an Atom <feed>"
        )


def _category(entry: ET.Element) -> str | None:
    """Extract the bulletin category ("Information"/"Watch"/"Advisory"/
    "Warning") from the entry's ``<summary type="xhtml">`` block.

    The summary is a nested XHTML ``<div>`` (its own namespace, but
    still well-formed XML nested inside the Atom document, so ET parses
    it along with everything else) containing a
    ``<strong>Category:</strong> Information`` line among several
    others. All text within the summary is concatenated and searched
    with a regex rather than parsed structurally, because the XHTML
    markup around the label (``<strong>`` vs plain text, trailing
    ``<br/>``) is presentational and not itself part of the data model.
    """
    summary = entry.find("atom:summary", NS)
    if summary is None:
        return None
    text = " ".join(summary.itertext())
    match = _CATEGORY_RE.search(text)
    if match is None:
        return None
    return match.group(1)


def _description(entry: ET.Element) -> str | None:
    """Full text of the entry's ``<summary>`` block, whitespace-
    collapsed. This is the same element ``_category`` reads its
    ``Category:`` line from; kept separate because one is a targeted
    regex extraction and the other relays the whole block verbatim.
    """
    summary = entry.find("atom:summary", NS)
    if summary is None:
        return None
    text = " ".join("".join(summary.itertext()).split())
    return text or None


def _geometry(entry: ET.Element) -> dict | None:
    lat = _text(entry, "geo:lat")
    lon = _text(entry, "geo:long")
    if lat is None or lon is None:
        return None
    try:
        return {"type": "Point", "coordinates": [float(lon), float(lat)]}
    except ValueError:
        return None


def _raw_reference(entry: ET.Element) -> str | None:
    """The CapXML link is the most literal "raw" document behind the
    bulletin (mirrors gdacs:cap in gdacs.py); the plain-text bulletin
    link is the fallback when no CapXML link is present.
    """
    cap_href = None
    bulletin_href = None
    for link in entry.findall("atom:link", NS):
        rel = link.get("rel")
        title = link.get("title")
        href = link.get("href")
        if href is None:
            continue
        href = href.strip()
        if rel == "related" and title == "CapXML document":
            cap_href = href
        elif rel == "alternate" and title == "Bulletin":
            bulletin_href = href
    return cap_href or bulletin_href


class _CentreFeed:
    """One tsunami.gov centre feed — NTWC or PTWC."""

    def __init__(self, authority: str, url: str):
        self.authority = authority
        self.url = url


class TsunamiAdapter:
    """Fetches and normalises US tsunami bulletins from NTWC + PTWC."""

    source_id = "tsunami-gov"

    NTWC_URL = "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
    PTWC_URL = "https://www.tsunami.gov/events/xml/PHEBAtom.xml"

    _CENTRES = (
        _CentreFeed("us-ntwc", NTWC_URL),
        _CentreFeed("us-ptwc", PTWC_URL),
    )

    # This feed structurally never supplies urgency, certainty, expires
    # or instruction on any entry -- and severity is deliberately never
    # derived from the bulletin category (see module docstring) even
    # though the source states one. onset is likewise never distinct
    # from the bulletin's issue time (used as `sent`); there is no
    # separate onset concept on this feed. A class attribute so
    # /sources can report it without running a fetch.
    STRUCTURAL_GAPS: tuple[str, ...] = (
        "severity", "urgency", "certainty", "expires", "onset", "instruction",
    )

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0):
        self._client = client
        self._timeout = timeout

    def parse(
        self, payload: bytes | str, retrieved_at: datetime, authority: str, source_url: str
    ) -> RecordQuarantine:
        """Parse one centre's Atom feed.

        Unparseable XML or a non-``<feed>`` root is a hard failure — the
        source didn't answer with a feed at all. A single ``<entry>``
        that cannot be parsed (missing category, unmapped category, or
        a naive timestamp) is quarantined instead (D3), never allowed
        to discard every other entry.
        """
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ValueError(f"tsunami.gov response is not parseable XML: {exc}") from exc

        _require_atom_feed(root)

        alerts = RecordQuarantine()

        for entry in root.findall("atom:entry", NS):
            try:
                entry_id = _text(entry, "atom:id")
                if not entry_id:
                    raise ValueError(
                        "tsunami.gov entry has no <id>; cannot derive a stable identity"
                    )

                category = _category(entry)
                if not category:
                    raise ValueError(
                        f"tsunami.gov entry {entry_id!r} has no Category: in its summary"
                    )
                event = CATEGORY_TO_EVENT.get(category)
                if event is None:
                    raise ValueError(
                        f"tsunami.gov category {category!r} is not a known bulletin "
                        "level; refusing to pass it through unmapped or default it"
                    )

                fields = {
                    "headline": _text(root, "atom:title"),
                    "description": _description(entry),
                    "area_description": _text(entry, "atom:title"),
                    # A Tsunami Information Statement is NOT a warning --
                    # see the module docstring. The bulletin level is
                    # never mapped to CAP severity; it lives in
                    # source_severity only.
                    "severity": None,
                    "urgency": None,
                    "certainty": None,
                    "source_severity": category,
                    "source_urgency": None,
                    "source_certainty": None,
                    "sent": _iso_utc(_text(entry, "atom:updated"), "updated"),
                    "onset": None,
                    "expires": None,
                    "geometry": _geometry(entry),
                }

                unavailable = sorted(
                    set(self.STRUCTURAL_GAPS) | {k for k, v in fields.items() if v is None}
                )

                alerts.append(
                    NormalisedAlert(
                        id=f"{self.source_id}:{authority}:{entry_id}",
                        event=event,
                        provenance=Provenance(
                            authority=authority,
                            source_id=self.source_id,
                            source_url=source_url,
                            retrieved_at=retrieved_at,
                            raw_reference=_raw_reference(entry),
                        ),
                        unavailable_fields=unavailable,
                        **fields,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad entry is quarantined, not fatal
                alerts.quarantine(exc)

        return alerts

    def _fetch_one(
        self, client: httpx.Client, centre: _CentreFeed, retrieved_at: datetime
    ) -> RecordQuarantine:
        """Fetch and parse one centre. Raises on total failure (network,
        HTTP status, envelope) -- caught by the caller so one centre's
        outage never takes the other centre's alerts down with it.
        """
        response = client.get(centre.url)
        response.raise_for_status()
        return self.parse(response.content, retrieved_at, centre.authority, centre.url)

    def fetch(self) -> FetchResult:
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(
            timeout=self._timeout, headers={"User-Agent": USER_AGENT}
        )

        alerts: list[NormalisedAlert] = []
        invalid_count = 0
        invalid_samples: list[str] = []
        centre_errors: list[str] = []

        try:
            for centre in self._CENTRES:
                try:
                    centre_alerts = self._fetch_one(client, centre, retrieved_at)
                except Exception as exc:  # noqa: BLE001 - one centre's failure isn't fatal
                    centre_errors.append(
                        f"{centre.authority}: {type(exc).__name__}: {exc}"
                    )
                    continue
                alerts.extend(centre_alerts)
                invalid_count += centre_alerts.invalid_count
                room = RECORD_SAMPLE_CAP - len(invalid_samples)
                if room > 0:
                    invalid_samples.extend(centre_alerts.invalid_samples[:room])

            # Both centres unreachable is a total failure -- one centre
            # down is a partial result (invalid_count/errors would need
            # a dedicated field to surface per-centre outages the way
            # SWIC's truncated/duplicate_count do; recorded honestly in
            # invalid_samples for now rather than silently dropped).
            if len(centre_errors) == len(self._CENTRES):
                raise RuntimeError("; ".join(centre_errors))
            for err in centre_errors:
                if len(invalid_samples) < RECORD_SAMPLE_CAP:
                    invalid_samples.append(err)
                invalid_count += 1
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
            invalid_count=invalid_count,
            invalid_samples=invalid_samples,
        )
