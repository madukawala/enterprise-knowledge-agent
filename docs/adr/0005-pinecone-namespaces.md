# ADR-0005: One namespace per document collection

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assignment requires Pinecone namespaces and metadata filtering. Namespaces could split the data by collection, department, access level or tenant. The choice affects how many queries a question needs and how permissions are enforced.

## Decision

- One namespace per **document collection** from the business scenario: `policies`, `architecture`, `runbooks`, `incidents`, `product-specs`, `meeting-notes`.
- **Department, date and access level are metadata filters** inside each namespace, not namespaces.
- The retrieval agent picks the namespaces that fit the question's intent (for example `incidents` and `runbooks` for an outage question) and queries them **in parallel** with `asyncio`. Results are merged by hybrid score; the scores are comparable because all namespaces live in the same index with the same encoders.

## Consequences

- **Positive:** collections match how bank staff think about documents; namespace choice cuts noise and query cost.
- **Positive:** permission filtering is one rule (`access_level` in the role's allowed levels) applied in every query, instead of choosing namespaces by role, which is easy to get wrong.
- **Positive:** RLM step 1 (explore collections) maps directly onto namespaces.
- **Negative:** a wrong namespace choice can miss a document. The agent falls back to searching all namespaces when the first pass returns too few results, and routing accuracy is part of the evaluation set.

## Alternatives considered

| Option | Why not |
|---|---|
| One namespace per access level | Security would depend on choosing the right namespace; a filter is simpler to test |
| One namespace per department | Many questions cross departments (payments incidents involve core banking) |
| A single namespace | Shows less of Pinecone; every query scans all collections |

## Related

[ADR-0003](0003-pinecone-single-hybrid-index.md) · [ADR-0007](0007-keycloak-rbac-and-data-access.md)
