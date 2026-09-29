---
title: Lane Selection Spec
type: spec
generated: false
lifecycle: active
project: harbor
status: accepted
sources:
- raw/sources/documents/harbor/routing-design-review/review.md
---

# Lane Selection Spec

The router must reject lanes whose cut-off has passed and must prefer the promised date over price
when both cannot be met. Implemented in [[parcel-router]].
