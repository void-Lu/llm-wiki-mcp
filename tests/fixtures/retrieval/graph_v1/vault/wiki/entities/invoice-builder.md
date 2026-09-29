---
title: Invoice Builder
type: entity
generated: false
lifecycle: active
tags:
- billing
- service
sources:
- raw/sources/documents/ledger/billing-rfc/rfc.md
---

# Invoice Builder

Invoice Builder is the invoice assembly service. It turns shipment charges into invoice lines,
using prices returned by [[rate-engine]], and writes each posted invoice to [[audit-trail]].

## Rules

- Amounts follow [[money-decimal]].
- Invoice dates follow [[utc-timestamps]].
