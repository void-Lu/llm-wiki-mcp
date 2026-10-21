---
title: Error Envelope Rule
type: entity
generated: false
lifecycle: active
domain: shared-specs
tags:
- shared-spec
applies_to:
  frameworks:
  - fastapi
  languages:
  - python
conditions: Public HTTP APIs.
derived_from:
- project: harbor
  path: wiki/projects/harbor/specs/api-errors.md
  rule: error body
---

# Error Envelope Rule

Every HTTP failure body is a JSON object with `code`, `message` and `correlation_id`.
Codes are stable snake_case identifiers; messages are for humans and may change.
