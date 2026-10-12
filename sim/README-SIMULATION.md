# EdgeShell Fleet Simulation

> **Superseded.** For realistic testing use the [sandbox](../sandbox/) with
> [Pi Fleet Emulator](https://github.com/855princekumar/pi-fleet-emulator):
> `bash sandbox/run.sh`. This older demo is kept for reference.

A self-contained local test environment that emulates a 6-node Raspberry Pi fleet (3 × 32-bit, 3 × 64-bit, all Raspberry Pi OS Bookworm) on a simulated VPN/LAN network, running in Docker.

## What's Included

| Container | Role | IP | Ports |
|---|---|---|---|
| `edgeshell` | Control plane (Web UI) | 10.10.0.10 | 5000 |
| `edge-node-1` | Factory Pi 01 | 10.10.0.21 | SSH:2221, Node-RED:1881 |
| `edge-node-2` | Warehouse GW 02 | 10.10.0.22 | SSH:2222, Node-RED:1882 |
| `edge-node-3` | Outdoor Hub 03 | 10.10.0.23 | SSH:2223, Node-RED:1883 |
| `node-64bit-1` | Control 04 (64-bit) | 10.10.0.24 | SSH:2224, Node-RED:1884 |
| `node-64bit-2` | Gateway 05 (64-bit) | 10.10.0.25 | SSH:2225, Node-RED:1885 |
| `node-64bit-3` | Edge 06 (64-bit) | 10.10.0.26 | SSH:2226, Node-RED:1886 |
| `portainer` | Container dashboard | - | 9000 |

All containers share the `edgenet` network (`10.10.0.0/24`) - simulating a Tailscale/WireGuard VPN LAN.

---

## Quick Start

### Prerequisites
- Docker Desktop for Windows (running)
- Python 3.10+ in PATH

### 1. Start everything
```
Double-click: START-SIMULATION.bat
```
Or from PowerShell:
```powershell
cd sim
docker compose -f docker-compose.sim.yml up --build -d
python scripts/seed-nodes.py
```

### 2. Open EdgeShell
```
http://localhost:5000
Login: admin / admin123
```

### 3. Stop
```
Double-click: STOP-SIMULATION.bat
```
Or: `docker compose -f docker-compose.sim.yml down`

---

## What You Can Test

### SSH Multi-node Execution
1. Go to **Terminal** in EdgeShell
2. Select all 3 nodes
3. Run: `hostname` → each node returns its unique name
4. Run: `cat /tmp/metrics/summary.txt` → see live metrics
5. Run: `uptime`, `free -h`, `df -h`, `ps aux`

### Ansible Playbook Execution
The ansible playbooks in `../ansible/playbooks/` work against the simulated nodes:
```powershell
# From inside the edgeshell container:
docker exec -it edgeshell bash
ansible -i ansible/inventory/hosts.ini target_nodes -m ping
ansible-playbook ansible/playbooks/command.yml -e "cmd='uname -a'"
```

### File Upload & Deploy
1. Go to **Files** in EdgeShell
2. Upload any file (txt, sh, config)
3. Select target nodes
4. Set destination path: `/home/pi/`
5. Deploy - verify via SSH: `ls /home/pi/`

### Log Browsing
Available log files on each node:
- `/var/log/syslog`
- `/var/log/edgeapp/app.log`
- `/var/log/nodered.out.log`
- `/var/log/sshd.out.log`

### Monitoring
Each node generates realistic fluctuating metrics every 30 seconds:
- CPU: 5-75% (with occasional spike simulation)
- RAM: 60-85% of 512MB (Pi-realistic)
- Disk: 45-70% of 16GB
- Temperature: 42-65°C
- Load average: proportional to CPU

### Node-RED
Each node runs a real Node-RED instance:
- http://localhost:1881 (Factory Pi)
- http://localhost:1882 (Warehouse GW)
- http://localhost:1883 (Outdoor Hub)

---

## Direct SSH Access

From your Windows terminal (PowerShell/CMD):
```powershell
ssh pi@localhost -p 2221   # factory-pi-01  (password: raspberry)
ssh pi@localhost -p 2222   # warehouse-gw-02
ssh pi@localhost -p 2223   # outdoor-hub-03
```

From inside EdgeShell container (node-to-node, mimics VPN):
```bash
docker exec -it edgeshell bash
ssh pi@10.10.0.21   # direct LAN IP, no port mapping needed
```

---

## Network Architecture

```
Your Windows PC
│
├── localhost:5000  ──►  edgeshell (10.10.0.10)
│                              │
│                         edgenet (10.10.0.0/24)
│                         [simulated VPN LAN]
│                              │
│         ┌────────────────────┼────────────────────┐
│         │                   │                    │
│   edge-node-1         edge-node-2          edge-node-3
│  (10.10.0.21)        (10.10.0.22)         (10.10.0.23)
│  factory-pi-01      warehouse-gw-02      outdoor-hub-03
│
├── localhost:2221  ──►  SSH to node-1
├── localhost:2222  ──►  SSH to node-2  
├── localhost:2223  ──►  SSH to node-3
├── localhost:1881  ──►  Node-RED node-1
├── localhost:1882  ──►  Node-RED node-2
├── localhost:1883  ──►  Node-RED node-3
└── localhost:9000  ──►  Portainer
```

---

## Simulated Services on Each Node

| Service | Status | How to check |
|---|---|---|
| OpenSSH | Real, running | `ssh pi@localhost -p 222X` |
| Node-RED | Real, running | `http://localhost:188X` |
| Docker | Simulated (`/usr/local/bin/docker`) | `docker ps` via SSH |
| systemctl | Simulated | `systemctl is-active node-red` via SSH |
| /proc metrics | Simulated via fake-metrics | `cat /tmp/metrics/summary.txt` |

---

## Resetting the Simulation

To wipe all data and start fresh:
```powershell
docker compose -f docker-compose.sim.yml down -v   # -v removes volumes
docker compose -f docker-compose.sim.yml up --build -d
python scripts/seed-nodes.py
```
