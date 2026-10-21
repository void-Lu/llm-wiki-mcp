---
title: Billing Pipeline
type: architecture
generated: false
lifecycle: active
project: ledger
sources:
- raw/sources/documents/ledger/billing-rfc/rfc.md
---

# Billing Pipeline

Charges flow from [[invoice-builder]] to [[ledger-sync]]; both append to [[audit-trail]].
