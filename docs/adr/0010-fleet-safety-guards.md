# 0010. Fleet safety guards: the control plane must not take the fleet down

## Status
Accepted (1.7.1 to 1.8.1).

## Context
EdgeShell reaches every node with SSH and can run commands on all of them at
once. Its own failures, an operator's mistake or an attacker using a valid
session can therefore cause harm at fleet scale:

- A login that keeps failing, retried every minute across a whole fleet,
  locks accounts, fills node auth logs and looks like a brute-force attack to
  the mesh and to intrusion detection.
- One mistyped or malicious command sent to every node (`rm -rf /`, a fork
  bomb, `mkfs` on a disk) can destroy the fleet in seconds.
- VPN policy (Tailscale SSH) decides whether logins work at all. Overlapping
  rules silently turn logins into interactive checks that automation cannot
  pass.

## Decision
- **Node login policy**: after repeated failures a node's logins are paused
  instead of retried, with the reason shown on its card. A paused node can be
  resumed by hand, or recovers when its credentials are corrected; there is no
  blind guessing. SSH connections that fail to log in are closed at once.
- **Command policy**: built-in rules block commands that destroy a system,
  including fork bombs, recursive deletes of `/`, `--no-preserve-root`,
  filesystem creation, raw writes to disks and `chmod -R 777 /`.
  Administrators can add their own patterns. The policy applies to the
  terminal, the fleet terminal and the API.
- **API commands are off by default** (ADR 0009).
- **Bulk actions are explicit**: bulk delete goes through the Recycle Bin, and
  a permanent purge needs typed confirmation.
- **Tailscale SSH rules must not overlap.** A `check` rule overrides `accept`,
  so fleet access uses separate, non-overlapping rules (documented in the
  troubleshooting guide).
- **Every action is attributed**: activity log entries carry the user, the
  node and the result, and the log is kept in the data lake (ADR 0008).

## Consequences
- A wrong credential costs one paused node, not a locked-out fleet.
- The most destructive commands need a deliberate change of policy, not a
  typo. A blocklist is not a sandbox: operators with shell access can still do
  harm, which is why roles (ADR 0004) and the audit trail matter.
- Accepted risk: SSH host keys are trusted on first use; keep nodes on the
  mesh (SECURITY.md). Host key pinning is planned.
