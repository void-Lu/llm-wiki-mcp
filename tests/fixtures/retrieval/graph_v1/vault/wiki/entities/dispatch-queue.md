---
title: Dispatch Queue
type: entity
generated: false
lifecycle: active
tags:
- routing
- queue
---

# Dispatch Queue

Dispatch Queue is the durable pickup job queue. Each job carries an [[idempotency-key]] so that a
retried enqueue never creates a second pickup. Jobs that keep failing move to the
[[dead-letter-queue]].

## Consumers

Pickup drivers' handhelds poll the queue; status changes are published as events.
