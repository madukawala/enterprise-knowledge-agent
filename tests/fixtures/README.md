# Test fixtures

Small, made-up documents for the ingestion and search tests. They are kept
apart from the demo documents in `data/sample_docs/`, so editing the demo
data never breaks a test.

| Document | Access level | Used to check |
|---|---|---|
| `runbooks/RB-TEST-001.md` | internal | what every role can find |
| `incidents/INC-TEST-001.md` | confidential | Analysts and Administrators find it, Viewers do not; severity, tags and date filters |
| `policies/POL-TEST-001.md` | restricted | only Administrators find it, even when asked for by ID or by its own words |
