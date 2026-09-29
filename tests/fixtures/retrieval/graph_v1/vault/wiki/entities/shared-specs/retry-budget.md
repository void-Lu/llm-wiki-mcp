---
title: Retry Budget Rule
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
conditions: Any outbound network call.
derived_from:
- project: harbor
  path: wiki/projects/harbor/specs/carrier-call-retries.md
  rule: carrier retries
- project: ledger
  path: wiki/projects/ledger/specs/export-retries.md
  rule: export retries
---

# Retry Budget Rule

At most three attempts per logical operation, with jittered [[exponential-backoff]] between them.
A caller that exhausts the budget must surface the failure instead of looping.

## Origins

- [[projects/harbor/specs/carrier-call-retries]]
- [[projects/ledger/specs/export-retries]]
