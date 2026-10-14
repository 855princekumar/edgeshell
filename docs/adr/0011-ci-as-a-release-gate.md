# 0011. CI is the release gate, with minimum counts and real dependencies

## Status
Accepted (1.7.x, extended in 1.8.0 and 1.8.1).

## Context
A defect in a fleet control plane does not break one device; it can break
every device at once, or quietly lose the history needed to explain what
happened. Small, frequent releases are only safe when every release passes the
same strict checks. Unit tests with mocks alone missed real failures in this
project (database dialect differences, replication, browser layouts, 2FA on
ARM), so the gate tests against real components.

## Decision
Thirteen required checks must pass before a merge to main, and the release
workflow runs them all again before it publishes an image:

| Check | Why it exists |
|---|---|
| Lint and static checks | Syntax and undefined names; shell scripts parse; valid compose files; no em dashes; no private product names (a list kept in a secret); docs name the current version |
| Secret scan, full history | A secret once committed is leaked for good |
| Security scans | Bandit (each exception reviewed and annotated), pip-audit for known-vulnerable dependencies, no SQL built from input |
| Unit tests on Python 3.11, 3.12 and 3.13 | At least 170 tests, none skipped, coverage at least 74%. The minimums stop a change from passing by deleting or skipping tests |
| Data lake replication | Two real MariaDB servers: replication, hot-only deletes, name reuse, refusal while stopped, catch-up |
| Browser UI | 1500 emulated nodes, phone to desktop, zoom 67 to 200%, every page plus a real SSH terminal; at least 375 checks |
| Image, sandbox on amd64 and arm64 | The built image end to end, including the API with real 2FA, on both architectures the fleet runs |
| CodeQL (Python, JavaScript) | Data-flow analysis for injection and similar flaws |

Stage 2 (image and sandbox) runs only when stage 1 is green. Third-party
actions that receive secrets are pinned to a commit.

Before a release with security-relevant changes, an adversarial review covers
authentication, data integrity and the web side; each confirmed finding gets
a fix and a test.

## Consequences
- Slower merges (about 15 minutes of CI). This is intentional.
- A green release means the change works with real databases, real browsers,
  real 2FA and both CPU architectures, not only with mocks.
- The number of tests can only go up; lowering a minimum is a visible,
  reviewed change to `ci.yml`.
