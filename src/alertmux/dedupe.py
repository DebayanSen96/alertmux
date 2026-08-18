"""Cross-source duplicate detection.

Measured 18 Aug 2026: SWIC's `us-noaa` slice and a direct NWS fetch overlap
completely (133/133 of SWIC's distinct (event, area_description) pairs also
appear in NWS's 148). NWS is the richer record — 32 source fields against 9.
This module surfaces that overlap so a consumer can choose the richer copy.

It never merges and never drops. `/alerts` keeps returning every record with
provenance intact, per principle 1 (relay, never issue) and D2 (identity is
load-bearing for any future notifier). Merging would mean picking whose
wording a user sees — editorialising hazard content. See DECISIONS.md D13.

A candidate group is only reported when every member comes from a
*different* `provenance.source_id` — a source's own ids are authoritative
about its own event distinctness, so two records from the same source
sharing a key mean the key is too coarse, not that the records are
duplicates. See `summarise_duplicates` and DECISIONS.md D13's addendum.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel

from alertmux.schema import NormalisedAlert

# The set of optional fields counted when ranking how "rich" a record is.
# Deliberately the same fields NormalisedAlert can legitimately omit — i.e.
# everything that is not `id`, `event`, or `provenance`, which are mandatory
# on every alert regardless of source.
_OPTIONAL_FIELDS = (
    "headline",
    "description",
    "instruction",
    "area_description",
    "severity",
    "urgency",
    "certainty",
    "source_severity",
    "source_urgency",
    "source_certainty",
    "sent",
    "onset",
    "expires",
    "geometry",
)


class DuplicateGroup(BaseModel):
    """A set of alert ids believed to describe the same hazard record.

    Purely a report: grouping never changes `AlertsResponse.alerts`.
    """

    key: str
    alert_ids: list[str]
    preferred_id: str
    reason: str


def _normalise(value: str) -> str:
    """Case-fold and collapse whitespace. Nothing more aggressive.

    Case and incidental whitespace differences (a trailing space, a double
    space from source formatting) are safe to treat as identical: they carry
    no hazard-relevant information. Anything beyond that — trimming
    punctuation, reordering words, fuzzy/edit-distance matching — could
    equate two records an authority actually meant to distinguish, which
    principle 2 (false negatives over false positives here) forbids.
    """
    return " ".join(value.casefold().split())


def event_key(alert: NormalisedAlert) -> str | None:
    """The authority-agnostic identity used to group duplicates.

    `event` + `area_description` is what empirically matches across SWIC
    and NWS for the same hazard. Returns None when the record cannot be
    keyed (no `area_description`) so it is excluded from grouping rather
    than being matched to other unkeyable records, which would group
    unrelated alerts on the absence of information rather than on shared
    content.
    """
    if not alert.event or not alert.area_description:
        return None
    event = _normalise(alert.event)
    area = _normalise(alert.area_description)
    if not event or not area:
        return None
    return f"{event}|{area}"


def _richness(alert: NormalisedAlert) -> int:
    """Count of optional schema fields this record has populated.

    The inverse of `unavailable_fields`: how many of the fields a source
    could have supplied actually came back non-None. Used only to pick the
    preferred record within a group that already exact-matched on identity
    — never to decide whether two records match in the first place.
    """
    return sum(
        1 for field in _OPTIONAL_FIELDS if getattr(alert, field) is not None
    )


def _bucket_by_key(
    alerts: list[NormalisedAlert],
) -> dict[str, list[NormalisedAlert]]:
    buckets: dict[str, list[NormalisedAlert]] = defaultdict(list)
    for alert in alerts:
        key = event_key(alert)
        if key is None:
            continue
        buckets[key].append(alert)
    return buckets


def summarise_duplicates(
    alerts: list[NormalisedAlert],
) -> tuple[list[DuplicateGroup], int]:
    """Group candidates that share an `event_key`, then apply the
    single-record-per-source rule.

    A source's own ids are authoritative about its own event distinctness
    (DECISIONS.md D13 addendum). If GDACS assigns two records different
    event ids, GDACS is stating they are different events, and this
    module must never overrule that by collapsing them onto a coarser
    key. Cross-source duplicate detection is only meaningful *across*
    sources — never within one.

    A key candidate group is therefore only reported as a
    `DuplicateGroup` when every member comes from a **different**
    `provenance.source_id`. If any source contributes two or more
    records to a candidate group, the key was too coarse for that
    source (the textbook case: GDACS's `area_description` is
    country-level, so 98 distinct Angola wildfires, each with its own
    `gdacs:eventid`, all keyed to `wildfire|angola`) and the *entire*
    group is discarded rather than reported as a false-positive pair.

    Discarding is not the same as silently dropping (principle 4): the
    second element of the returned tuple counts how many candidate
    groups were rejected this way, so an operator can see that N
    potential duplicates were found but could not be confidently
    paired across sources.

    Never merges or drops anything from the caller's alert list — the
    accepted groups are purely a report. Grouping requires exact
    agreement on the normalised key; no fuzzy or similarity-based
    matching is used anywhere in this function.
    """
    buckets = _bucket_by_key(alerts)

    groups: list[DuplicateGroup] = []
    ambiguous = 0
    for key in sorted(buckets):
        members = buckets[key]
        if len(members) < 2:
            continue

        source_ids = [m.provenance.source_id for m in members]
        if len(set(source_ids)) != len(source_ids):
            # At least one source contributed >1 record under this key,
            # so the source's own ids disagree with our key about
            # distinctness. That source's ids win: the whole candidate
            # group is too coarse to trust, and is discarded rather
            # than reported as a possibly-wrong pairing.
            ambiguous += 1
            continue

        # Rank by richness, tie-broken deterministically by id so output
        # is stable across runs regardless of adapter fetch order.
        ranked = sorted(members, key=lambda a: (-_richness(a), a.id))
        preferred = ranked[0]
        preferred_count = _richness(preferred)

        groups.append(
            DuplicateGroup(
                key=key,
                alert_ids=sorted(a.id for a in members),
                preferred_id=preferred.id,
                reason=(
                    f"{preferred_count} of {len(_OPTIONAL_FIELDS)} "
                    "optional fields populated"
                ),
            )
        )

    return groups, ambiguous


def group_duplicates(alerts: list[NormalisedAlert]) -> list[DuplicateGroup]:
    """Report groups of 2+ alerts that share an `event_key` AND come from
    mutually distinct sources. See `summarise_duplicates` for the full
    rule and its reasoning.
    """
    groups, _ = summarise_duplicates(alerts)
    return groups
