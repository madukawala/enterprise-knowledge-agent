---
doc_id: INC-TEST-001
title: Test card payment outage
department: cards
document_type: incident
access_level: confidential
created_date: 2026-03-02
owner: Cards Platform
tags: [cardswitch, certificate]
severity: SEV1
---
# Test card payment outage

Card payments were declined for 40 minutes.

## Root cause

The mTLS certificate on the network gateway expired at 09:00.

## Resolution

The certificate was renewed and CardSwitch was restarted at 09:40.
