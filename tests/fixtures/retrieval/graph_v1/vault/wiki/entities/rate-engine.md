---
title: Rate Engine
type: entity
generated: false
lifecycle: active
tags:
- pricing
- service
---

# Rate Engine

Rate Engine is the tariff calculator: it computes carrier tariffs from the tariff tables and adds the
fuel surcharge. Weight bands come from [[weight-classifier]] and the weekly diesel figure comes from
[[fuel-index-feed]]. Every night it freezes a [[tariff-snapshot]].

运价计算引擎：根据运价表计算承运商运费。

## Notes

- Tariff tables are versioned per carrier.
- Surcharge rules are described in [[fuel-surcharge]].
