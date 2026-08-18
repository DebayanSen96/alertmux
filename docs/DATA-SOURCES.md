# Data sources

Everything known about the feeds alertmux reads, including things not documented
anywhere else. All observations are dated; **re-verify before relying on them.**
Public feeds change without notice.

> **Keeping this current:** when you add an adapter, add a section with the exact
> endpoint, an observed response, the fields it supplies, and — critically — the
> fields it *does not*. When a feed changes shape, record the date and what changed
> rather than overwriting history. Someone debugging a two-year-old alert needs to
> know what the feed looked like then.

---

## WMO SWIC — Severe Weather Information Centre

**The primary source: one endpoint, 59 national alerting authorities.**

SWIC publishes no public API documentation. The endpoint below was recovered by
reading the site's own JavaScript bundles (`geoserver1.js` builds its URLs from a
`gsdmn` host variable) and confirmed from the live site's network traffic.

### List endpoint

```
https://severeweather.wmo.int/g/wfs
  ?request=GetFeature
  &version=1.1.0
  &typeName=local_postgis:effective_warning_view
  &outputFormat=json
  &maxFeatures=3000
  [&cql_filter=mem='<authority code>']
```

Observed 17 Aug 2026: **2,145 warnings in force globally**, 774,979 bytes for the
full set.

Notes that cost time to learn:

- The path is **`/g/wfs`**, not `/geoserver/wfs`. Every conventional GeoServer path
  404s.
- **`GetCapabilities` 404s.** Only `GetFeature` is exposed, so the layer list cannot
  be discovered the normal way.
- **`effective_warning_view` returns only warnings currently in force.** This is the
  layer to use — expired alerts never enter the pipeline and so can never be
  re-notified.
- **Always send `maxFeatures`.** An unfiltered query on the raw layer exceeded 24MB
  and timed out at 60s.
- A second layer, `local_postgis:postgis_geojsons` on `/f/wfs`, carries geometry and
  is filtered by `row_type` (`POLYGON` held 4,732 features; `POINT` 96; `LINE` and
  `CIRCLE` were empty). `effective_warning_view` returns `geometry: null`.
- **A bad `cql_filter` or `typeName` returns HTTP 200** with an OWS exception report
  and no `features` key. This is why the adapter validates the envelope.

### Response fields

```
capurl, sent, event, s, u, c, mem, areadesc, rlink
```

- `capurl` — path to the authority's original CAP file. **Content-addressed and
  stable**, which is why alert ids derive from it.
- `rlink` — **a path to a RELATED CAP file, not a description.** Non-empty in 554 of
  2,133 alerts observed. Mapping it to a description surfaces a filename as alert
  text; do not use it.
- The synthetic `feature["id"]` (e.g.
  `effective_warning_view.fid--7463f54d_1a0121513f5_-1f9d`) **embeds a request
  timestamp** — the middle segment decodes as epoch millis of the query. It changes
  on every fetch. Confirmed: the same Nigeria alert fetched twice seconds apart
  returned ids ending `_2d5b` then `_2d5c` with an identical `capurl`. **Never use it
  as an identity.**
- The list view supplies **no** `headline`, `description`, `onset`, `expires`,
  `instruction` or `polygon`. Those live only in the CAP file.

### Detail endpoint — the raw CAP file

```
https://severeweather.wmo.int/v2/cap-alerts/<capurl>
```

Returns full **CAP 1.2**, digitally signed (`ds:Signature`), with namespaced tags
(`cap:severity`, not `severity`). Example (NiMet, 17 Aug 2026, 4,598 bytes):

```xml
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <cap:identifier>urn:oid:2.49.0.1.566.0.2026.8.17.14.50.16</cap:identifier>
  <cap:sender>cfo@nimet.gov.ng</cap:sender>
  <cap:status>Actual</cap:status>
  <cap:severity>Severe</cap:severity>
  <cap:urgency>Expected</cap:urgency>
  <cap:certainty>Observed</cap:certainty>
  <cap:onset>2026-08-17T19:16:00+01:00</cap:onset>
  <cap:expires>2026-08-18T06:00:00+01:00</cap:expires>
  <cap:headline>THUNDERSTORMS OVER PARTS OF NIGERIA</cap:headline>
```

Not fetched in v0.1 — one request per alert is too expensive for a list endpoint. It
is the natural source for `expires`, which the notifier will need. See
`V0.2` issue 5 in the project backlog.

> Finding this cost several rounds: `/v2/cap-alerts/rss.xml` 404s, and that single
> 404 was taken as evidence the whole directory did not exist. **Probe a directory
> with a file you know exists, never with a guessed filename.**

### Severity / urgency / certainty codes — CONFIRMED

`s`, `u` and `c` are integers. The mapping was established by fetching the raw CAP
for seven distinct combinations and comparing. **No conflicts.**

```
s=1 u=2 c=2  ->  Minor     Future     Possible
s=2 u=2 c=4  ->  Moderate  Future     Observed
s=2 u=3 c=2  ->  Moderate  Expected   Possible
s=3 u=3 c=2  ->  Severe    Expected   Possible
s=3 u=3 c=4  ->  Severe    Expected   Observed
s=4 u=4 c=2  ->  Extreme   Immediate  Possible
s=4 u=4 c=3  ->  Extreme   Immediate  Likely
```

| code | severity | urgency | certainty |
|---|---|---|---|
| 0 | *never observed* | *never observed* | *never observed* |
| 1 | **Minor** | *never observed* | *never observed* |
| 2 | **Moderate** | **Future** | **Possible** |
| 3 | **Severe** | **Expected** | **Likely** |
| 4 | **Extreme** | **Immediate** | **Observed** |

It is the CAP ordinal scale ascending by intensity.

**Only bold cells are mapped in code.** By the pattern, urgency `1` "should" be
`Past` and certainty `1` "should" be `Unlikely` — but neither was ever observed, and
pattern-matching is not evidence. See `DECISIONS.md` for why this is not an
oversight, and what evidence would justify adding them.

### Authority codes (`mem`) — 59 observed, 17 Aug 2026

Derived by fetching all in-force warnings and mapping `mem` to the `capurl` prefix.
**Derive this at runtime; never hardcode it.** WMO adds members.

| mem | authority | mem | authority | mem | authority |
|---|---|---|---|---|---|
| 001 | cn-cma | 062 | fr-meteofrance | 103 | bg-meteo |
| 003 | pt-ipma | 064 | gw-inm | 105 | lt-lhms |
| 005 | ba-fhmzbih | 066 | in-ndma | 106 | by-belhydromet |
| 006 | at-zamg | 067 | ie-met | 107 | ru-roshydromet |
| 008 | no-met | 070 | kz-kazhydromet | 108 | md-shs |
| 009 | pl-imgw | **075** | **ng-nimet** | 122 | dz-onm |
| 013 | il-met | 079 | sa-ncm | 137 | ec-inamhi |
| 015 | si-meteo | 083 | es-aemet | 139 | sb-sims |
| 016 | de-dwd | 085 | sd-sma | 169 | vu-vmgd |
| 017 | hu-met | 087 | ch-meteoswiss | 171 | cr-imn |
| 019 | hr-meteo | 088 | td-anam | 172 | cz-chmi |
| 021 | ph-pagasa | 089 | th-tmd | 176 | it-meteoam |
| 026 | jm-jms | 092 | ua-meteo | 177 | kg-meteo |
| 028 | cl-meteo | 093 | us-noaa | 179 | mx-smn |
| 037 | nl-rnmi | 094 | uy-inumet | 181 | cw-meteo |
| 038 | bz-nms | 095 | kr-kma | 183 | ro-meteoromania |
| 039 | ee-emhi | 096 | se-smhi | 185 | au-bom |
| 043 | id-inatews | 101 | rs-hidmet | 193 | me-meteo |
| 046 | nz-nms | 053 | be-irm | 055 | cm-meteo |
| 056 | ca-msc | 061 | fi-fmi | | |

---

## Source overlap — measured, not assumed

Verified 18 Aug 2026. The v0.1 design claimed SWIC "replaced" the NWS, GDACS and
EONET adapters because one endpoint covers 59 authorities. **That was wrong**, and
the numbers say so plainly.

### SWIC vs NWS — same domain, SWIC is the lossy copy

| | NWS direct | SWIC `mem='093'` (us-noaa) |
|---|---|---|
| Alerts in force | **292** | 204 (**69%**) |
| Fields per alert | **32** | 9 |

Missing from SWIC: 84 Small Craft Advisories, plus High Surf Advisory entirely.
NWS additionally supplies `description`, `instruction`, `headline`, `expires`,
`onset`, `effective`, `sender`, `category`, `response`, `status`, `messageType`,
and — decisively — **`severity`/`urgency`/`certainty` as named CAP values**, so no
integer-code mapping is needed at all.

`expires` matters most: it is the field a notifier needs to tell a live warning from
a lapsed one, and SWIC's list view does not carry it.

**Conclusion: use NWS directly for US coverage.** SWIC's `us-noaa` slice is strictly
worse on both count and depth.

### SWIC does not cover whole hazard families

Across all 2,235 SWIC alerts in force:

```
   694  wind/storm        206  wildfire/fire
   376  heat              115  flood
   269  rain                1  volcano
     2  snow/ice
     0  earthquake     <-- zero
     0  drought        <-- zero
     0  tsunami        <-- zero
```

SWIC is a **meteorological warning** system. GDACS carries what it cannot:
365 events — 315 wildfire, 19 flood, **16 earthquake, 12 drought**, 3 tropical
cyclone — each with an alert level and impact estimate
(*"Green earthquake, Magnitude 5.6M, Depth 10km, Indonesia, 3 thousand in MMI V"*).
EONET carries satellite-**observed** events: 195 wildfires, 5 severe storms.

The two groups answer different questions and are not interchangeable:

- **SWIC + NWS** — what authorities are **warning** about (forecast)
- **GDACS + EONET + USGS** — what is **happening** (observed)

A coverage metric counting only authorities is therefore misleading: 59/300
authorities can still mean zero drought and zero earthquake coverage. **Track
coverage by hazard family as well as by authority.**

## NOAA / NWS — United States

```
https://api.weather.gov/alerts/active
```

No key. GeoJSON, CAP-derived, 32 properties per feature.

- **`?limit=N` returns HTTP 400.** The parameter is not supported the way it looks;
  use the unparameterised endpoint.
- **`status` must be checked.** The live feed carries test traffic — a persistent
  `KEEPALIVE` record with `status: "Test"`, `event: "Test Message"`. Observed 1 of
  295. **Only `status == "Actual"` may be relayed as a warning.** Relaying a test
  message as a real hazard alert would violate the project's central promise. SWIC
  does not have this problem; it filters upstream (0 test events observed).
- `messageType` is `Alert` (189) or `Update` (106). `Cancel` exists in the CAP spec
  and must not surface as an active alert if it appears.
- **Only 48 of 295 features carry geometry**; the rest are zone-referenced via
  `affectedZones` / `geocode`. A null geometry here means "referenced by zone", not
  "unknown" — do not conflate the two.
- `severity` values observed: `Minor` 171, `Severe` 66, `Moderate` 49, `Unknown` 9.
  `Unknown` is a real CAP value, not a missing field.

## GDACS — global disaster alerts

```
https://www.gdacs.org/xml/rss.xml
```

RSS with a `gdacs:` namespace. 365 items observed. Event types via
`<gdacs:eventtype>`: `WF` wildfire 315, `FL` flood 19, `EQ` earthquake 16,
`DR` drought 12, `TC` tropical cyclone 3.

Namespaced tags include `gdacs:alertlevel` (Green/Orange/Red), `gdacs:alertscore`,
`gdacs:country`, `gdacs:bbox`, `gdacs:eventtype`, `gdacs:cap`, `gdacs:severity`.

**`gdacs:alertlevel` is an impact score, not CAP severity.** Green/Orange/Red
describe expected humanitarian impact. Do not map it onto CAP's
Minor/Moderate/Severe/Extreme — that would assert a severity GDACS never stated.
Keep it in `source_severity`.

The JSON API at `/gdacsapi/api/events/geteventlist/MAP` returned 400 for every
parameter name tried. Use the RSS.

**Implemented in `adapters/gdacs.py`, 18 Aug 2026 (issue #13).** The first
RSS/XML adapter in the codebase — parsed with the standard library's
`xml.etree.ElementTree`, no new dependency. Notes gathered while building it:

- **Identity: `gdacs:eventid` + `gdacs:episodeid`, not `<guid>`.** The RSS
  `<guid>` on this feed is only `{eventtype}{eventid}` (e.g. `EQ1559738`) —
  stable, but coarser than needed, since the same disaster accumulates
  multiple episodes as it evolves (the earthquake fixture item is
  `eventid=1559738`, `episodeid=1726926`). Verified stable by fetching the
  live feed twice, 18 Aug 2026: the same 369 `(eventid, episodeid)` pairs
  came back identical on both fetches.
- **`gdacs:eventtype` needs an explicit table, same as SWIC's severity
  codes.** Observed in the live feed that day: `WF` 319, `FL` 19, `EQ` 16,
  `DR` 12, `TC` 3 — 369 total. `VO` (volcano) is in the mapping table on the
  strength of GDACS's own documentation, though it was not present in that
  day's sample.
- **No `urgency`, `certainty`, or `expires` at all** — not even as unmapped
  source-native values. All three are structurally unavailable on every
  alert.
- **Timestamps are RFC-822** (`pubDate`, `gdacs:fromdate`), not ISO-8601 —
  parsed with `email.utils.parsedate_to_datetime`, not `fromisoformat`.
- **Geometry:** `geo:Point` is used when present (most precise); `gdacs:bbox`
  (`lonmin lonmax latmin latmax`) is mapped to a rectangular GeoJSON Polygon
  when there is no point — a faithful reading, not an approximation, since a
  bbox unambiguously denotes a rectangle.
- A fixture trimmed to 5 items (`tests/fixtures/gdacs_rss.xml`, one each of
  EQ/DR/WF/TC/FL) was recorded from the live feed the same day; real field
  names and values throughout, no invented data.
- **`gdacs:country` (`area_description`) is country-level only — never a
  locality.** It is just the country name (`"Angola"`, `"Brazil"`), not a
  region, province, or town. This is what makes `event` + `area_description`
  a dangerously coarse identity key for GDACS specifically: 98 separate
  Angola wildfires, each with its own distinct `gdacs:eventid`, all carry
  `area_description="Angola"` and would collapse onto one
  `wildfire|angola` key with nothing to tell them apart if grouped naively.
  `dedupe.py` guards against this by requiring every member of a candidate
  group to come from a different `provenance.source_id` — see
  DECISIONS.md D13's 18 Aug 2026 addendum. Anything else built on top of
  `area_description` (filtering, display grouping, a future notifier) should
  expect the same coarseness from this source and not assume country-level
  text means "the whole country is affected."

## NASA EONET — satellite-observed events

```
https://eonet.gsfc.nasa.gov/api/v2.1/events?limit=<n>
```

Clean JSON, no key. Event fields: `id`, `title`, `description`, `link`,
`categories`, `sources`, `geometries`.

Observed categories: Wildfires 195, Severe Storms 5.

**These are observations, not warnings.** EONET has no severity, no urgency, no
certainty, no expiry — those concepts do not apply, and all must be recorded as
structurally unavailable rather than invented. `geometries` is a list of timestamped
points or polygons; an event may have many as it is tracked over time.

Largely overlaps GDACS on wildfires. Its value is independent satellite
corroboration of an event another source is only forecasting.

## USGS — earthquakes

```
https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson
```

Clean GeoJSON, no key, well documented upstream. Two traps:

- **`time` and `updated` are epoch MILLISECONDS.** A seconds-based conversion puts
  events roughly 56,000 years in the future.
- **`alert` is a PAGER level** (`green`/`yellow`/`orange`/`red`), **not CAP
  severity.** It is kept in `source_severity` only; `severity` stays null. Mapping it
  to a CAP level would assert a severity USGS never stated.
- `type` is not always `earthquake` — the feed also emits `quarry blast`,
  `explosion`, `ice quake`, `sonic boom`, `mining explosion`. Never default it.
- USGS reports *observed* events, so it supplies no `urgency`, `certainty`, `onset`
  or `expires`. All four are structurally unavailable.

---

## WMO Register of Alerting Authorities

```
https://alertingauthority.wmo.int/rss.xml     (also /atom.xml)
```

250KB of RSS, **300 items** — every official alerting authority worldwide, with the
CAP categories each covers. Nigeria's entry:

```
Nigeria: Nigerian Meteorological Agency
https://alertingauthority.wmo.int/authorities.php?recId=119
CAP categories: Geo Met Safety Security Health Env Transport CBRNE
```

It lists authorities, **not feed URLs**, so it is a directory rather than a source of
alerts. Its value is as an authoritative coverage map: 59 of 300 authorities are
reachable through SWIC today, and the gaps are a legitimate contribution backlog
nobody has to be persuaded matters.

Implemented in v0.3 as `registry.py` / `GET /authorities`.

> Practical note: this host returned 0 bytes to some clients and 250KB to others.
> If it comes back empty, change the user agent before concluding the feed is down.

### The join problem — country-level only, and why

Each item carries `title, link, description, guid, pubDate, author,
iso:countrycode, raa:authorityAbbrev, cap:area, cap:geocode, cap:polygon,
cap:value, cap:valueName, georss:box`. Two mismatches make a naive join from
this register to alertmux's own alert data impossible at the authority level.

**1. `iso:countrycode` is ISO 3166-1 alpha-3** (`NGA`, `USA`, `CYM`). Every
alertmux authority slug (`ng-nimet`, `us-noaa`) uses the alpha-2 prefix, and
the feed does not carry the alpha-3 → alpha-2 mapping anywhere. It cannot be
derived by truncating the alpha-3: `NGA` → `ng` and `CHE` → `ch` happen to
work that way, but `ZAF` → `za`, `DEU` → `de` and `GBR` → `gb` do not follow
any rule from their alpha-3 form. `registry.py`'s `ALPHA3_TO_ALPHA2` is an
explicit, embedded ISO 3166-1 table — not a heuristic.

Some items carry a `cap:geocode` with `valueName: iso-3166-1-alpha-2` and the
alpha-2 value directly (Nigeria's item does not; the USA's NOAA/NWS entry
does). Measured 18 Aug 2026: only 159 of 300 items carry this geocode at
all — including neither Nigeria's nor 140 others — so it cannot be relied on
as the mapping source; the embedded table covers all 300.

**2. `raa:authorityAbbrev` disagrees with SWIC's own abbreviation for the
same authority.** WMO records Nigeria's agency abbreviation as `nma`
(Nigerian Meteorological Agency); SWIC's `capurl` calls the identical
authority `nimet`. So alertmux's own slug `ng-nimet` never reconstructs from
`NGA` + `nma` — the two registries independently chose different short names
for the same agency, and there is no transform between them. Verified: the
one case where WMO's abbrev agrees with alertmux's own slug suffix is
`us-noaa` (WMO: `noaa`). That is not evidence the join generally works — it
is the coincidence that makes the failure easy to miss. Measured: the
register holds 300 authorities across 199 countries; alertmux's live alerts
carry 56 authorities across 52 alpha-2 country prefixes, and country-level
overlap between those two sets is real and useful; authority-level overlap
cannot be asserted without inventing a mapping nobody publishes.

**The consequence.** `GET /authorities` joins at country level only.
`countries_covered` / `countries_uncovered` compare the register's mapped
alpha-2 codes against the country prefix of authorities that actually
returned an alert this fetch. Each register entry also carries
`matched_authority`, populated only when `"{alpha2}-{abbrev}"` exactly
equals an authority slug seen this fetch (so `us-noaa` matches, `ng-nimet`
does not) — this is the deliberately rare, honest case, never generalised
into a claim the data cannot support. See docs/DECISIONS.md for the
decision record and its reasoning.

---

## Sources evaluated and rejected

| Source | Why not |
|---|---|
| **NiMet direct** (`nimet.gov.ng`) | No machine-readable feed. HTTP 307 to a JavaScript wall; `/api/warnings` 302s. Nigeria is reached through SWIC instead. |
| **MeteoAlarm** | Europe only. 404 for Nigeria. |
| **GDACS JSON API** | `/gdacsapi/api/events/geteventlist/MAP` returned 400 for every parameter name tried. The RSS feed at `gdacs.org/xml/rss.xml` works unambiguously. |
| **Ushahidi** | Dormant (2 commits in six months) and licensed `NOASSERTION` — no grant of rights to rely on. |
