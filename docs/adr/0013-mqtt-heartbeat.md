# 0013. A second, VPN-independent liveness signal: encrypted MQTT heartbeats

## Status
Accepted (1.9.0).

## Context
EdgeShell measures node health over SSH through a VPN. A VPN glitch makes
healthy nodes look offline, so availability (SLA) was understated, and it was
impossible to tell "the node is down" from "the path to it is down". A second
signal must reach EdgeShell without the VPN, which in practice means over the
public internet, without exposing anything about the fleet and without letting
anyone fake a node's health.

Options considered:

| Option | Why not chosen |
|---|---|
| Nodes call an HTTPS endpoint on EdgeShell | Exposes a public ingest endpoint on the control plane; every node needs a route to it |
| Public MQTT broker, plain heartbeats | Anyone can read the fleet's names and inject fake "online" or "offline" |
| A private broker | Another internet-facing service to run and secure |
| **Public MQTT broker, encrypted and authenticated messages, a separate receiver** | No new inbound exposure; the broker only ever sees opaque bytes; the receiver is isolated from the control plane |

## Decision
- **Agent** on each node (`/opt/edgeshell-heartbeat`, systemd service running as
  the unprivileged `hbagent`): a heartbeat every 10 s and a last will (LWT)
  over a public broker (default `broker.hivemq.com`, TLS 8883).
- **Protocol** (one file shared by agent, receiver and EdgeShell, pinned by test
  vectors in all three repositories):
  - a fleet master key held only by EdgeShell and SenseHive; each node gets only
    its own key, its key id and the topic key;
  - AES-256-GCM with the topic as authenticated data; the name inside must match
    the key, so a node cannot speak for another node;
  - topics derived from the topic key and the ten-minute window, two
    random-looking levels; random client ids;
  - freshness (two minutes) and strictly increasing times; a last will counts
    only for its own connection (session), so an old one cannot mark a live node
    offline.
- **Receiver: SenseHive** in the stack (own container, SQLite, internal
  network). It verifies, keeps per-node state, events and per-minute presence,
  and serves them on a read-only API (bearer token).
- **Access to SenseHive** goes through EdgeShell first (nginx `auth_request`):
  - only administrators with a SenseHive pass get through;
  - the sidebar button (a POST with the CSRF token) issues the pass;
  - a direct visit needs a fresh 2FA code;
  - the pass is HttpOnly, SameSite=Strict, scoped to `/sensehive/`, and bound
    to the EdgeShell session;
  - nginx forwards only SenseHive's own cookie;
  - SenseHive adds its own login with salted hashes, a lockout, a new session
    at each sign-in and CSRF refusal, behind nginx's rate limit.
- **EdgeShell** installs the agent (a pinned copy in the image, never downloaded
  at install time), pushes a fleet `config.yaml` that agents hot reload, reads
  SenseHive's API every 15 s, shows heartbeat and LWT state on node cards for
  every role, and counts a node as up when SSH **or** a fresh verified heartbeat
  says so (dashboard SLA and hourly history, with VPN gaps recorded separately).
- **Hot reload, safely**: settings (`config.yaml`, no secrets) are separate from
  identity (`agent.env`). An invalid settings file is ignored; a new broker
  means a clean reconnect, so no false offline.
- Installing the agent and pushing settings are administrator actions (ADR 0010).

## Consequences
- A VPN outage no longer reads as a fleet outage; a real outage still does (the
  last will arrives within seconds).
- The public broker sees only random topics and ciphertext; a stranger cannot
  read, inject, replay or take a node offline.
- New dependencies on a public broker and the internet: if either is down, nodes
  show "silent", never "offline", and SSH monitoring continues as before.
- One more container (SenseHive) and two secrets in `.env`. Rotating the master
  key means reinstalling the agent on the nodes.
- Tested: protocol and agent unit tests (Python 3.9 to 3.13, paho 1.x and 2.x),
  a real broker end to end, a hardened systemd install with hot reload, an
  install without systemd, SenseHive's receiver and API, EdgeShell's
  provisioning, sync, SLA and roles, and the sandbox end to end (agent on an
  emulated node, broker, SenseHive, EdgeShell, settings push, last will).
