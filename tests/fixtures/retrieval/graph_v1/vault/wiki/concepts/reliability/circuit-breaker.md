---
title: Circuit Breaker
type: concept
generated: false
lifecycle: active
domain: reliability
tags:
- reliability
---

# Circuit Breaker

The circuit breaker pattern stops calling a failing dependency for a cool-down period after an
error threshold is crossed, then probes with a single trial request.
