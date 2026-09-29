---
title: UTC Timestamp Rule
type: entity
generated: false
lifecycle: active
domain: shared-specs
tags:
- shared-spec
applies_to:
  languages:
  - go
  - python
conditions: Stored or exchanged instants.
derived_from:
- project: ledger
  path: wiki/projects/ledger/specs/invoice-dates.md
  rule: utc storage
---

# UTC Timestamp Rule

Persist instants as ISO-8601 strings in UTC with an explicit `Z`; convert to local time only for display.

## Origins

- [[projects/ledger/specs/invoice-dates]]
