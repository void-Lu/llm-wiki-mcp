---
title: Export Retries Spec
type: spec
generated: false
lifecycle: active
project: ledger
status: accepted
---

# Export Retries Spec

A failed journal export batch is retried with the same batch identifier so the journal can drop
duplicates. See [[retry-budget]].
