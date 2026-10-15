# MQTT heartbeat: VPN-independent node liveness

EdgeShell reaches nodes over SSH through a VPN. When the VPN glitches, every
node looks offline although it is fine, and that time used to count as
downtime. The MQTT heartbeat adds a **second, independent signal**: a small
agent on each node reports "alive" every 10 seconds over a public MQTT broker
on the internet, encrypted and authenticated. A node counts as **up** when SSH
reaches it **or** a verified heartbeat arrived, so availability (SLA) is
measured precisely.

```mermaid
flowchart LR
  N[Node<br/>heartbeat agent<br/>/opt/edgeshell-heartbeat] -- "encrypted heartbeat every 10 s<br/>signed last will (LWT)" --> B[(Public MQTT broker<br/>TLS 8883)]
  B --> S[SenseHive<br/>verifies, keeps state]
  S -- "read-only API (token)" --> E[EdgeShell<br/>node cards, SLA]
  E -- "SSH over the VPN" --> N
```

| Part | Where | Role |
|---|---|---|
| Heartbeat agent | every node, `/opt/edgeshell-heartbeat` ([repository](https://github.com/855princekumar/mqtt-heartbeat-service)) | publishes heartbeats and a signed last will; reloads `config.yaml` on the fly |
| SenseHive | a container in the EdgeShell stack ([repository](https://github.com/855princekumar/sense-hive)) | verifies every message, keeps per-node state (SQLite), serves it read-only |
| EdgeShell | the app | installs the agent, pushes settings, reads SenseHive, shows status, counts uptime |

## Contents

1. [What you see](#what-you-see)
2. [Set it up](#set-it-up)
3. [Install the agent on nodes](#install-the-agent-on-nodes)
4. [Change settings on every node (hot reload)](#change-settings-on-every-node-hot-reload)
5. [How uptime is counted](#how-uptime-is-counted)
6. [Security on a public broker](#security-on-a-public-broker)
7. [Roles](#roles)
8. [Operate and troubleshoot](#operate-and-troubleshoot)

## What you see

On every node card (all roles):

| Badge | Meaning |
|---|---|
| **heartbeat** (green) | verified heartbeats are arriving |
| **LWT offline** (red) | the node's last will arrived: its agent's connection dropped (node down, or its internet) |
| **heartbeat silent** (amber) | no heartbeat and no last will: usually the broker or the internet path, not the node |
| **no heartbeat agent** (grey) | the node is reachable but has no agent; uptime relies on SSH only. Administrators install it in Fleet Setup |

Every change of heartbeat state is an event in the event feed
(`GET /api/v1/events`, kind `heartbeat_changed`). Administrators also get a
**SenseHive** link in the sidebar (new tab) with the full heartbeat page.

## Set it up

Docker stack, on the host, in the folder with `compose.yaml` (1.9.0 or later):

```bash
# Two secrets, generated once, kept only in .env
echo "HEARTBEAT_MASTER_KEY=$(openssl rand -hex 32)" >> .env
echo "SENSEHIVE_API_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d
```

- `HEARTBEAT_MASTER_KEY`: gives each node its own keys at install time. It
  stays on the server (EdgeShell and SenseHive); **it is never sent to a node**.
- `SENSEHIVE_API_TOKEN`: lets EdgeShell read SenseHive's API.
- Optional: `HEARTBEAT_BROKER` (default `broker.hivemq.com`), `HEARTBEAT_PORT`
  (8883), `HEARTBEAT_TLS` (true) as the defaults for new installs.

SenseHive keeps its data in `./data/sensehive` (kept across restarts and
upgrades). Open it once from the sidebar and **change its own password**
(default `admin` / `admin123`); only EdgeShell administrators can reach it at
all (nginx asks EdgeShell first).

## Install the agent on nodes

**Node Fleet, Fleet Setup, Heartbeat agent, Install on missing** (administrators).
It runs as a background job, 8 nodes at a time, with live progress. Per node:

1. EdgeShell uploads the agent (a pinned copy inside the EdgeShell image; no
   download from the internet), this node's `agent.env` (its name and keys) and
   the fleet `config.yaml` into a private temporary folder.
2. `install.sh` installs the OS packages (`python3-paho-mqtt`, `python3-yaml`,
   `python3-cryptography`), creates the unprivileged user `hbagent`, puts the
   files in `/opt/edgeshell-heartbeat` and starts the hardened systemd service
   `edgeshell-heartbeat`; the temporary folder is removed.
3. The agent identifies itself by the **EdgeShell node name**, so its heartbeats
   land on exactly that node: no duplicates, no overwriting another node.

Try one node first (select it, then install), then the rest. The Fleet Setup
scan shows which nodes have the agent running.

## Change settings on every node (hot reload)

**Fleet Setup, Heartbeat settings**: broker, port, TLS, interval, then **Save and
push to fleet**. EdgeShell writes the same `config.yaml` to every node with the
agent (atomically). Each agent:

- applies a new interval at once, without reconnecting;
- switches to a new broker cleanly (disconnect, connect, new last will), so no
  false "offline";
- ignores a broken or invalid file and keeps its last good settings, so a bad
  push can never stop heartbeats.

`config.yaml` holds no secrets. A node's keys (`agent.env`) are never part of a
push.

## How uptime is counted

| | Before | With the heartbeat |
|---|---|---|
| Dashboard availability (SLA) | nodes reachable over SSH now | nodes reachable over SSH **or** with a verified heartbeat in the last 40 s |
| History, stats export (`uptime_percent`) | minutes the node answered over SSH | minutes the node answered over SSH **or** sent a verified heartbeat |
| VPN gaps | counted as downtime | counted as up, and recorded separately per hour (`heartbeat_only`) |

A node that is really down sends no heartbeat (its last will arrives within
seconds), so real outages are still counted.

## Security on a public broker

Anyone can read and publish on a public broker, so nothing on it is trusted:

- **Encrypted and authenticated** (AES-256-GCM): the message says only the node
  name, a time, a counter, the kind and the agent version. No addresses, no
  credentials, no metrics, never anything from the node's data.
- **Per-node keys**: each node can only speak for itself; a stolen node key
  cannot fake another node.
- **Rotating topics**: random-looking, changing every ten minutes (derived from a
  key, like a 2FA code); the MQTT client id is random. Nothing on the broker
  names EdgeShell, the fleet or a node.
- **No replays, no fake "offline"**: times must be fresh and increasing; a last
  will only counts for the connection it belongs to.
- **TLS** to the broker with certificate verification.
- **On the node**: the agent runs as `hbagent` with systemd hardening (read-only
  system, no privileges, memory and CPU caps); `agent.env` is readable only by
  root and the agent.
- **SenseHive's API** is read-only (GET), needs a 32+ character token, and is not
  published outside the Docker network.

Details: [ADR 0013](adr/0013-mqtt-heartbeat.md) and the
[protocol](https://github.com/855princekumar/mqtt-heartbeat-service/blob/main/heartbeat_protocol.py).

## Roles

| Action | Admin | Operator | Monitor |
|---|---|---|---|
| See heartbeat and LWT status on node cards | yes | yes | yes |
| Install the agent, push heartbeat settings | yes | - | - |
| Open SenseHive | yes | - | - |

## Operate and troubleshoot

| Symptom | Check |
|---|---|
| Every node "heartbeat silent" | SenseHive's connection to the broker: SenseHive, Heartbeats page; `docker compose logs sensehive` |
| One node "LWT offline" but SSH works | the agent on that node: `systemctl status edgeshell-heartbeat`, `journalctl -u edgeshell-heartbeat -n 50` |
| Install fails at the packages step | the node needs internet for `apt` once |
| "HEARTBEAT_MASTER_KEY is not set" in Fleet Setup | add it to `.env` (see above) and `docker compose up -d` |

Rotating the master key: set a new `HEARTBEAT_MASTER_KEY`, `docker compose up -d`,
then reinstall the agent on every node (Fleet Setup). Until a node is
reinstalled its heartbeats are rejected (it shows silent), nothing else breaks.

Removing the agent from a node (the installer is not kept on the node):

```bash
sudo systemctl disable --now edgeshell-heartbeat
sudo rm -rf /opt/edgeshell-heartbeat /etc/systemd/system/edgeshell-heartbeat.service
sudo systemctl daemon-reload && sudo userdel hbagent
```

Or copy the repository's `install.sh` to the node and run `sudo bash install.sh --uninstall`.
