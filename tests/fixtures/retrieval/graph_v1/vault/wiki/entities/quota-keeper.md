---
title: Quota Keeper
type: entity
generated: false
lifecycle: active
tags:
- integration
- throttling
sources:
- raw/sources/documents/harbor/carrier-incident-2026-05/postmortem.md
---

# Quota Keeper

Quota Keeper is the outbound call throttler: a token bucket per carrier account that keeps
external API usage under the contracted request ceiling. See [[rate-limiting]].

## Tuning

Bucket sizes are configured per carrier; bursts above the ceiling wait instead of failing.
