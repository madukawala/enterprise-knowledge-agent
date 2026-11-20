# Sample documents

`sample_docs/` holds 70 internal documents of **Helix Commercial Bank**, a fictional bank. Every person, system, supplier, incident and number in them is made up. They are the knowledge base the assistant searches.

`index/` is created by `make ingest` and is not committed. It holds the fitted BM25 parameters and the document catalog (see [ingestion/README.md](../ingestion/README.md)).

## Collections

Each folder is one collection and becomes one Pinecone namespace.

| Collection | Documents | What is in it | Access levels |
|---|---|---|---|
| `policies` | 10 | Security, data classification, incident severity, change management, privileged access, payment cut-offs, card fraud, remote working, vendor risk | public 2, internal 5, confidential 2, restricted 1 |
| `architecture` | 9 | PayHub, CardSwitch, the instant payments gateway, CoreLedger, event streaming, network segmentation, Sentinel fraud engine, channels, disaster recovery | internal 6, confidential 2, restricted 1 |
| `runbooks` | 10 | Step-by-step fixes for the problems that keep coming back (connection pool, certificates, FastLane timeouts, Kafka lag, bad config changes) and a few others | internal 8, confidential 1, restricted 1 |
| `incidents` | 22 | Incident reports from July 2025 to November 2026 | internal 10, confidential 11, restricted 1 |
| `product-specs` | 9 | Cards, instant transfers, bulk payments, standing orders, notifications, fraud scoring, interest | public 3, internal 4, confidential 1, restricted 1 |
| `meeting-notes` | 10 | Quarterly payments reliability reviews, a post-incident review, the change board, security steering, a vendor review, roadmap planning, an HR town hall | public 1, internal 6, confidential 2, restricted 1 |

Departments: `payments`, `cards`, `core-banking`, `security`, `hr`. Every access level is used, so the role filters visibly change what each test user can find.

## Document format

Each file is Markdown with YAML front matter. The file name is the `doc_id`.

```markdown
---
doc_id: INC-2026-003
title: Salary-day payment failures in PayHub
department: payments            # payments | cards | core-banking | security | hr
document_type: incident         # policy | architecture | runbook | incident | product_spec | meeting_notes
access_level: confidential      # public | internal | confidential | restricted
created_date: 2026-01-30        # for incidents, the day it happened
owner: Payments Platform        # optional
tags: [payhub, connection-pool] # optional
severity: SEV1                  # incidents only
---
# Salary-day payment failures in PayHub

## Summary
...
```

- The `# ` heading must match `title`.
- Use `##` and `###` headings for sections. Search results cite the section, so give sections clear names.
- Put a `title` that contains a colon in double quotes.
- `make ingest-check` checks every rule and lists all problems at once, without calling Pinecone.

## The story inside the documents

The documents describe one year of payment problems and how the bank fixed them. That gives the research example from the assignment, *"summarise payment-failure outages over the last year"*, a real answer that can be checked.

Between 21 November 2025 and 20 November 2026 there are **16 payment-failure incidents** (department `payments` or `cards`), with five causes that repeat:

| Cause | Incidents | How it was fixed |
|---|---|---|
| Expired or half-deployed mTLS certificates between CardSwitch and the Network Gateway | INC-2025-041, INC-2026-015, INC-2026-035 | Expiry monitoring, first for one site only; fully in place on both sites after the third incident (RB-002) |
| PayHub database connection pool running out at salary day and month-end | INC-2026-003, INC-2026-009, INC-2026-021, INC-2026-032 | An index, a larger pool (200 to 350), then a read replica for read-only queries (RB-001) |
| Timeouts and an outage at the FastLane scheme (run by NorthClear) | INC-2026-012, INC-2026-024, INC-2026-038 | A queue-and-retry degraded mode in the gateway, a longer retry window, a vendor review (RB-003, MTG-006) |
| Configuration changes made in production without a canary | INC-2026-007, INC-2026-027, INC-2026-043 | The change policy now requires a canary and a rollback plan (POL-005, RB-005) |
| Kafka consumer lag in the Payment Status Service, causing stuck statuses and duplicate payments | INC-2026-017, INC-2026-030, INC-2026-040 | Consumer autoscaling and idempotency keys (RB-004) |

Two older payment incidents (INC-2025-022 and INC-2025-031) fall outside the last 12 months, so a date filter visibly changes the answer. Four incidents are not payment failures (end-of-day batch, phishing, payroll file, mobile login).

The quarterly reliability reviews (MTG-001, MTG-004, MTG-007, MTG-009) track the same causes over time, and runbooks, policies and architecture documents refer back to the incidents. The evaluation set in `feature/observability-evals` uses this table as its expected answer.

## Things to know

- The documents use British spelling, as a UK-style bank would.
- The `restricted` documents (for example the Sentinel fraud rules) contain made-up thresholds that look sensitive on purpose, so the role checks have something real to protect.
- The tests check these numbers ([tests/unit/test_sample_corpus.py](../tests/unit/test_sample_corpus.py)). If you add or remove documents, keep 60–80 documents and 15–20 payment-failure incidents in the last year, or update the test and this file together.
