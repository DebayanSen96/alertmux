# alertmux

Multi-source natural-hazard alert normaliser.

Fetches natural-hazard alerts from official sources and normalises them into
one schema (`alertmux.schema.NormalisedAlert`). Fields a source does not
supply are `None`, never inferred or defaulted; the omission is recorded in
`unavailable_fields`.
