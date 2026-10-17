# 0015: Hardware lifecycle: nodes as positions, detected replacements, fault reports

- **Status:** Accepted (1.13.0)
- **Date:** 2026-10-17

## Context

Edge hardware fails: power supplies, SD cards, boards. When an engineer swaps
an SD card or a whole Raspberry Pi, EdgeShell saw nothing: the node kept its
name and the replacement left no trace, or a new node was added and the old one
lingered offline. Nobody could answer how many SD cards failed this quarter,
which site keeps losing power supplies, or how long fixes take. Faults were
reported by message, outside the system. The owner wants:

- replacements detected and recorded per node, keeping the node's identity
- a new node that replaces a broken one linked to it, without deleting it
- fault reports by anyone, including monitoring staff and site engineers,
  resolved by operators and administrators only
- a fleet-wide hardware log with measurable numbers
- an API and integrations, generic enough for any fleet, not one company's

## Decision

**A node is a position; parts have service periods.** `hw_components` holds
each part (board, storage, network adapter, OS install, and parts recorded by
hand such as power supplies) with its node, identifier, model, installed and
removed times and the removal reason. Replacing hardware closes a period and
opens another; the node record stays. All hardware tables use `node_id`
without a foreign key and keep `node_name`, so the history outlives a node
deleted for good.

**Identity comes with the metrics.** The collector's single SSH command gains
a section that reads the board serial, the root disk's SD card ID or serial,
the first physical interface's MAC address, `/etc/machine-id`, descriptive
text and the Raspberry Pi power flags. No extra connection, no root, no
packages; missing values are empty and never count as a change. A fingerprint
of the result is stored on the node, so an unchanged node costs one string
comparison per cycle.

**Detected changes wait for a person.** A difference opens a pending
`hw_changes` row, named from the set of parts that changed (board and MAC:
board replaced; SD card ID and machine ID: SD card replaced; machine ID only:
OS reinstalled). Without a readable board serial the MAC stands in for the
board. Confirming (operators and administrators, with a reason) rewrites the
record; *fault* also records or resolves a fault so the numbers count it.
Rejecting keeps the record and flags the node as unexpected hardware until it
matches again. EdgeShell never rewrites the record by itself: a cloned SD card
on other hardware and a real swap look the same to software, and only a person
knows which one it was.

**Replacement nodes are linked, not merged.** `replaces_id` and
`replaced_by_id` on nodes form a lineage; the old node gets `retired_at`, is
no longer checked and keeps everything. The history view walks the lineage.
Suggestions rank down nodes by a shared part (strongest), location, name
similarity and type. An online node is suggested only when it shares a part,
because in numbered fleets every name looks similar.

**Faults are node-centred, separate from support tickets.** The existing issue
portal is a conversation between a user and the administrators;
`hw_faults` belongs to nodes, has a component category, severity, status and a
resolution, and feeds the numbers. Reporting needs any signed-in role;
progressing and resolving need operator. Categories are a settings list with
built-in keys (renamable, hideable) and custom ones, so other fleets track
their own parts.

**A narrow `report` API permission.** Every role has `read` and `report`;
`report` allows filing faults and nothing else, so a service key for a field
form or ticketing tool cannot change the fleet. Saved API policies from before
1.13 gain `report` automatically unless an administrator turns it off.

**Signed webhooks with an address guard.** Outgoing webhooks are HMAC-SHA256
signed over `"<timestamp>.<body>"` with a per-webhook secret (shown once,
stored encrypted), delivered from a small background pool with short timeouts,
no redirects and two retries, and switched off after 50 failures in a row.
Addresses resolving to loopback, link-local (the cloud metadata service),
multicast or reserved ranges are always refused; private and CGNAT ranges only
with `WEBHOOK_ALLOW_PRIVATE`. The same events go to the existing polling feed
(`/api/v1/events`).

## Consequences

- Swaps are visible within one collector cycle of the node being reachable,
  with a name a site engineer understands, and nothing changes the record
  without a decision.
- Numbers are defined in docs/HARDWARE.md and computed from rows, not guessed:
  faults per 100 nodes, time to fix, downtime from the per-minute history,
  service life per kind of part (marked "at least" when the start is unknown).
- A board serial is readable by anyone with root on the node, so detection
  raises the bar against cloned SD cards but does not prove identity. Signed
  per-node keys for the heartbeat agent are a separate, later change.
- On x86 the board serial usually needs root; the MAC stands in. Containers
  have no stable board or disk identity.
- DNS rebinding between the webhook address check and the connection is not
  prevented; the guard stops configuration mistakes and casual misuse, and
  only administrators add webhooks.
- The schema grows by six tables and ten node columns, created and added on
  start like every earlier change; the data lake replicates them.
