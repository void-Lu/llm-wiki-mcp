---
title: Carrier Call Retries
type: spec
generated: false
lifecycle: active
project: harbor
status: accepted
related_objects:
- carrier-gateway
---

# Carrier Call Retries

Calls to carrier APIs are retried on timeouts and 5xx answers only, never on validation errors.
The shared limit is [[retry-budget]].
