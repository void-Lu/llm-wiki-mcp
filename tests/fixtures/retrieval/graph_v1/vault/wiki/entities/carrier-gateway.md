---
title: Carrier Gateway
type: entity
generated: false
lifecycle: active
tags:
- integration
- carrier
---

# Carrier Gateway

Carrier Gateway is the adapter that prints shipping labels through each carrier's API.
Outbound calls pass through [[quota-keeper]] and are wrapped in a [[circuit-breaker]].
Credentials follow [[api-key-rotation]].

## Failure modes

Carrier outages surface as open breakers rather than hung requests.
