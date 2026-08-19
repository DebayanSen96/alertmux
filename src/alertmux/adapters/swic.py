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
headline, description, expires, onset, instruction and polygon, which
this list view omits. Fetching one file per alert is too expensive for
a list endpoint, so `fetch()` never does it inline; `fetch_detail()`
fetches and parses a single CAP file on request, cached by `capurl`
(issue #4). See its docstring.

Pagination and truncation: GeoServer caps a single response at
`maxFeatures`. `fetch()` loops on `startIndex`, requesting further
pages while a page came back filled to the cap or `numberMatched` says
more remain, until the server is exhausted, `max_pages` is reached, or
a page adds nothing new (see `fetch()`'s docstring for the exact
rules). `FetchResult.truncated` is set only when the loop stops without
confirming exhaustion, and that flag propagates to
`AlertsResponse.partial` — see DECISIONS.md D4.

Identity: the GeoServer synthetic feature `id` embeds a REQUEST
timestamp, so it changes on every fetch and cannot be a stable alert id.
The alert id is derived from `capurl`, which is the identity of the CAP
file itself and is stable across fetches.
"""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

from alertmux.adapters.base import RECORD_SAMPLE_CAP, FetchResult, RecordQuarantine
from alertmux.schema import NormalisedAlert, Provenance

USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

_AUTHORITY_RE = re.compile(r"^([a-z]{2}-[a-z0-9]+)")

# Confirmed against raw CAP 1.2, 17 Aug 2026, 7 samples, no conflicts.
# Deliberately partial: codes never observed are absent, not guessed.
SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}
URGENCY = {2: "Future", 3: "Expected", 4: "Immediate"}
CERTAINTY = {2: "Possible", 3: "Likely", 4: "Observed"}


def _authority_from_capurl(capurl: str | None) -> str:
    """capurl looks like 'ng-nimet-en/2026/08/17/...xml'.

    Authority is mandatory provenance. An unparseable capurl raises
    rather than inventing a placeholder authority.
    """
    if not capurl:
        raise ValueError("SWIC feature has no capurl; cannot derive authority")
    match = _AUTHORITY_RE.match(capurl)
    if match is None:
        raise ValueError(
            f"SWIC capurl {capurl!r} does not carry a parseable authority prefix"
        )
    return match.group(1)


def _iso_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an offset.

    A naive value would be interpreted as server-local time by
    astimezone(), silently shifting a hazard timestamp by the deploying
    machine's UTC offset. Refuse instead of guessing.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"SWIC {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_feature_collection(payload: dict) -> None:
    """A 200 that is not a FeatureCollection is an error, not a quiet day.

    GeoServer answers a bad cql_filter, an unknown typeName or backend
    trouble with an OWS exception report at HTTP 200 and no `features`
    key. Treating that as zero alerts would report "no hazards anywhere"
    as a healthy result.
    """
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
            or "features" not in payload:
        raise ValueError(
            "SWIC response is not a GeoJSON FeatureCollection: "
            f"{repr(payload)[:400]}"
        )


CAP_DETAIL_URL_TEMPLATE = "https://severeweather.wmo.int/v2/cap-alerts/{capurl}"
CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
_CAP_ROOT_TAG = "{urn:oasis:names:tc:emergency:cap:1.2}alert"


class CapDetailError(RuntimeError):
    """Raised when a single alert's CAP detail file cannot be fetched or
    parsed (issue #4). Distinct from D3's per-record quarantine, which
    applies to the *list* endpoint and skips one bad record among many:
    `fetch_detail()` is a single explicit request for one alert, so a
    failure here has nowhere quieter to go than the caller. The
    `/alerts/{id}/detail` endpoint turns this into a clear HTTP error
    rather than an empty record that would read as "no detail exists".
    """


class CapDetail(BaseModel):
    """The fields a CAP 1.2 file supplies that SWIC's list view omits.

    A separate model rather than reusing `NormalisedAlert` directly: the
    CAP file describes one alert's *detail*, not a second alert, and
    keeping the two shapes distinct makes `enrich_with_detail()`'s merge
    rules explicit instead of implicit in field-by-field overwriting.
    """

    identifier: str | None = None
    sender: str | None = None
    language: str | None = None
    headline: str | None = None
    description: str | None = None
    instruction: str | None = None
    onset: datetime | None = None
    expires: datetime | None = None
    # Named CAP values, straight from the authority's own signed record
    # -- authoritative over the list view's integer codes (DECISIONS.md).
    severity: str | None = None
    urgency: str | None = None
    certainty: str | None = None
    geometry: dict | None = None


def _cap_text(elem: ET.Element, path: str) -> str | None:
    child = elem.find(path, CAP_NS)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _cap_polygon_to_geojson(text: str) -> dict | None:
    """CAP polygon text is space-separated "lat,lon" pairs, closed (first
    point repeats last). GeoJSON wants [lon, lat]. A polygon that fails
    to parse is dropped (returns None) rather than raising -- one
    malformed geometry must not cost the rest of the detail record,
    matching D3's quarantine spirit for a field that is decoration, not
    identity.
    """
    try:
        points = []
        for pair in text.strip().split():
            lat_str, _, lon_str = pair.partition(",")
            points.append([float(lon_str), float(lat_str)])
        if len(points) < 4:
            return None
        return {"type": "Polygon", "coordinates": [points]}
    except (ValueError, IndexError):
        return None


def parse_cap_detail(payload: bytes | str) -> CapDetail:
    """Parse one CAP 1.2 file (issue #4).

    Real signed CAP files observed from this endpoint use a bound
    `cap:` prefix for the namespace in some authorities and the bare
    default namespace in others (both resolve to the same
    `urn:oasis:names:tc:emergency:cap:1.2` URI) -- `ET`'s own namespace
    resolution handles either, since lookups here use our own `cap:`
    prefix bound to that URI, not the source document's chosen prefix.

    A CAP file can carry more than one `<info>` block (observed:
    parallel-language versions of the same alert). The English block is
    preferred when present; otherwise the first block, since alertmux
    relays verbatim and does not choose a "best" translation beyond
    that.

    An envelope that is not parseable XML, or carries no `<info>` block
    at all, is a hard failure (mirrors every other adapter's envelope
    check, D3) -- there is no "quiet" reading of a CAP file that failed
    to parse.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"SWIC CAP detail is not parseable XML: {exc}") from exc

    if root.tag != _CAP_ROOT_TAG:
        raise ValueError(
            f"SWIC CAP detail root is {root.tag!r}, not a CAP 1.2 <alert>"
        )

    infos = root.findall("cap:info", CAP_NS)
    if not infos:
        raise ValueError("SWIC CAP detail has no <cap:info> block")

    info = next(
        (i for i in infos if (_cap_text(i, "cap:language") or "").lower().startswith("en")),
        infos[0],
    )

    geometry = None
    polygon_text = _cap_text(info, "cap:area/cap:polygon")
    if polygon_text:
        geometry = _cap_polygon_to_geojson(polygon_text)

    return CapDetail(
        identifier=_cap_text(root, "cap:identifier"),
        sender=_cap_text(root, "cap:sender"),
        language=_cap_text(info, "cap:language"),
        headline=_cap_text(info, "cap:headline"),
        description=_cap_text(info, "cap:description"),
        instruction=_cap_text(info, "cap:instruction"),
        onset=_iso_utc(_cap_text(info, "cap:onset"), "onset"),
        expires=_iso_utc(_cap_text(info, "cap:expires"), "expires"),
        severity=_cap_text(info, "cap:severity"),
        urgency=_cap_text(info, "cap:urgency"),
        certainty=_cap_text(info, "cap:certainty"),
        geometry=geometry,
    )


# CAP files are content-addressed: capurl embeds a hash of the file's
# own content, so the same capurl can never resolve to different bytes
# once published. That makes the cache unconditional -- no TTL, no
# staleness check needed, because there is nothing to go stale. See
# DECISIONS.md.
_cap_cache_lock = threading.Lock()
_cap_cache: dict[str, CapDetail] = {}


def clear_cap_cache() -> None:
    """Drop the cached CAP details. Used by tests; harmless in production."""
    with _cap_cache_lock:
        _cap_cache.clear()


def fetch_cap_detail(
    capurl: str, client: httpx.Client | None = None, timeout: float = 30.0
) -> CapDetail:
    """Fetch and parse one CAP file, cached by `capurl` forever (issue #4).

    Deliberately separate from `SwicAdapter.fetch()`: with ~2,200 alerts
    in force, fetching one CAP file per alert on every list poll would
    be ~2,200 requests to WMO per fetch, which is far too expensive for
    a list endpoint. This is only called explicitly, per alert, by
    `SwicAdapter.fetch_detail()` and the `/alerts/{id}/detail` route.

    Raises `CapDetailError` on any failure -- network, HTTP status, or
    parse -- rather than returning `None`. A `None` return would be
    ambiguous between "fetched fine, nothing there" (which cannot
    happen; a CAP file always has an `<info>` block, see
    `parse_cap_detail`) and "something went wrong", and principle 4
    forbids exactly that kind of silent partial success.
    """
    with _cap_cache_lock:
        cached = _cap_cache.get(capurl)
    if cached is not None:
        return cached

    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    try:
        response = http_client.get(CAP_DETAIL_URL_TEMPLATE.format(capurl=capurl))
        response.raise_for_status()
        detail = parse_cap_detail(response.content)
    except Exception as exc:  # noqa: BLE001 - normalised into CapDetailError
        raise CapDetailError(
            f"SWIC CAP detail fetch failed for {capurl!r}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if owns_client:
            http_client.close()

    with _cap_cache_lock:
        _cap_cache[capurl] = detail
    return detail


def enrich_with_detail(alert: NormalisedAlert, detail: CapDetail) -> NormalisedAlert:
    """Merge a fetched CAP detail into a list-view `NormalisedAlert`
    (issue #4).

    The CAP file is the authority's own signed record, so where it
    states a named severity/urgency/certainty, that value wins over the
    list view's integer-code mapping -- the integer itself is untouched
    in `source_severity`/`source_urgency`/`source_certainty` either way
    (see DECISIONS.md). Every other CAP field fills a gap the list view
    structurally could not supply (`headline`, `description`,
    `instruction`, `onset`, `expires`, and `geometry` when the list view
    had none); nothing the list view already stated is overwritten with
    a CAP value that might disagree with it, since neither record is
    "more true" for fields both happen to carry.

    Returns a new `NormalisedAlert` -- `alert` itself is never mutated,
    matching D9's "shared state must not be mutated by one caller for
    another" discipline now that a cached response could be enriched
    more than once.

    Merging only ever fills a field in, never empties one, so a field
    that stays None keeps whichever of `unavailable_fields` /
    `unmapped_fields` it was already in -- an unverified list-view code
    (unmapped) does not turn into "the source said nothing" just
    because the CAP file happened not to restate it either, and a
    field the list view structurally never carries does not turn into
    "declined to map" just because this function ran.
    """
    updated = alert.model_copy(
        update={
            "headline": alert.headline or detail.headline,
            "description": alert.description or detail.description,
            "instruction": alert.instruction or detail.instruction,
            "onset": alert.onset or detail.onset,
            "expires": alert.expires or detail.expires,
            "geometry": alert.geometry or detail.geometry,
            # The CAP file is authoritative (see docstring); it wins
            # over the list view's mapped name whenever it states one.
            "severity": detail.severity or alert.severity,
            "urgency": detail.urgency or alert.urgency,
            "certainty": detail.certainty or alert.certainty,
        }
    )
    still_none = {
        name
        for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields", "unmapped_fields"}
        and getattr(updated, name) is None
    }
    unavailable = sorted(still_none & set(alert.unavailable_fields))
    unmapped = sorted(still_none & set(alert.unmapped_fields))
    return updated.model_copy(
        update={"unavailable_fields": unavailable, "unmapped_fields": unmapped}
    )


class SwicAdapter:
    """Fetches and normalises WMO SWIC effective warnings."""

    source_id = "wmo-swic"
    URL = "https://severeweather.wmo.int/g/wfs"
    TYPE_NAME = "local_postgis:effective_warning_view"

    # Fields this feed structurally never supplies, regardless of what
    # any particular record carries -- the list view carries no
    # headline/description (those live only in the CAP file) and no
    # onset/expires. A class attribute so /sources can report it
    # without running a fetch. See parse() for how it is combined with
    # per-record gaps.
    STRUCTURAL_GAPS: tuple[str, ...] = (
        "onset", "expires", "headline", "description", "instruction",
    )

    def __init__(
        self,
        client: httpx.Client | None = None,
        mem: str | None = None,
        max_features: int = 3000,
        timeout: float = 60.0,
        max_pages: int = 20,
    ):
        self._client = client
        self._mem = mem
        self._max_features = max_features
        self._timeout = timeout
        # Bounds the startIndex loop in fetch() (see the pagination
        # section of the module docstring and DECISIONS.md D4). An
        # unbounded loop against a remote server is a liability; hitting
        # this ceiling before the server is exhausted leaves `truncated`
        # True rather than either spinning forever or silently stopping.
        self._max_pages = max_pages

    def build_params(
        self, mem: str | None, max_features: int, start_index: int = 0
    ) -> dict:
        params = {
            "request": "GetFeature",
            "version": "1.1.0",
            "typeName": self.TYPE_NAME,
            "outputFormat": "json",
            "maxFeatures": max_features,
            "startIndex": start_index,
            # WFS 1.1.0 does not mandate a feature order, so startIndex is
            # only well-defined paired with a sort key -- verified live,
            # 18 Aug 2026: this GeoServer answers ANY request carrying
            # startIndex with a bare "Err" body (HTTP 200, not even a
            # proper WFS exception report) unless sortBy is also present.
            # capurl is unique and stable (D2), so sorting by it costs
            # nothing and gives a deterministic page order for free -- no
            # risk of the same record shifting between two pages.
            "sortBy": "capurl",
        }
        if mem:
            params["cql_filter"] = f"mem='{mem}'"
        return params

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        """Parse the FeatureCollection. An envelope that is not a valid
        FeatureCollection is a hard failure (see
        `_require_feature_collection`) -- the source is broken. A single
        feature that cannot be parsed is quarantined instead: it is
        skipped, counted, and sampled onto the returned list's
        `.invalid_count` / `.invalid_samples` (see `RecordQuarantine`),
        never allowed to discard every other feature in the response
        (DECISIONS.md D3).
        """
        _require_feature_collection(payload)

        alerts = RecordQuarantine()

        for feature in payload["features"]:
            try:
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
                if not capurl:
                    raise ValueError(
                        f"SWIC feature {feature.get('id')!r} has no capurl; "
                        "the synthetic GeoServer fid is not a stable identity"
                    )

                def _code(key: str) -> str | None:
                    value = props.get(key)
                    return str(value) if value is not None else None

                def _named(key: str, table: dict[int, str]) -> str | None:
                    """Map a code only if it was verified. Never guess.

                    SWIC emits s/u/c as JSON numbers, but some authorities send
                    them as digit strings ("3" instead of 3). A string key
                    against the int table misses silently and leaves the named
                    field null, so coerce digit strings to int before lookup.
                    Genuinely unmappable values (None, non-digit strings, codes
                    outside the verified table) still fall through to None.
                    Whether that None then lands in unmapped_fields (a code
                    was supplied but refused) or unavailable_fields (nothing
                    was supplied at all) is decided below, from whether the
                    raw props value was present -- not from this function.
                    """
                    raw = props.get(key)
                    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
                        raw = int(raw)
                    return table.get(raw)

                # The WFS list view carries no headline and no description;
                # both live only in the CAP file. `rlink` is a path to a
                # RELATED CAP file and is not a description of this alert.
                fields = {
                    "headline": None,
                    "description": None,
                    "area_description": props.get("areadesc"),
                    "severity": _named("s", SEVERITY),
                    "urgency": _named("u", URGENCY),
                    "certainty": _named("c", CERTAINTY),
                    "source_severity": _code("s"),
                    "source_urgency": _code("u"),
                    "source_certainty": _code("c"),
                    "sent": _iso_utc(props.get("sent"), "sent"),
                    # onset/expires live in the CAP file, not this list view.
                    "onset": None,
                    "expires": None,
                    "geometry": feature.get("geometry"),
                }

                # A named field that came back None is unmapped -- not
                # unavailable -- exactly when its raw s/u/c code was
                # supplied and refused (an unverified code, D1). When the
                # source sent nothing at all for that code, it is
                # unavailable like any other missing field.
                unmapped = sorted(
                    name
                    for name, key in (
                        ("severity", "s"), ("urgency", "u"), ("certainty", "c")
                    )
                    if fields[name] is None and props.get(key) is not None
                )

                # Unioned with every optional field that came back None, so
                # the list is exhaustive by construction rather than by
                # remembering to append. unmapped fields are carved out so
                # a field never lands in both lists.
                unavailable = sorted(
                    (set(self.STRUCTURAL_GAPS) | {k for k, v in fields.items() if v is None})
                    - set(unmapped)
                )

                alerts.append(
                    NormalisedAlert(
                        id=f"{self.source_id}:{capurl}",
                        event=event,
                        provenance=Provenance(
                            authority=_authority_from_capurl(capurl),
                            source_id=self.source_id,
                            source_url=self.URL,
                            retrieved_at=retrieved_at,
                            raw_reference=capurl,
                        ),
                        unavailable_fields=unavailable,
                        unmapped_fields=unmapped,
                        **fields,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad record is quarantined, not fatal
                alerts.quarantine(exc)

        return alerts

    def fetch(self) -> FetchResult:
        """Fetch every page of `effective_warning_view`, looping on
        `startIndex` until the server is exhausted (issue #3 / D4).

        A page is considered possibly incomplete -- and worth requesting
        a follow-up page for -- when either:

        1. It came back filled to `maxFeatures` (the belt-and-braces
           rule: this fires even when `numberMatched` is the string
           `"unknown"`, so a capped page is never reported complete
           just because the server declined to say how many it holds).
        2. `numberMatched` is a usable int and the cumulative offset
           reached so far is still short of it.

        The loop stops -- and `truncated` stays False -- as soon as a
        page comes back short of the cap with nothing left owed by
        `numberMatched`: that is exhaustion. It also stops -- leaving
        `truncated` True -- if `max_pages` is reached first, or if a
        page contributes zero records not already seen (the server's
        ordering is unstable, or `startIndex` is not being honoured);
        either way, spinning forever against a remote server is not an
        option, and pretending the fetch is complete would not be
        either.

        Records are deduplicated across pages by alert id (derived from
        the stable `capurl`, see D2): a repeat is dropped and counted in
        `duplicate_count` rather than allowed to inflate the alert
        count. Per-record quarantine (D3) applies independently on every
        page and accumulates into the same `invalid_count` /
        `invalid_samples`.
        """
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(
            timeout=self._timeout, headers={"User-Agent": USER_AGENT}
        )

        combined = RecordQuarantine()
        seen_ids: set[str] = set()
        duplicate_count = 0
        matched: int | None = None
        truncated = False
        start_index = 0
        pages_fetched = 0

        try:
            while True:
                response = client.get(
                    self.URL,
                    params=self.build_params(
                        self._mem, self._max_features, start_index
                    ),
                )
                response.raise_for_status()
                payload = response.json()
                page_alerts = self.parse(payload, retrieved_at)
                pages_fetched += 1

                page_matched = payload.get("numberMatched")
                page_returned = payload.get("numberReturned")
                page_matched = page_matched if isinstance(page_matched, int) else None
                page_returned = (
                    page_returned if isinstance(page_returned, int) else None
                )
                if page_matched is not None:
                    matched = page_matched

                new_on_page = 0
                for alert in page_alerts:
                    if alert.id in seen_ids:
                        duplicate_count += 1
                        continue
                    seen_ids.add(alert.id)
                    combined.append(alert)
                    new_on_page += 1

                combined.invalid_count += page_alerts.invalid_count
                if page_alerts.invalid_samples:
                    room = RECORD_SAMPLE_CAP - len(combined.invalid_samples)
                    if room > 0:
                        combined.invalid_samples.extend(
                            page_alerts.invalid_samples[:room]
                        )

                # Belt-and-braces (D4): a page filled to maxFeatures can
                # never be reported complete, whatever numberMatched said
                # (including "unknown").
                page_full = (
                    page_returned is not None and page_returned >= self._max_features
                )
                cumulative_offset = start_index + (page_returned or 0)
                matched_says_more = (
                    matched is not None
                    and page_returned is not None
                    and cumulative_offset < matched
                )
                more_pages_owed = page_full or matched_says_more

                if not more_pages_owed:
                    # Exhausted: this page was short of the cap and
                    # numberMatched (if usable) is satisfied.
                    break
                if new_on_page == 0:
                    # No forward progress possible; stop rather than spin.
                    truncated = True
                    break
                if pages_fetched >= self._max_pages:
                    # Ceiling hit before exhaustion -- never spin forever.
                    truncated = True
                    break
                start_index = cumulative_offset
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
            alerts=combined,
            retrieved_at=retrieved_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
            matched=matched,
            returned=len(combined),
            invalid_count=combined.invalid_count,
            invalid_samples=combined.invalid_samples,
            duplicate_count=duplicate_count,
        )

    def fetch_detail(self, capurl: str) -> CapDetail:
        """Fetch and parse one alert's raw CAP file (issue #4).

        Never called from `fetch()` or `parse()` -- see the module
        docstring and `fetch_cap_detail`'s. Opt-in, per alert, cached by
        `capurl` forever. Raises `CapDetailError` on failure; see that
        function's docstring for why this does not return `None`.
        """
        return fetch_cap_detail(capurl, client=self._client, timeout=self._timeout)
