---
title: Duplicate Journal Entries
type: troubleshooting
generated: false
lifecycle: active
project: ledger
sources:
- raw/sources/documents/ledger/journal-incident/postmortem.md
status: resolved
---

# Duplicate Journal Entries

Symptom: the same journal lines appear twice after an exporter restart.
Cause: the batch identifier was regenerated on retry. Fix: persist it before sending.
