---
title: Money Minor Units Rule
type: entity
generated: false
lifecycle: active
domain: shared-specs
tags:
- shared-spec
sources:
- raw/sources/documents/ledger/billing-rfc/rfc.md
applies_to:
  languages:
  - go
  - python
conditions: Any stored amount.
derived_from:
- project: ledger
  path: wiki/projects/ledger/specs/invoice-totals.md
  rule: minor units
---

# Money Minor Units Rule

Monetary amounts are integers in minor units (cents) plus an ISO currency code; never binary floats.

## Origins

- [[projects/ledger/specs/invoice-totals]]
