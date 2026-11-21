# ADR-0016: Guardrails as rules in code, personal data masked with patterns

- **Status:** Accepted
- **Date:** 2026-11-21

## Context

The plan named LLM Guard for prompt-injection detection and Microsoft Presidio for personal data masking. While building `feature/guardrails` we checked what they need on the laptop this project must run on (ADR-0001: localhost, free tools):

- LLM Guard's injection scanner runs a transformer model through PyTorch. The install is several gigabytes and the model needs about 1 GB of memory, next to Keycloak, Postgres, Redis and the API in Docker.
- Presidio's analyzer needs a spaCy language model for names and places (the small English model is a further download; the accurate one is about 500 MB).

Both would also make the API image much larger and slower to build and scan in CI.

## Decision

- **Injection detection is a set of rules in code** ([security/guardrails/injection.py](../../security/guardrails/injection.py)): six categories (instruction override, prompt leak, role escalation, data exfiltration, tool abuse, fake delimiters), run on a normalised form of the text that undoes full-width letters, invisible characters, look-alike letters, leetspeak and base64. The same rules scan the question and every retrieved chunk.
- **Personal data is masked with patterns and checksums** ([core/pii.py](../../core/pii.py)): e-mail, card numbers (Luhn), IBAN (mod-97), UK sort code and account, UK National Insurance numbers, phone numbers, IP addresses and API keys or tokens. It runs on answers, the streamed preview, every log line and every LangSmith trace.
- **Both sit behind small functions** (`scan(text)`, `mask(text)`), so a model-based scanner can be added later without touching the agents.
- The approach and its layers are written up in [PROMPT_INJECTION.md](../security/PROMPT_INJECTION.md).

## Consequences

- **Positive:** no extra models or downloads; the image stays small; scanning takes well under a millisecond, so it adds nothing to the time a user waits.
- **Positive:** every rule is readable and tested, including against all 564 chunks of the sample documents for false positives.
- **Negative:** rules only catch known shapes of attack. New phrasings get past them. This is acceptable because the role, the document filter and the tool permissions are enforced in code (ADR-0007) and do not depend on the scan.
- **Negative:** names and postal addresses are not recognised.
- **Production:** add LLM Guard (or a hosted classifier) as a second opinion on questions and chunks, and Presidio with a spaCy model for names and addresses, on a machine with the memory for them.

## Alternatives considered

| Option | Why not, here |
|---|---|
| LLM Guard prompt-injection scanner | Several GB of PyTorch and model files; about 1 GB of memory |
| Presidio with spaCy | Language model download; names and addresses are not in the fictional sample data |
| Asking the language model whether a question is an attack | One more model call per question on a free tier with daily limits; the judge can itself be injected |
| A hosted moderation API | Sends questions to another provider; adds a dependency the assignment does not need |

## Related

[ADR-0007](0007-keycloak-rbac-and-data-access.md) · [THREAT_MODEL.md](../security/THREAT_MODEL.md) · [ASSUMPTIONS.md](../ASSUMPTIONS.md) S10
