---
title: Label Print Timeouts
type: troubleshooting
generated: false
lifecycle: active
project: harbor
sources:
- raw/sources/documents/harbor/carrier-incident-2026-05/postmortem.md
status: resolved
---

# Label Print Timeouts

Symptom: label requests hang for thirty seconds and then fail during the morning peak.
Cause: the per-account bucket in [[quota-keeper]] was sized for last year's volume.
Fix: raise the bucket size and spread the depot batch over five minutes.
