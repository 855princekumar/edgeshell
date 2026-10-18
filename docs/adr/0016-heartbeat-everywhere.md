# 0016: The heartbeat agent on every node, kept there by a routine; four ways to the broker

- **Status:** Accepted (1.15.0)
- **Date:** 2026-10-18

## Context

The MQTT heartbeat (ADR 0013) is what lets the SLA tell a node that is down
from a VPN or SSH glitch. It only works where the agent runs, and in the field
it did not run everywhere:

- some nodes never got the agent (added after the last Fleet Setup install);
- some had an old agent that was never updated (there was no update rollout);
- on two or three nodes the agent was installed but stuck: SSH worked, yet it
  retried the same broker connection forever. TLS on 8883 was blocked on that
  network, or the port was taken by a proxy, and the agent had no other way in.

Uptime then quietly fell back to SSH only, the very signal the heartbeat exists
to back up. OpenRemoteGUI, Docker and Node-RED stay optional; the heartbeat
agent must not be.

## Decision

**A routine keeps the agent on every node.** In the collector loop, about 10
minutes after a start and then every `hours` (6 by default, Fleet Setup), for
each node SSH reaches now:

| The node | The routine |
| -------- | ----------- |
| has no agent | installs it |
| has an agent older than the one in this EdgeShell | updates it |
| SSH works but no heartbeat for 15 minutes | runs the agent's own test on the node; reinstalls only if the test fails |
| failed in the last 24 hours | leaves it alone (no hammering a broken node) |

At most `HEARTBEAT_ROUTINE_MAX` (25) nodes per pass, 8 at a time, as a
background job with an activity log entry per node and the pass summary in
Fleet Setup. Administrators can turn it off, change the interval, or run it now.

**Install through the agent's `setup.py`** with a join token EdgeShell makes
from the fleet key (the node's keys only, never the master key) and the broker
login (`HEARTBEAT_BROKER_USERNAME` / `_PASSWORD`, default `admin` /
`admin123`). On the node, `setup.py` tries **the four ways an MQTT client can
reach a broker**, each with a real login: MQTT over TLS (8883), WebSocket over
TLS (8884), plain MQTT (1883), plain WebSocket (8000). It keeps the first that
works as the preferred way and records it; the node card shows it.

**The agent falls back by itself** (agent 1.2.0): if its way in stops working
(three beats without a connection), it probes the ways in again and switches;
every 10 minutes (a new topic window) it tries the preferred way first. A
refused login stops the search (all ways share the login). No exception ends
the agent's loop.

**Settings pushes merge** (`setup.py --merge-config`): broker, port, TLS and
interval change, but each node keeps the way in it found unless the broker
itself changed.

## Alternatives considered

- **Alert only, install by hand:** the problem was exactly that nobody noticed.
- **Reinstall every node every pass:** needless load and restarts; the plan is
  per node and a repair tests before it reinstalls.
- **Plain MQTT as the only fallback:** fails where only port 443-style
  WebSocket traffic gets out; WebSocket over TLS is tried before any plain way.
- **Fallback inside the agent only:** a node with no working way at install
  would install and stay silent; the install-time checkpoint says so at once.

## Consequences

- Every node SSH reaches gets the agent, the current version and a working way
  to the broker within one routine interval, with nothing for an administrator
  to remember.
- A node on a network that blocks TLS still reports, over WebSocket TLS or, as
  a last resort, plain MQTT; the heartbeats stay encrypted end to end, only the
  broker login is unprotected on a plain way (the job log says so).
- The routine changes software on nodes on its own: it is on by default, logs
  every action and can be turned off in Fleet Setup.
- Tests: `tests/test_heartbeat.py` (plan, repair, job, keeper, login push,
  merging push, the vendored setup.py reading EdgeShell's tokens) and the
  agent's own tests (four ways, fallback, refused logins, stuck connections).
