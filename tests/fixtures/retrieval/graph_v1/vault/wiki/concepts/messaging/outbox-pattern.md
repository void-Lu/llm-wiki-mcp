---
title: Outbox Pattern
type: concept
generated: false
lifecycle: active
domain: messaging
tags:
- messaging
---

# Outbox Pattern

The outbox pattern writes outgoing messages to a table in the same transaction as the business
change; a relay publishes them afterwards, giving transactional messaging without two-phase commit.
