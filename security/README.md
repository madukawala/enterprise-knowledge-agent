# security

Who the user is, and what they may do. Everything that decides access lives here, so there is exactly one place to read and one place to change.

| Module | What it does |
|---|---|
| [roles.py](roles.py) | The three roles (`viewer`, `analyst`, `admin`) and the document access levels each may read |
| [policies.py](policies.py) | The role matrix: what each role may do. Deny by default |
| [auth.py](auth.py) | Checks a Keycloak access token and returns the signed-in user (`Principal`) |
| [rate_limit.py](rate_limit.py) | Questions per minute and the daily token budget, with the counts in Redis |
| [guardrails/](guardrails/__init__.py) | Prompt-injection rules, cleaning of retrieved text, and answer checks ([PROMPT_INJECTION.md](../docs/security/PROMPT_INJECTION.md)) |

Personal data masking is in [core/pii.py](../core/pii.py), because the logging in `core` uses it too.

## The role matrix

| Action | Viewer | Analyst | Admin |
|---|---|---|---|
| Chat | Yes | Yes | Yes |
| Knowledge search | Yes | Yes | Yes |
| Python analysis tool | No | Yes | Yes |
| Enterprise data tools (MCP) | No | Yes | Yes |
| Deep research | No | Yes | Yes |
| Admin: access matrix | No | No | Yes |
| Admin: audit log | No | No | Yes |
| Admin: rate limits | No | No | Yes |
| Admin: all feedback | No | No | Yes |
| Admin: re-index documents | No | No | Yes |
| Admin: memory clean-up | No | No | Yes |
| **Documents it can read** | public, internal | + confidential | + restricted |

Rules that keep it honest:

- **Deny by default.** An action that is not listed for a role is refused. A new `Action` is refused for everyone until it is added to the matrix on purpose, and `tests/unit/test_policies.py` compares the matrix with a table written out by hand, so a change in one place without the other fails the build.
- **One source.** Code asks `is_allowed(role, action)` or `require(role, action)`. No route, agent or tool keeps its own list of roles. An admin can see the live table at `GET /admin/access-matrix`.
- **Code decides, never the model.** The supervisor's intent is checked against the matrix after the model has chosen it. If the role cannot use what the intent needs (deep research, the enterprise data tools), the question runs as a plain document question and the answer says why. The search filter comes from the role too, so a document above the user's level never reaches the prompt.
- **Every refusal is logged** on the `security` log channel (`access_denied`, `token_rejected`, `tool_not_offered`), with the user's pseudonym and role.

## How a token is checked

The API receives `Authorization: Bearer <access token>` on every request (see [api/identity.py](../api/identity.py)). `TokenVerifier.verify()` accepts it only if all of these hold:

1. It is shorter than 8 KB and has a readable header.
2. The header says RS256. `none`, HS256 and every other algorithm are refused before the signature is looked at.
3. The header has a key ID (`kid`) that Keycloak publishes.
4. The signature is valid for that key.
5. The issuer is the realm (`AUTH_ISSUER`), the audience includes this API (`AUTH_AUDIENCE`), and it has not expired (30 seconds of clock skew allowed). `exp`, `iat`, `sub`, `iss` and `aud` must be present.
6. `typ` is `Bearer`: ID tokens and refresh tokens are signed by the same key but do not open the API.
7. It carries one of our realm roles. With several, the highest wins. With none, the user gets 403.

A failed check always gives the same 401, "You need to sign in again.", so the response tells an attacker nothing. The reason (`expired`, `wrong_audience`, `unknown_key` ...) goes to the security log as a `token_rejected` line; the token itself is never logged. A request with no token at all is logged the same way, with the reason `missing` (no `Authorization` header) or `no_bearer_token` (another scheme, such as Basic, or `Bearer` with nothing after it), so probing without a token shows up too.

### Keycloak's keys

`JwksClient` fetches the realm's public keys from Keycloak (`KEYCLOAK_URL`) on first use and keeps them for 5 minutes. A token with a key ID the client has not seen makes it fetch the keys again straight away, so a key rotation needs no restart, but at most once every 30 seconds, so tokens with made-up key IDs cannot flood Keycloak. If Keycloak is down, keys already in memory keep working; with no keys at all the API answers 503 (`auth_unavailable`) rather than 401, so the UI does not sign users out for a Keycloak restart.

Keycloak publishes an encryption key next to its signing key. Only signing keys with an allowed algorithm are kept.

### Pseudonyms

Logs and LangSmith traces name a user by `u_` plus the first 16 hex characters of a SHA-256 of their user ID. The same user always gets the same pseudonym, so their requests can be followed, but the logs never hold a username, name or e-mail. A real deployment would use a keyed hash with a secret (see [ASSUMPTIONS](../docs/ASSUMPTIONS.md), I8).

## Rate limiting

Two limits are checked before every question ([ADR-0015](../docs/adr/0015-rate-limiting.md)):

| Limit | Viewer | Analyst | Admin |
|---|---|---|---|
| Questions per minute, per user (after a burst of 5) | 10 | 20 | 40 |
| Questions per minute, all users of the role together | 120 | 120 | 120 |
| Model tokens per user per day (UTC) | 100,000 | 250,000 | 500,000 |

How it works:

- **Token buckets.** A bucket holds up to `burst` questions and refills at the role's rate. A question takes one from the user's bucket and one from the role's bucket, or from neither. In Redis this is one Lua script, which Redis runs as a single step, so two requests at the same moment never both get the last question.
- **Budget.** A question reserves 4,000 tokens before it runs. When the answer ends, the reservation is replaced by the real count from `response_end`. Leaving early keeps the reservation; a question refused with 404 or 409 gets it back.
- **429.** The response has `Retry-After` (seconds), `details.limit` (`requests` or `daily_tokens`) and a message in words: "Please wait 10 seconds and try again." or "It resets at 00:00 UTC, in 5 hours 12 minutes."
- **Admin changes** (`PUT /admin/rate-limits/{role}`) are stored in Redis and picked up by every worker within 5 seconds. `DELETE` goes back to the `.env` values.
- **Redis down.** Each worker carries on with its own counts in memory and every limit halved, logs `rate_limit_store_unavailable` at most once a minute, and sets `rate_limit_degraded` to 1. Admin changes get 503 until Redis is back. Without `REDIS_URL` at all (`make run-api`), memory is the normal store and the full limits apply.
- Keys use the user's pseudonym: `ratelimit:user:<pseudonym>`, `ratelimit:role:<role>`, `ratelimit:tokens:<pseudonym>:<date>`.

## The Keycloak realm

[infra/keycloak/realm-helix.json](../infra/keycloak/realm-helix.json) is imported when Keycloak first starts. It sets:

- the three realm roles and one demo user per role (passwords in the main [README](../README.md#test-users));
- the public client `enterprise-knowledge-ui`, with PKCE required for the browser flow and the password grant enabled for the demo UI;
- an audience mapper that adds `enterprise-knowledge-api` to every access token, which the API requires;
- 5-minute access tokens, 30-minute idle sessions, and brute-force protection (5 failed attempts).

Because the realm is only imported on first start, a change to the file needs `make down-volumes` and `make up` to take effect.

## Tests

| Test | What it proves |
|---|---|
| `tests/unit/test_policies.py` | The matrix matches the documented table; deny by default; refusals are logged |
| `tests/unit/test_jwks_client.py` | The key cache, key rotation, the refetch limit, Keycloak being down |
| `tests/security/test_tokens.py` | 16 kinds of bad token are refused, on their own and through the API, with the same 401 and no token in the log |
| `tests/security/test_rbac_matrix.py` | Role × route (every route must be listed), role × tool, role × data, and conversations owned by users, not roles |
| `tests/e2e/test_keycloak_login.py` | Real tokens from the local Keycloak pass the same checks (`make test-keycloak`) |
| `tests/unit/test_rate_limit.py` | Bucket maths with a fake clock; per-user and per-role buckets; the budget reserved, settled, refunded and reset at midnight |
| `tests/integration/test_rate_limit_redis.py` | The real Lua script; 20 simultaneous questions; two workers sharing counts; Redis failing and coming back |
| `tests/security/test_rate_limit_api.py` | 429 and `Retry-After` through the API; real tokens charged; leaving early; admin changes |
