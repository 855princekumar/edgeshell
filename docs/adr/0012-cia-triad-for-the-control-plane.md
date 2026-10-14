# 0012. Security model: the CIA triad for a central fleet control plane

## Status
Accepted (1.8.1).

## Context
EdgeShell is a central management plane: one place that holds the credentials
for, and can act on, every node. That concentration is the product's value and
its biggest risk. Every design choice is weighed against confidentiality,
integrity and availability, for the platform and for the fleet behind it.

## Decision

**Confidentiality**
- Two-factor sign-in for everyone. Its secrets are kept by a separate 2FA
  server and never replicated.
- Node passwords and API keys are encrypted or hashed at rest. Secrets come
  only from the host's environment, and the app refuses to start without them
  (ADR 0003).
- Databases sit on an internal network with no route to the internet and no
  published ports. Only nginx is exposed, with HTTPS, HSTS and strict headers.
- Least privilege everywhere: roles (ADR 0004), API scopes (ADR 0009), and a
  SELECT-only app account on the lake.
- **The public repository and images disclose nothing operational**: no
  secrets, hostnames, addresses, database access details or product and
  customer names. CI enforces this (ADR 0011).

**Integrity**
- CSRF protection on every form, strict input schemas, parameterized SQL.
- Every action is attributed and logged. History cannot be erased through the
  app (ADR 0008).
- Destructive fleet commands are blocked, and bulk deletes need confirmation
  (ADR 0010).
- Releases are built only from a green gate, with provenance attestation, for
  amd64 and arm64.

**Availability**
- The control plane must never become the outage: failing logins are paused,
  not retried; the collector uses pooled connections and bounded work; rate
  limits apply per credential and on sign-in.
- Data lives in host volumes and services restart on their own, so a container
  crash or upgrade loses nothing. Database dumps go to `backups/` before every
  deploy, and disk snapshots cover loss of the disk.
- Deploys use pinned image tags and can be rolled back by changing one tag.
- `/api/v1/health` and the lake lag report real readiness for monitoring.

**People**
- Social engineering guidance is in SECURITY.md: nobody needs your password, a
  2FA code or a key, and pasted secrets are rotated.

## Consequences
- Some convenience is traded away on purpose: 2FA to create keys, refused
  deletes while the lake is behind, typed confirmation for purges, and a slow
  release gate.
- New features are reviewed against this record. A feature that weakens one
  side of the triad needs its own ADR, or an entry in SECURITY.md's accepted
  risks.
