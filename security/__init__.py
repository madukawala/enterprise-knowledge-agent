"""Security: login, the role matrix, guardrails and rate limiting.

- roles.py: the three roles and the document access levels each may read.
- policies.py: the role matrix, what each role may do (deny by default).
- auth.py: checks Keycloak access tokens and returns the signed-in user.

Guardrails and rate limiting are added by their own branches.
"""
