# Architecture

How alertmux is put together and why each part is shaped the way it is.

> **Keeping this current:** when you add a module or change a boundary, update the
> map below and the relevant section. When you add a *defence* — a guard against a
> specific real-world failure — record what failure it guards, with evidence. A
> guard whose reason is forgotten is a guard someone deletes.

## The shape

```
official feeds ──> adapters ──> NormalisedAlert[] ──> query ──> api
                   swic.py                            collect   /alerts
                   usgs.py                                      /health
                                                                 /sources
```

One direction. No writes to any external system. No state except a 60-second cache.

| Module | Responsibility |
|---|---|
| `schema.py` | `NormalisedAlert`, `Provenance`, `DISCLAIMER` — the contract |
| `adapters/base.py` | `FetchResult`, the `Adapter` protocol |
| `adapters/swic.py` | WMO SWIC — 59 national alerting authorities |
| `adapters/usgs.py` | USGS earthquakes |
| `adapters/__init__.py` | Adapter registry — `default_adapters()` |
| `query.py` | Aggregation, partial-result labelling |
| `dedupe.py` | Cross-source duplicate reporting — never merges or drops |
| `sources.py` | `/sources` report: structural gaps, authorities seen, heuristic hazard coverage |
| `api.py` | FastAPI, TTL cache, HTTP status semantics |

An adapter knows its own source's quirks and **nothing** about any other adapter,
the query layer, or the API. That isolation is what makes adding a feed a one-file
change, and it is the property to protect above convenience.

## schema.py — the contract

`Provenance` is mandatory on every alert and has no default, so pydantic rejects any
alert that lacks it. It is not possible to construct hazard data in this system
without recording where it came from.

The central pairing in `NormalisedAlert`:

```python
severity: str | None = None          # our interpretation — "Severe"
source_severity: str | None = None   # what the source said — "3"
```

**Both always travel together.** The named CAP level is an interpretation; the raw
value is the authority's own words. If a mapping is ever wrong, the truth is still
in the response and a consumer can recover. This is why a mapping error here is
correctable rather than silent corruption.

`unavailable_fields` exists because `expires: null` is ambiguous — it could mean the
alert never expires, or that the source did not say. Naming the gap removes the
ambiguity. The README states this list is *exhaustive*; that promise is enforced
mechanically (see below), not by remembering to append.

## adapters/base.py — adapters never raise

A source being down is **data**, not an exception:

```python
class FetchResult(BaseModel):
    source_id: str
    ok: bool
    alerts: list[NormalisedAlert] = []
    error: str | None = None
    truncated: bool = False
    matched: int | None = None
    returned: int | None = None
```

`fetch()` catches broadly and returns `ok=False`. This is what stops one broken feed
taking down the whole response.

`truncated` is deliberately separate from `ok`: the source answered correctly and the
alerts present are real, but there were more. A right answer that is incomplete.

## adapters/swic.py — three defences

Each guards a failure that actually occurred during development. See
`DATA-SOURCES.md` for the evidence and `DECISIONS.md` for the reasoning.

**1. A 200 is not necessarily success.**

```python
def _require_feature_collection(payload: dict) -> None:
    if payload.get("type") != "FeatureCollection" or "features" not in payload:
        raise ValueError(...)
```

GeoServer answers a bad `cql_filter`, an unknown `typeName`, or backend trouble with
an OWS exception report at **HTTP 200** and no `features` key. Without this guard,
`payload.get("features", [])` yields zero alerts and every source reports healthy —
the system would say "no hazards anywhere on Earth" while completely broken.

**2. Identity comes from content, not from the server.**

```python
capurl = props.get("capurl")
if not capurl:
    raise ValueError("...the synthetic GeoServer fid is not a stable identity")
id=f"{self.source_id}:{capurl}"
```

GeoServer's `feature["id"]` embeds a *request* timestamp, so it changes on every
fetch. `capurl` identifies the CAP file itself and is stable. Deduplication — and
therefore any future notifier — rests entirely on this.

**3. Refuse to assume a timezone.**

```python
if parsed.tzinfo is None:
    raise ValueError(f"SWIC {field} {value!r} has no UTC offset; refusing to assume one")
```

`astimezone()` on a naive datetime silently assumes the *server's* local time. The
same deployment would produce different hazard timestamps in Lagos and London.

**Exhaustiveness is derived, not remembered.**

```python
structural = ("onset", "expires", "headline", "description")
unavailable = sorted(
    set(structural) | {k for k, v in fields.items() if v is None}
)
```

`structural` names what this feed never supplies; the set comprehension catches
everything that came back `None` for any other reason. Add a field to the schema and
it is covered automatically. Hand-maintained lists drift; this one cannot.

## query.py — one line carries the safety property

```python
partial = any((not s.ok) or s.truncated for s in statuses)
```

Failed **or** truncated. Both mean the answer must not be presented as complete.

```python
source_id=getattr(adapter, "source_id", "unknown")
```

The failure handler cannot itself fail on an adapter so broken it lacks a
`source_id`. Handling failure with code that can fail is how a degraded service
becomes a dead one.

## dedupe.py — reports, never edits

```python
def event_key(alert: NormalisedAlert) -> str | None: ...
def group_duplicates(alerts: list[NormalisedAlert]) -> list[DuplicateGroup]: ...
```

The key is `event` + `area_description`, case-folded and whitespace-collapsed
and nothing more — that pair is what actually matches SWIC's `us-noaa` slice
against a direct NWS fetch (see DATA-SOURCES.md's "Source overlap" section
and DECISIONS.md D13). A record with no `area_description` keys to `None`
and is excluded from grouping rather than matched to other unkeyable
records.

Grouping requires exact key agreement — no fuzzy or similarity matching
anywhere in this module, because wrongly grouping two distinct hazards is
the failure mode a future notifier (v0.5) cannot afford. Within a group,
`preferred_id` is the record with the most optional schema fields populated
(ties broken by `id` for determinism), and `AlertsResponse.alerts` is
untouched either way — `collect()` calls `summarise_duplicates()` purely to
populate `duplicate_groups` (and `ambiguous_duplicate_groups`) alongside the
full, unfiltered alert list.

**A key match alone is not enough to report a group.** Every member of a
candidate group must also come from a different `provenance.source_id` — a
source's own ids are authoritative about its own event distinctness, so
two records from one source sharing a key (98 separate GDACS wildfires all
keyed `wildfire|angola`, since `gdacs:country` is country-level — see
DATA-SOURCES.md) mean the key is too coarse for that source, not that the
records are duplicates. Such a candidate group is discarded entirely, and
counted in `ambiguous_duplicate_groups` rather than silently dropped
(principle 4). See DECISIONS.md D13's 18 Aug 2026 addendum for the
measurement that forced this rule.

## sources.py — coverage, not health

`/health` answers "is it working." `/sources` answers "what does this system
actually cover, and what does it miss" — a different question aimed at a
human deciding whether to rely on it for a given hazard or region, not a
monitor.

```python
class SourceSummary(BaseModel):
    source_id: str
    endpoint: str
    authorities: list[str]       # observed THIS fetch, not declared
    authority_count: int
    structural_gaps: list[str]   # from the adapter's STRUCTURAL_GAPS
    ok: bool
    alert_count: int
    latency_ms: int | None
    truncated: bool
```

**`structural_gaps` is reportable without a fetch.** Each adapter's
`structural` tuple — fields that feed structurally never supplies — was a
local inside `parse()`, invisible outside a live run. It is now a class
attribute, `STRUCTURAL_GAPS`, on all five adapters (`NwsAdapter.STRUCTURAL_GAPS`
is `()` — NWS is the one source with no structural gaps, which is correct and
meaningful, not an oversight). `parse()` still unions it with whatever came
back `None` on a given record; `/sources` reads the class attribute directly.

**`authorities` is measured, not declared.** It comes from
`{a.provenance.authority for a in <this source's alerts from this fetch>}`
— a source that is down or genuinely quiet this fetch reports an empty list,
even if it normally carries 59.

**`hazard_coverage` is the endpoint's reason to exist.**

```python
hazard_coverage: dict[str, list[str]]   # family -> source_ids that contributed
uncovered_hazards: list[str]            # families with zero contributing sources
```

Measured 18 Aug 2026: tsunami had zero contributing sources and volcano had
one, while the authority count read 59/300 and looked healthy by itself. An
authority count alone hides a missing hazard family; `hazard_coverage` does
not let that gap disappear into a healthy-looking total.

**Hazard classification is heuristic and says so.** `classify_hazard` maps
each alert's free-text `event` field to a family via an explicit keyword
table, `HAZARD_KEYWORDS`. Wording varies by authority — "THUNDERSTORMS" vs
"Heat Advisory" vs "Wildfire" — so this *will* misfile some alerts. It is
documented in the module docstring and the `/sources` endpoint docstring,
and — critically — it **never writes back to `NormalisedAlert`**; it only
builds this one summary. Extending the keyword table follows the same
evidence bar as DECISIONS.md D1: a real observed `event` string, not a guess.

**Read-only over the shared cache.** `build_sources_response()` takes an
already-collected `AlertsResponse` and never mutates it or anything
reachable from it; `/sources` calls `_collect_shared()`, the same
non-deep-copying path `/health` uses, per D9.

## api.py — three things worth knowing

**The cache hands out deep copies.**

```python
return cached[1].model_copy(deep=True)
```

`/alerts` filters `response.alerts` in place. The mutation was safe while the object
was per-request; adding a cache made it shared, at which point one request's filter
would poison the next. A fix in one place invalidated a decision made in another —
worth remembering when adding state.

**An unknown authority is not a quiet day.**

```python
if not matching:
    response.available_authorities = sorted({a.provenance.authority for a in response.alerts})
```

A typo returns `[]`, which in this domain reads as "that country has issued no
warnings" — a dangerous false negative. Naming the authorities that answered lets a
caller tell a typo from genuine quiet.

**Degraded service is visible at the status line.**

```python
if result.partial:
    return JSONResponse(status_code=503, content=body)
```

`200 OK` carrying `{"ok": false}` reads green to every standard monitor.

## What is deliberately absent

No notifications, no alert issuing, no accounts, no database, no frontend. v0.1 reads
official feeds and normalises them. The boundary is legal as well as architectural —
see `DECISIONS.md`.
