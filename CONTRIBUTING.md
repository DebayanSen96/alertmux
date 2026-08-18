# Contributing to alertmux

alertmux relays hazard alerts published by official authorities. Everything
below exists to protect two properties: the text we relay is exactly what the
authority published, and a value we did not receive is never invented.

## Before you write code

- **Open an issue before a significant change** — a new source, a schema
  change, or anything touching severity mapping. It is much cheaper to agree
  on the approach than to rework a PR.
- **A bug fix with a test can skip the discussion.** Open the PR directly.
- **Small PRs get reviewed fast.** One coherent change per PR.

## The two rules a hazard adapter must follow

1. **Never map an unverified code.** A source's severity/urgency/certainty
   codes are only translated to CAP names after being confirmed against that
   source's raw CAP files. An unconfirmed code leaves the named field `None`
   and keeps the raw value in `source_severity` / `source_urgency` /
   `source_certainty`. A guessed severity is either a missed warning or a
   false alarm. Do not "complete" a partial mapping table.
2. **Record every unsupplied field in `unavailable_fields`.** The list is a
   contract: it is exhaustive. Derive it from the values you actually built,
   as `swic.py` and `usgs.py` do, rather than appending by hand.

Related: never default a missing value (`or "earthquake"`, `or "unknown"`).
For hazard data, raising a `ValueError` the adapter turns into `ok=False`
beats quietly inventing a fact.

## Commits

Conventional commits, one coherent change per commit:

```
feat(adapters): add Environment Canada adapter
fix(schema): ...
docs: ...
test: ...
ci: ...
```

## Tests

```bash
pytest          # the unit suite; no network
pytest -m live  # hits the real WMO and USGS endpoints
```

CI runs `pytest` only, on Python 3.11, 3.12 and 3.13. It never runs the live
suite — CI must not depend on third-party uptime.

## Labels

- `good first issue` — self-contained, no deep context needed.
- `help wanted` — we would like help here.
- `new source` — adding an alerting authority or feed.
