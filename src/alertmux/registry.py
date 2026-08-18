"""WMO Register of Alerting Authorities -- a coverage map, not a source.

``https://alertingauthority.wmo.int/rss.xml`` lists every official CAP
alerting authority WMO knows about, worldwide, and the CAP categories
each covers. **It carries no warnings.** An entry here is a directory
listing -- a country and an agency name -- never an alert, and this
module must never be mistaken for one. See docs/DATA-SOURCES.md's "WMO
Register of Alerting Authorities" section.

## The join problem (read before touching this file)

Two mismatches make a naive join from the register to alertmux's own
alert data impossible, and both are structural, not bugs to fix:

1. **``iso:countrycode`` is ISO 3166-1 alpha-3** (``NGA``, ``USA``). Our
   authority slugs (``ng-nimet``, ``us-noaa``) use the alpha-2 prefix.
   The feed does not carry the alpha-3 -> alpha-2 mapping anywhere, and
   it cannot be derived by truncation (``ZAF`` -> ``za``, not ``ZA``'s
   first two letters coincidentally matching in some cases and not
   others -- ``DEU`` -> ``de``, ``GBR`` -> ``gb``). ``ALPHA3_TO_ALPHA2``
   below is an explicit ISO 3166-1 table, not a heuristic.
2. **``raa:authorityAbbrev`` disagrees with SWIC's own abbreviation.**
   WMO records Nigeria's agency abbreviation as ``nma`` (Nigerian
   Meteorological Agency); SWIC's ``capurl`` calls the same authority
   ``nimet``. So ``ng-nimet`` (our slug) never reconstructs from
   ``NGA`` + ``nma`` -- the two registries independently chose
   different short names for the same agency. Verified 18 Aug 2026:
   ``us-noaa`` is the one case where WMO's abbrev (``noaa``) happens to
   equal our slug's suffix; measured against the live register, that
   is not the common case -- most authority abbreviations disagree.

**The fix is to join at country level, not authority level.** Country
is reliable once alpha-3 is mapped to alpha-2 through the explicit
table; authority abbreviations are not reliable at all. This module
still *attempts* an authority-level match (``matched_authority``) for
the rare case an abbreviation happens to agree, exactly like
``us-noaa`` -- but an unmatched authority is reported as **unmatched**,
never folded into "uncovered." An authority alertmux simply cannot
prove a link for is not the same claim as "no alerts are ever issued
for this country." See docs/DECISIONS.md for the decision record.

## What this module guarantees

- **Cached, with the cache age always reported.** The register is
  ~250KB and changes rarely; fetching it every request would be
  wasteful, and a stale answer that looks fresh would repeat the exact
  failure mode this project exists to avoid (principle 4).
- **A fetch failure serves the last cache, and says how old it is.**
  Never silently substitutes an empty register -- an empty list here
  would read as "no alerting authorities exist," which is false and
  dangerous. If there is no cache at all (first fetch, and it fails),
  ``RegisterUnavailable`` is raised so the endpoint can report a clear
  error state instead of an empty list.
- **Never invents a value.** 3 of 300 live entries carry no
  ``raa:authorityAbbrev`` at all; those keep ``abbrev = None``, never a
  guess.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from alertmux.query import AlertsResponse
from alertmux.schema import DISCLAIMER

URL = "https://alertingauthority.wmo.int/rss.xml"
USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

NS = {
    "iso": "http://www.itu.int/tML/tML-ISO-3166",
    "cap": "urn:oasis:names:tc:emergency:cap:1.1",
    "georss": "http://www.georss.org/georss",
    "raa": "http://www.oid-info.com/get/2.49.0",
}

# The register changes rarely (most items observed carry a pubDate
# years old); a monitor or MCP client re-hitting /authorities should
# not re-fetch 250KB every time. Long TTL, deliberately much longer
# than /alerts' 60s -- see the module docstring and DECISIONS.md.
CACHE_TTL_SECONDS = 24 * 60 * 60.0

# ISO 3166-1 alpha-3 -> alpha-2, current officially assigned country
# code elements. Source: ISO 3166-1 (the standard alertmux's own
# authority slugs already follow for their alpha-2 prefix). Embedded
# as data rather than derived heuristically -- truncating or
# transliterating the alpha-3 does not produce the alpha-2 in general
# (ZAF -> za, DEU -> de, GBR -> gb; none of those are "first two
# letters"). Extend this table only from the published standard, never
# a guess, per DECISIONS.md's evidence bar for anything that could
# silently mislabel a country.
ALPHA3_TO_ALPHA2: dict[str, str] = {
    "ABW": "aw", "AFG": "af", "AGO": "ao", "AIA": "ai", "ALA": "ax",
    "ALB": "al", "AND": "ad", "ARE": "ae", "ARG": "ar", "ARM": "am",
    "ASM": "as", "ATA": "aq", "ATF": "tf", "ATG": "ag", "AUS": "au",
    "AUT": "at", "AZE": "az", "BDI": "bi", "BEL": "be", "BEN": "bj",
    "BES": "bq", "BFA": "bf", "BGD": "bd", "BGR": "bg", "BHR": "bh",
    "BHS": "bs", "BIH": "ba", "BLM": "bl", "BLR": "by", "BLZ": "bz",
    "BMU": "bm", "BOL": "bo", "BRA": "br", "BRB": "bb", "BRN": "bn",
    "BTN": "bt", "BVT": "bv", "BWA": "bw", "CAF": "cf", "CAN": "ca",
    "CCK": "cc", "CHE": "ch", "CHL": "cl", "CHN": "cn", "CIV": "ci",
    "CMR": "cm", "COD": "cd", "COG": "cg", "COK": "ck", "COL": "co",
    "COM": "km", "CPV": "cv", "CRI": "cr", "CUB": "cu", "CUW": "cw",
    "CXR": "cx", "CYM": "ky", "CYP": "cy", "CZE": "cz", "DEU": "de",
    "DJI": "dj", "DMA": "dm", "DNK": "dk", "DOM": "do", "DZA": "dz",
    "ECU": "ec", "EGY": "eg", "ERI": "er", "ESH": "eh", "ESP": "es",
    "EST": "ee", "ETH": "et", "FIN": "fi", "FJI": "fj", "FLK": "fk",
    "FRA": "fr", "FRO": "fo", "FSM": "fm", "GAB": "ga", "GBR": "gb",
    "GEO": "ge", "GGY": "gg", "GHA": "gh", "GIB": "gi", "GIN": "gn",
    "GLP": "gp", "GMB": "gm", "GNB": "gw", "GNQ": "gq", "GRC": "gr",
    "GRD": "gd", "GRL": "gl", "GTM": "gt", "GUF": "gf", "GUM": "gu",
    "GUY": "gy", "HKG": "hk", "HMD": "hm", "HND": "hn", "HRV": "hr",
    "HTI": "ht", "HUN": "hu", "IDN": "id", "IMN": "im", "IND": "in",
    "IOT": "io", "IRL": "ie", "IRN": "ir", "IRQ": "iq", "ISL": "is",
    "ISR": "il", "ITA": "it", "JAM": "jm", "JEY": "je", "JOR": "jo",
    "JPN": "jp", "KAZ": "kz", "KEN": "ke", "KGZ": "kg", "KHM": "kh",
    "KIR": "ki", "KNA": "kn", "KOR": "kr", "KWT": "kw", "LAO": "la",
    "LBN": "lb", "LBR": "lr", "LBY": "ly", "LCA": "lc", "LIE": "li",
    "LKA": "lk", "LSO": "ls", "LTU": "lt", "LUX": "lu", "LVA": "lv",
    "MAC": "mo", "MAF": "mf", "MAR": "ma", "MCO": "mc", "MDA": "md",
    "MDG": "mg", "MDV": "mv", "MEX": "mx", "MHL": "mh", "MKD": "mk",
    "MLI": "ml", "MLT": "mt", "MMR": "mm", "MNE": "me", "MNG": "mn",
    "MNP": "mp", "MOZ": "mz", "MRT": "mr", "MSR": "ms", "MTQ": "mq",
    "MUS": "mu", "MWI": "mw", "MYS": "my", "MYT": "yt", "NAM": "na",
    "NCL": "nc", "NER": "ne", "NFK": "nf", "NGA": "ng", "NIC": "ni",
    "NIU": "nu", "NLD": "nl", "NOR": "no", "NPL": "np", "NRU": "nr",
    "NZL": "nz", "OMN": "om", "PAK": "pk", "PAN": "pa", "PCN": "pn",
    "PER": "pe", "PHL": "ph", "PLW": "pw", "PNG": "pg", "POL": "pl",
    "PRI": "pr", "PRK": "kp", "PRT": "pt", "PRY": "py", "PSE": "ps",
    "PYF": "pf", "QAT": "qa", "REU": "re", "ROU": "ro", "RUS": "ru",
    "RWA": "rw", "SAU": "sa", "SDN": "sd", "SEN": "sn", "SGP": "sg",
    "SGS": "gs", "SHN": "sh", "SJM": "sj", "SLB": "sb", "SLE": "sl",
    "SLV": "sv", "SMR": "sm", "SOM": "so", "SPM": "pm", "SRB": "rs",
    "SSD": "ss", "STP": "st", "SUR": "sr", "SVK": "sk", "SVN": "si",
    "SWE": "se", "SWZ": "sz", "SXM": "sx", "SYC": "sc", "SYR": "sy",
    "TCA": "tc", "TCD": "td", "TGO": "tg", "THA": "th", "TJK": "tj",
    "TKL": "tk", "TKM": "tm", "TLS": "tl", "TON": "to", "TTO": "tt",
    "TUN": "tn", "TUR": "tr", "TUV": "tv", "TWN": "tw", "TZA": "tz",
    "UGA": "ug", "UKR": "ua", "UMI": "um", "URY": "uy", "USA": "us",
    "UZB": "uz", "VAT": "va", "VCT": "vc", "VEN": "ve", "VGB": "vg",
    "VIR": "vi", "VNM": "vn", "VUT": "vu", "WLF": "wf", "WSM": "ws",
    "YEM": "ye", "ZAF": "za", "ZMB": "zm", "ZWE": "zw",
}


def alpha3_to_alpha2(code: str | None) -> str | None:
    """Look up the embedded ISO 3166-1 table. Never guesses -- an
    alpha-3 code not in the table (a non-standard code, e.g. WMO's own
    "EUM" for EUMETNET, which is not a country) returns None rather
    than a heuristic truncation."""
    if not code:
        return None
    return ALPHA3_TO_ALPHA2.get(code.strip().upper())


def _text(elem: ET.Element, path: str) -> str | None:
    child = elem.find(path, NS)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


class RegisteredAuthority(BaseModel):
    """One entry in the WMO register. A directory listing, not an
    alert -- see the module docstring."""

    title: str
    country_name: str | None = None
    alpha3: str | None = None
    # None when alpha3 is missing or not in ALPHA3_TO_ALPHA2 -- see
    # unmappable_country_codes on AuthoritiesResponse, which is how
    # this gap stays visible rather than silently dropped.
    alpha2: str | None = None
    authority_name: str | None = None
    # None for the 3 of 300 live entries with no raa:authorityAbbrev --
    # never guessed (principle 2).
    abbrev: str | None = None
    link: str | None = None
    # The alertmux authority slug this entry could be proven to match
    # (an exact reconstruction of "{alpha2}-{abbrev}" against an
    # authority actually seen in the current fetch), or None. None
    # means unmatched, which is NOT the same claim as "uncovered" --
    # see the module docstring's join-problem section.
    matched_authority: str | None = None


def _split_title(title: str) -> tuple[str | None, str | None]:
    """"Nigeria: Nigerian Meteorological Agency" -> (country, authority).

    Splits on the first ": " only, since an authority name can itself
    contain a colon-free description with commas (observed live). A
    title with no ": " at all (never observed, but not impossible)
    yields (title, None) rather than raising -- this is a display
    field, not something matching logic depends on.
    """
    if ": " in title:
        country, _, authority = title.partition(": ")
        return country or None, authority or None
    return title or None, None


def parse(payload: bytes | str) -> list[RegisteredAuthority]:
    """Parse the register RSS into a flat list of entries.

    An unparseable envelope (not RSS at all) is a hard failure, same
    discipline as every other adapter's envelope check (DECISIONS.md
    D3) -- reading zero <item>s out of something that was never a feed
    would report "no authorities exist" as a healthy result. A single
    malformed <item> is skipped rather than aborting the whole parse;
    the register is a directory, and losing one listing must not cost
    every other one.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"WMO register response is not parseable XML: {exc}") from exc

    if root.tag != "rss":
        raise ValueError(f"WMO register response root is {root.tag!r}, not <rss>")
    channel = root.find("channel")
    if channel is None:
        raise ValueError("WMO register response has no <channel>")

    authorities: list[RegisteredAuthority] = []
    for item in channel.findall("item"):
        try:
            title = _text(item, "title")
            if not title:
                raise ValueError("WMO register item has no <title>")

            country_name, authority_name = _split_title(title)
            alpha3 = _text(item, "iso:countrycode")
            abbrev = _text(item, "raa:authorityAbbrev")
            link = _text(item, "link")

            authorities.append(
                RegisteredAuthority(
                    title=title,
                    country_name=country_name,
                    alpha3=alpha3,
                    alpha2=alpha3_to_alpha2(alpha3),
                    authority_name=authority_name,
                    abbrev=abbrev,
                    link=link,
                )
            )
        except Exception:  # noqa: BLE001 - one bad <item> is skipped, not fatal
            continue

    return authorities


class RegisterUnavailable(RuntimeError):
    """Raised when the register cannot be fetched and no cache exists
    to fall back on. Distinct from "empty register" -- an empty list
    would read as "zero alerting authorities exist worldwide," which is
    false and the exact failure this project exists to avoid."""


@dataclass
class _RegisterSnapshot:
    authorities: list[RegisteredAuthority]
    fetched_at: datetime
    fetched_monotonic: float
    # Set when this snapshot is being served because a *later* fetch
    # attempt failed and this is the last good cache -- see get_register.
    fetch_error: str | None = field(default=None)


_cache_lock = threading.Lock()
_cache: _RegisterSnapshot | None = None


def clear_cache() -> None:
    """Drop the cached register. Used by tests; harmless in production."""
    global _cache
    with _cache_lock:
        _cache = None


def _fetch_payload(client: httpx.Client | None, timeout: float) -> bytes:
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        response = http_client.get(URL)
        response.raise_for_status()
        return response.content
    finally:
        if owns_client:
            http_client.close()


def get_register(
    client: httpx.Client | None = None, timeout: float = 30.0
) -> tuple[list[RegisteredAuthority], datetime, float, str | None]:
    """Return (authorities, fetched_at, age_seconds, fetch_error).

    Within the TTL, returns the cached parse with no network call.
    Past the TTL, attempts a fresh fetch: on success the cache is
    replaced; on failure the *existing* cache is served (however old)
    with `fetch_error` set to what went wrong, per the "serve the last
    cache, say how old it is" rule. If there is no cache at all and the
    fetch fails, raises RegisterUnavailable rather than returning an
    empty register.
    """
    global _cache

    with _cache_lock:
        cached = _cache
        fresh_enough = (
            cached is not None
            and (time.monotonic() - cached.fetched_monotonic) < CACHE_TTL_SECONDS
        )
    if fresh_enough:
        age = time.monotonic() - cached.fetched_monotonic
        return cached.authorities, cached.fetched_at, age, None

    try:
        payload = _fetch_payload(client, timeout)
        authorities = parse(payload)
        now = datetime.now(tz=timezone.utc)
        snapshot = _RegisterSnapshot(
            authorities=authorities, fetched_at=now, fetched_monotonic=time.monotonic()
        )
        with _cache_lock:
            _cache = snapshot
        return authorities, now, 0.0, None
    except Exception as exc:  # noqa: BLE001 - fetch/parse failure falls back to cache
        error = f"{type(exc).__name__}: {exc}"
        if cached is not None:
            age = time.monotonic() - cached.fetched_monotonic
            return cached.authorities, cached.fetched_at, age, error
        raise RegisterUnavailable(
            f"WMO register unavailable and no cache exists: {error}"
        ) from exc


def _match_authority(
    entry: RegisteredAuthority, carried_authorities: set[str]
) -> str | None:
    """Exact-reconstruction match only -- see the module docstring.

    "{alpha2}-{abbrev}" must equal an authority slug actually observed
    in this fetch, case-insensitively. Never a substring or fuzzy
    match: those would produce exactly the false confidence this
    module exists to avoid. Most entries will not match; that is
    expected, not a bug -- see docs/DATA-SOURCES.md.
    """
    if not entry.alpha2 or not entry.abbrev:
        return None
    candidate = f"{entry.alpha2}-{entry.abbrev}".casefold()
    for authority in carried_authorities:
        if authority.casefold() == candidate:
            return authority
    return None


class AuthoritiesResponse(BaseModel):
    """`GET /authorities` -- the WMO register, joined at country level
    against alertmux's own coverage this fetch. See the module
    docstring for why authority-level joining is refused."""

    total_authorities: int
    total_countries: int
    # Register countries (alpha-2, mappable ones only) with at least
    # one alert in *this* fetch, via the same cached collect path
    # /sources uses.
    countries_covered: list[str] = Field(default_factory=list)
    # Register countries with zero alerts this fetch. The complement of
    # countries_covered within the register's mappable countries.
    countries_uncovered: list[str] = Field(default_factory=list)
    authorities: list[RegisteredAuthority] = Field(default_factory=list)
    # alpha-3 codes present in the register that ALPHA3_TO_ALPHA2 has
    # no entry for (e.g. WMO's "EUM" for EUMETNET, which is not a
    # country at all) -- named explicitly rather than silently dropped.
    unmappable_country_codes: list[str] = Field(default_factory=list)
    register_fetched_at: datetime
    register_cache_age_seconds: float
    # Set only when this response is serving a stale cache because the
    # most recent fetch attempt failed -- the register endpoint's own
    # "silent partial success is a bug" signal.
    register_fetch_error: str | None = None
    retrieved_at: datetime
    disclaimer: str = DISCLAIMER


def build_authorities_response(
    authorities: list[RegisteredAuthority],
    register_fetched_at: datetime,
    register_cache_age_seconds: float,
    register_fetch_error: str | None,
    alerts_response: AlertsResponse,
    country: str | None = None,
) -> AuthoritiesResponse:
    """Assemble the /authorities response.

    `alerts_response` must come from the same cached, read-only collect
    path /sources uses (`_collect_shared` in api.py, per D9) -- coverage
    is measured from alerts actually returned this fetch, never
    declared. This function itself never mutates `alerts_response` or
    anything reachable from it.
    """
    total_authorities = len(authorities)
    total_countries = len({a.alpha3 for a in authorities if a.alpha3})

    unmappable = sorted(
        {a.alpha3 for a in authorities if a.alpha3 and a.alpha2 is None}
    )

    # Country prefix of an alertmux authority slug, e.g. "ng-nimet" ->
    # "ng". Global sources with no country component (gdacs,
    # nasa-eonet) simply contribute nothing here -- they cannot be
    # mapped to a WMO member country and are not claimed to be.
    carried_authorities = {a.provenance.authority for a in alerts_response.alerts}
    carried_alpha2: set[str] = set()
    for authority in carried_authorities:
        prefix, sep, _ = authority.partition("-")
        if sep and len(prefix) == 2 and prefix.isalpha():
            carried_alpha2.add(prefix.lower())

    register_alpha2 = {a.alpha2 for a in authorities if a.alpha2}
    countries_covered = sorted(register_alpha2 & carried_alpha2)
    countries_uncovered = sorted(register_alpha2 - carried_alpha2)

    matched = [
        RegisteredAuthority(
            **{
                **entry.model_dump(),
                "matched_authority": _match_authority(entry, carried_authorities),
            }
        )
        for entry in authorities
    ]

    if country:
        needle = country.strip()
        needle_upper = needle.upper()
        needle_lower = needle.lower()
        matched = [
            entry
            for entry in matched
            if entry.alpha3 == needle_upper or entry.alpha2 == needle_lower
        ]

    return AuthoritiesResponse(
        total_authorities=total_authorities,
        total_countries=total_countries,
        countries_covered=countries_covered,
        countries_uncovered=countries_uncovered,
        authorities=matched,
        unmappable_country_codes=unmappable,
        register_fetched_at=register_fetched_at,
        register_cache_age_seconds=register_cache_age_seconds,
        register_fetch_error=register_fetch_error,
        retrieved_at=alerts_response.retrieved_at,
    )
