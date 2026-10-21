---
title: Ledger Sync
type: entity
generated: false
lifecycle: active
tags:
- billing
- integration
sources:
- raw/sources/documents/ledger/journal-incident/postmortem.md
---

# Ledger Sync

Ledger Sync is the accounting journal exporter: it reads posted invoices from [[invoice-builder]]
and exports balanced journal lines. Every export batch is recorded in [[audit-trail]].
It relies on the [[outbox-pattern]] and books revenue according to [[revenue-recognition]].

## Operations

Exports run hourly; a failed batch is retried under [[projects/ledger/specs/export-retries]].
