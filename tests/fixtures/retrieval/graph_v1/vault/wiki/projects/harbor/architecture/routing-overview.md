---
title: Routing Overview
type: architecture
generated: false
lifecycle: active
project: harbor
sources:
- raw/sources/documents/harbor/routing-design-review/review.md
- raw/sources/documents/harbor/carrier-incident-2026-05/postmortem.md
---

# Routing Overview

Shipment requests enter [[parcel-router]], which consults [[rate-engine]] and [[lane-registry]],
then hands work to [[dispatch-queue]]. Labels are printed by [[carrier-gateway]].
