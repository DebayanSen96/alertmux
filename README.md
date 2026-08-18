# alertmux

One API for natural-hazard alerts from multiple official sources, normalised
to a single schema with provenance intact.

**alertmux relays alerts published by official authorities. It never
originates a warning, and it is not a substitute for official warnings from
the issuing authority.**

## Sources

| Source | Coverage |
|---|---|
| WMO SWIC | 59 national alerting authorities, warnings currently in force |
| USGS | Global earthquakes, past hour |
| NOAA / NWS | United States, active alerts |
| GDACS | Global disaster alerts (earthquakes, floods, cyclones, drought, wildfire) |

## Install

```bash
pip install -e ".[dev]"
```

## Run

```bash
uvicorn alertmux.api:app --reload
```

- `GET /alerts` — all current alerts. Optional `?authority=ng-nimet`.
- `GET /health` — per-source health. Returns **HTTP 503** whenever the result
  is partial, so a standard monitor sees the degradation.

Any response where a source failed — or returned fewer alerts than it holds —
sets `partial: true`. Incomplete results are always labelled.

With `?authority=` applied, `sources[].alert_count` describes the **whole
fetch** and will not equal `len(alerts)`. Source health is about the fetch,
not the filter.

If `?authority=` matches nothing, the response also carries
`available_authorities` — the authorities present in this fetch — so a typo
is distinguishable from a genuinely quiet day.

Results are cached for 60 seconds, so polling `/health` does not repeatedly
pull ~774KB from WMO.

## An example alert

A real warning from the Nigerian Meteorological Agency, as alertmux returns it:

```json
{
  "id": "wmo-swic:ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml",
  "event": "THUNDERSTORMS",
  "headline": null,
  "description": null,
  "area_description": "Some states in Nigeria will be affected.",
  "severity": "Severe",
  "urgency": "Expected",
  "certainty": "Observed",
  "source_severity": "3",
  "source_urgency": "3",
  "source_certainty": "4",
  "sent": "2026-08-17T06:50:16Z",
  "onset": null,
  "expires": null,
  "geometry": null,
  "provenance": {
    "authority": "ng-nimet",
    "source_id": "wmo-swic",
    "source_url": "https://severeweather.wmo.int/g/wfs",
    "retrieved_at": "2026-08-17T23:39:03Z",
    "raw_reference": "ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml"
  },
  "unavailable_fields": [
    "description",
    "expires",
    "geometry",
    "headline",
    "onset"
  ]
}
```

Read it as: the authority stated a severity code of `3`, which is confirmed to
mean CAP `Severe`, so both are reported. It supplied no headline, description,
onset, expires or polygon — the SWIC list view carries none of them; they live
only in the raw CAP file at
`https://severeweather.wmo.int/v2/cap-alerts/<raw_reference>`. Every one of
those absences is named in `unavailable_fields`, which is exhaustive: if a
field is `null`, its name is in that list.

The `id` is derived from `raw_reference`, not from the GeoServer feature id —
GeoServer's synthetic fids embed the *request* timestamp and change on every
fetch, so they cannot be used as a deduplication key.

## Design rules

- **Nothing is inferred.** A field a source does not supply is `null`, and its
  name appears in `unavailable_fields`.
- **Only verified severity codes are translated.** SWIC's `s`/`u`/`c` are
  integers. The mapping was confirmed against raw CAP files, so `severity`,
  `urgency` and `certainty` carry proper CAP names — but any code that was
  never observed stays `null` rather than being guessed. The raw value is
  always preserved in `source_severity` / `source_urgency` /
  `source_certainty`, even when a mapping exists.
- **Malformed input fails loudly.** A feature missing an identity, an event
  type, or a timezone on its timestamp raises rather than being defaulted.
  The adapter turns that into `ok=false`, never into a plausible guess.
- **Adapters never raise outward.** A failing source returns a status, so one
  broken feed cannot take down a response.

## Running the tests

```bash
pytest          # the whole unit suite; no network, fixtures only
pytest -m live  # hits the real WMO SWIC, USGS, NOAA/NWS and GDACS endpoints
```

Live tests are excluded from the default run and from CI, so neither depends
on third-party uptime. A new adapter's live test belongs in
`tests/test_live_smoke.py`, under the `live` marker.

## Adding a source

1. Create `src/alertmux/adapters/yoursource.py` with a class exposing
   `source_id: str` and `fetch() -> FetchResult`. Copy `usgs.py` — it is the
   simplest example. Note that `FetchResult` requires `retrieved_at` and
   `latency_ms`; neither has a default.
2. Record a real payload to `tests/fixtures/yoursource_<feed>.json`, matching
   the existing naming (`swic_effective.json`, `usgs_all_hour.json`).
3. Write `tests/test_yoursource.py` against that fixture. No network in unit
   tests — use `respx` to mock `fetch()`.
4. Register it in `src/alertmux/adapters/__init__.py`. This is **three**
   edits, not one:

   ```python
   from alertmux.adapters.yoursource import YourSourceAdapter          # 1. import

   __all__ = ["SwicAdapter", "UsgsAdapter", "YourSourceAdapter",       # 2. __all__
              "default_adapters"]


   def default_adapters():
       return [SwicAdapter(), UsgsAdapter(), YourSourceAdapter()]      # 3. register
   ```

5. Add a live smoke test to `tests/test_live_smoke.py` under the `live` marker.

### The two rules your adapter must follow

1. **Never map an unverified code.** Translate a source's severity, urgency or
   certainty to a CAP name only after confirming that meaning against that
   source's own raw CAP output. Anything unconfirmed leaves the named field
   `None` and keeps the raw value in `source_*`. A guessed severity is either
   a missed warning or a false alarm — do not complete a partial table.
2. **Record every unsupplied field in `unavailable_fields`.** The list is
   exhaustive by contract: every `null` optional field must be named. Derive
   it from the values you actually built, as `swic.py` and `usgs.py` do,
   rather than appending entries by hand.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and commit style.

## Documentation

Deeper reference, kept in `docs/` and updated as the project grows:

| Document | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the modules fit together, and what each defensive check is guarding against |
| [`docs/DATA-SOURCES.md`](docs/DATA-SOURCES.md) | Every endpoint in detail — including WMO SWIC's undocumented WFS API, the 59 authority codes, and the confirmed CAP severity mapping |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why things are the way they are, what each choice costs if wrong, and what evidence would justify changing it |

If you are about to "fix" something that looks obviously wrong — particularly the
incomplete severity tables — read `docs/DECISIONS.md` first. It is probably
deliberate, and the entry will tell you what evidence would change our mind.

## Licence

MIT.
