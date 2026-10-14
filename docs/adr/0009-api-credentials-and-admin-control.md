# 0009. Public API: per-user credentials, least privilege, administrator control

## Status
Accepted (1.8.0, administration added in 1.8.1).

## Context
Other systems (dashboards, alerting, automation) need fleet data and some
control. An API on a fleet control plane is a powerful entry point: one leaked
credential could reach every node. Shared API keys make it impossible to tell
who did what, or to cut off one integration without breaking the others.

## Decision
- **Every credential belongs to a user.** An API session takes a password and
  a 2FA code. A service key is for unattended use and needs a fresh password
  and 2FA code to create, plus a stated **purpose**. There are no shared or
  anonymous keys.
- **Least privilege.** Effective permission is the key's scopes intersected
  with the user's role (`read`, `write`, `control`, `admin`). A key cannot
  create keys.
- **Secrets are hashed** (SHA-256, constant-time compare) and shown once. They
  are never logged or stored in plain text.
- **Browser cookies are read-only on the API**, so there is no CSRF exposure.
  Changes need a bearer credential.
- **Strict input**: JSON schemas with unknown fields refused, a body size
  limit, and per-credential rate limits by role.
- **Dangerous capability is off by default.** Shell commands through the API
  need `API_ALLOW_COMMANDS=true` and still pass the command blocklist
  (ADR 0010).
- **Administrators keep control** (Administration, API Access):
  - the owner can turn the API, or any part of it, off and stop new keys;
  - an administrator can block a user's API access, which revokes every
    credential that user holds, and cannot lift a block on their own account;
  - every credential is visible with its creator, purpose, scopes, addresses,
    expiry and last use, and can be revoked;
  - revoking another administrator's key follows the owner rules, through the
    web and the API alike.
- **Audit**: every API request is logged with user, credential, route, result,
  duration and address. Bodies, query values and typed passwords are never
  logged. Requests to unknown paths are not logged, since they bypass the rate
  limiter and would let anyone flood the log. The log is kept for good in the
  data lake (ADR 0008).

## Consequences
- A leaked key is limited to its scopes and addresses, and is attributable
  and revocable in one click.
- Misuse is visible after the fact, with the purpose a key was created for.
- More steps for integrators (2FA to create a key). This is intentional.
