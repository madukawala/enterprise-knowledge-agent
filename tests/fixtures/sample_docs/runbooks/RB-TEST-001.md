---
doc_id: RB-TEST-001
title: Test connection pool runbook
department: payments
document_type: runbook
access_level: internal
created_date: 2026-01-15
---
# Test connection pool runbook

## Symptoms and alerts

PayHub returns timeouts and the payhub-db pool shows 200 of 200 connections in use.

## Steps to fix

1. Check the slow query dashboard.
2. Raise the pool size for the busy pod group.
