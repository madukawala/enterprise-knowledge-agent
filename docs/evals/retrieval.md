# Retrieval evaluation

- Date: 2026-11-21
- Questions: 18 (`evals/datasets/retrieval.yaml`)
- Embedding model: `multilingual-e5-large`
- Re-ranker: off (hybrid order only)
- k = 5, metrics at document level

| alpha | hit@5 | MRR | recall@5 |
|---|---|---|---|
| 0.3 | 1.00 | 0.93 | 0.94 |
| 0.5 | 1.00 | 0.93 | 0.94 |
| 0.7 | 1.00 | 0.93 | 0.94 |
| 0.9 (best) | 1.00 | 0.94 | 0.94 |

## Per question, alpha 0.9

| Question | Role | Expected | First expected at | Returned (top 3) |
|---|---|---|---|---|
| pool-exhaustion | viewer | RB-001 | 1 | RB-001, INC-2026-032, INC-2025-031 |
| certificate-renewal | analyst | RB-002, ARC-006 | 1 | RB-002, INC-2025-041 |
| instant-transfer-limit | viewer | PS-001 | 1 | PS-001, ARC-003, PS-002 |
| cut-off-times | viewer | POL-007 | 1 | POL-007, RB-006, RB-010 |
| sev1-status-page | viewer | POL-004, RB-009 | 1 | RB-009, POL-004 |
| canary-rule | viewer | POL-005, MTG-003 | 1 | MTG-003, POL-005, MTG-009 |
| incident-by-id | analyst | INC-2026-003, MTG-002 | 1 | MTG-002, INC-2026-009, INC-2026-021 |
| hsm-firmware | analyst | INC-2025-022 | 1 | INC-2025-022, RB-007, ARC-002 |
| degraded-mode | viewer | RB-003, ARC-003 | 1 | RB-003, ARC-003, PS-001 |
| kafka-lag | viewer | RB-004, ARC-005 | 1 | RB-004, ARC-005, INC-2026-030 |
| break-glass | admin | POL-006 | 3 | ARC-004, ARC-009, POL-006 |
| office-days | viewer | POL-009 | 1 | POL-009, MTG-010 |
| freeze-card | viewer | PS-006, PS-002 | 1 | PS-002, PS-006, MTG-008 |
| mobile-login | viewer | INC-2026-034 | 1 | INC-2026-034, ARC-008, INC-2026-030 |
| vendor-review | analyst | MTG-006 | 2 | INC-2026-038, MTG-006, POL-010 |
| config-rollback | viewer | RB-005 | 1 | RB-005, MTG-003, POL-005 |
| payroll-file | viewer | RB-010, INC-2026-026 | 1 | RB-010, INC-2026-026 |
| month-end-failures-filtered | analyst | INC-2026-021, INC-2026-032 | 1 | INC-2026-021, INC-2026-032 |
