# ADR-0015: Rate limiting with Redis token buckets and a daily token budget

- **Status:** Accepted
- **Date:** 2026-11-21

## Context

The assignment asks for rate limiting that is per user, configurable and fails gracefully. The real cost of a question is the language model: the free tiers allow a limited number of requests and tokens per day, shared by everyone using the assistant. A user who asks very quickly, or who asks very long questions all day, can use up the quota for everybody. The API may run with more than one worker, so counts kept in one process are not enough.

## Decision

- **Two limits per question**, both checked before the agents run (`security/rate_limit.py`):
  1. **Questions per minute**, as token buckets: one bucket per user, sized by role (viewer 10, analyst 20, admin 40 per minute), and one bucket shared by every user of a role (120 per minute). A user may send 5 questions back to back, then one more each time the bucket refills. A question takes one from both buckets or from neither.
  2. **A daily budget of language model tokens per user**, by role (viewer 100,000, analyst 250,000, admin 500,000), reset at 00:00 UTC. Each question reserves 4,000 tokens before it runs, and the reservation is replaced by the real count from the final `response_end` event. A question the user abandons keeps its reservation, so leaving early never dodges the budget. A question refused for another reason (404, 409) gives its reservation back.
- **Redis holds the counts.** The bucket update is one Lua script, which Redis runs atomically, so two requests at the same moment cannot both take the last question. The budget uses `INCRBY` with an expiry. Keys use the user's pseudonym, never the user ID.
- **429 with `Retry-After`**, and a message that says how long to wait in words ("Please wait 10 seconds", "It resets at 00:00 UTC, in 5 hours 12 minutes"). The UI shows that message and today's usage in the sidebar.
- **Admins change limits while the API runs** (`PUT /admin/rate-limits/{role}`, permission `admin_rate_limits`). The change is kept in Redis, so every worker uses it within 5 seconds. `DELETE` goes back to the settings.
- **If Redis is down**, each worker keeps its own counts in memory with every limit halved (`RATE_LIMIT_FALLBACK_FACTOR`), logs one warning a minute and sets the `rate_limit_degraded` metric. Questions keep working. Admin changes are refused with 503 until Redis is back, so workers never disagree.
- Every 429 is logged on the `security` channel and counted in `rate_limit_rejections_total{limit, role}`.

## Consequences

- **Positive:** one user cannot use up the shared free quota; the limits hold across workers; a Redis outage does not stop the assistant.
- **Positive:** the budget counts real model tokens, so a short question costs less than a long research question.
- **Negative:** during a Redis outage the limits are per worker; with two workers a user could get up to the full limit. Halving the limits keeps that bounded.
- **Negative:** the reservation is an estimate. A user close to the budget may be refused a question that would just have fitted.
- Only `POST /chat` is limited. The other endpoints are cheap and need a login already.

## Alternatives considered

| Option | Why not |
|---|---|
| Fixed window counter (requests per calendar minute) | Allows twice the limit around the window edge; a token bucket smooths bursts |
| `slowapi` or another limiter library | Limits per IP or route, not per user and role from our token; no token budget; one more dependency for a short script |
| Counting tokens only after the answer ends | Leaving before the end would dodge the budget |
| Refusing every question while Redis is down | Turns a cache outage into a full outage |

## Related

[ASSUMPTIONS.md](../ASSUMPTIONS.md) S3, S4 · [THREAT_MODEL.md](../security/THREAT_MODEL.md) · [security/README.md](../../security/README.md#rate-limiting)
