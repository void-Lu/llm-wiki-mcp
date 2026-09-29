---
title: Parcel Router
type: entity
generated: false
lifecycle: active
tags:
- routing
- service
sources:
- raw/sources/documents/harbor/routing-design-review/review.md
related_objects:
- rate-engine
- lane-registry
---

# Parcel Router

Parcel Router assigns every shipment to a lane before pickup. It asks [[rate-engine]] for a quote,
reads open lanes from [[lane-registry]], and enqueues the chosen pickup on [[dispatch-queue]].
Label requests go through [[carrier-gateway]]; street addresses are cleaned by [[address-validator]].

## Behaviour

- Picks the cheapest lane that still meets the promised delivery date.
- See [[lane-assignment]] for the selection heuristic.
