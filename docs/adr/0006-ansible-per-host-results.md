# 0006. Ansible with real per-host results

## Status
Accepted.

## Context
Fleet operations (like installing OpenRemoteGUI everywhere) must report which
node succeeded and which failed. The previous Ansible call returned the whole
stdout for every node and used an invalid flag, so per-host truth was lost.

## Decision
Use `ansible-runner` and read structured events (`runner_on_ok`,
`runner_on_failed`, `runner_on_unreachable`) to build a per-node result map.
Fall back to sequential SSH when Ansible is absent, so a bare host still works.

## Consequences
- Fleet installs report accurate success/failure counts.
- Failures are logged to ActivityLog and a Deployment row.
- Unreachable nodes are surfaced, never silently dropped.
