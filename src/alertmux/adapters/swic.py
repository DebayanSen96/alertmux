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
import time
from datetime import datetime, timezone

import httpx

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
    STRUCTURAL_GAPS: tuple[str, ...] = ("onset", "expires", "headline", "description")

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
                    outside the verified table) still fall through to None and
                    land in unavailable_fields exactly as before.
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

                # Unioned with every optional field that came back None, so
                # the list is exhaustive by construction rather than by
                # remembering to append.
                unavailable = sorted(
                    set(self.STRUCTURAL_GAPS) | {k for k, v in fields.items() if v is None}
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
