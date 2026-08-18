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

Not implemented in v0.1. Planned as the registry module.

> Practical note: this host returned 0 bytes to some clients and 250KB to others.
> If it comes back empty, change the user agent before concluding the feed is down.

---

## Sources evaluated and rejected

| Source | Why not |
|---|---|
| **NiMet direct** (`nimet.gov.ng`) | No machine-readable feed. HTTP 307 to a JavaScript wall; `/api/warnings` 302s. Nigeria is reached through SWIC instead. |
| **MeteoAlarm** | Europe only. 404 for Nigeria. |
| **GDACS JSON API** | `/gdacsapi/api/events/geteventlist/MAP` returned 400 for every parameter name tried. The RSS feed at `gdacs.org/xml/rss.xml` works unambiguously. |
| **Ushahidi** | Dormant (2 commits in six months) and licensed `NOASSERTION` — no grant of rights to rely on. |
