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

## Install

```bash
pip install -e ".[dev]"
```

## Run

```bash
uvicorn alertmux.api:app --reload
```

- `GET /alerts` — all current alerts. Optional `?authority=ng-nimet`.
- `GET /health` — per-source health.

Any response where a source failed sets `partial: true`. Incomplete results
are always labelled.

## Design rules

- **Nothing is inferred.** A field a source does not supply is `null`, and its
  name appears in `unavailable_fields`.
- **Only verified severity codes are translated.** SWIC's `s`/`u`/`c` are
  integers. The mapping was confirmed against raw CAP files, so `severity`,
  `urgency` and `certainty` carry proper CAP names — but any code that was
  never observed stays `null` rather than being guessed. The raw value is
  always preserved in `source_severity` / `source_urgency` /
  `source_certainty`, even when a mapping exists.
- **Adapters never raise.** A failing source returns a status, so one broken
  feed cannot take down a response.

## Adding a source

Everything you need is in one file plus one line.

1. Create `src/alertmux/adapters/yoursource.py` with a class exposing
   `source_id: str` and `fetch() -> FetchResult`. Copy `usgs.py` — it is the
   simplest example.
2. Record a real payload to `tests/fixtures/yoursource.json`.
3. Write `tests/test_yoursource.py` against that fixture. No network in unit
   tests.
4. Add it to `default_adapters()` in `src/alertmux/adapters/__init__.py`.

That is the whole change. No existing file needs modifying beyond step 4.

## Licence

MIT.
