---
title: Notification Hub
type: entity
generated: false
lifecycle: active
tags:
- messaging
- service
sources:
- raw/sources/documents/beacon/messaging-kickoff/kickoff.md
---

# Notification Hub

Notification Hub sends customer emails and text messages. It subscribes to status events from
[[dispatch-queue]] and to payment events from [[ledger-sync]], then fans them out per channel.

## Channels

Email and SMS today; push is planned.
