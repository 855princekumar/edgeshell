# Architecture

EdgeShell is a Flask application built with the app-factory pattern. One control
plane process manages many nodes over SSH and Ansible. State lives in a SQL
database (SQLite with WAL by default).

## Components

```mermaid
flowchart TB
    subgraph Host[EC2 host on the mesh]
        G[Gunicorn] --> APP[Flask factory]
        APP --> RT[Routes: auth, dashboard, nodes, monitoring, vpn, users, files, logs, settings, api, health]
        RT --> SV[Services]
        SV --> SSH[ssh_service]
        SV --> ANS[ansible_service]
        SV --> ORG[orgui_service]
        SV --> VPN[vpn_service]
        SV --> IMP[import_service]
        SV --> DBIO[dbio_service]
        APP --> DB[(SQL: SQLite WAL / PG / MariaDB)]
    end
    RT -->|SSH / Ansible| NODES[(Fleet of edge nodes)]
```

## Data model

```mermaid
erDiagram
    USER ||--o{ ACTIVITYLOG : performs
    NODE ||--o{ SYSTEMMETRICS : samples
    NODE ||--o{ NODELOG : produces
    USER {
      int id
      string username
      string role
      int failed_logins
      datetime locked_until
    }
    NODE {
      int id
      string name
      string ip_address
      int ssh_port
      int nodered_port
      bool orgui_installed
      int orgui_port
      string arch
    }
    SYSTEMMETRICS {
      int id
      int node_id
      float cpu_percent
      float ram_percent
      float disk_percent
    }
    DEPLOYMENT {
      int id
      string status
      int success_count
      int failure_count
    }
    VPNSTATUS {
      int id
      bool connected
      string backend_state
    }
```

## Key decisions

- App factory so the app can be created per config (development, production,
  testing) and imported by both `run.py` and `wsgi.py`.
- SQLite WAL for concurrent multi-user reads with a single writer, enabled via
  an engine `connect` event that is a no-op on Postgres/MariaDB.
- Services isolate side effects (SSH, Ansible, VPN) from routes so they are
  testable and swappable.
- VPN behind an interface so Tailscale can be replaced by WireGuard without
  touching routes or templates.

See the [ADRs](adr/) for the reasoning behind each.

## Monitoring contract

`POST /monitoring/poll` returns, per node, `{ success, metrics, node_name }`.
The `metrics` object carries `cpu_percent`, `mem_percent`, `disk_percent`,
`load_1/5/15`, `uptime`, `temperature`, `net_rx_bytes`, `net_tx_bytes`,
`docker_running`, `nodered_running`, `collected_at`. The frontend unwraps
`metrics` and renders. The dashboard uses `GET /monitoring/fleet-summary`, a
flat aggregate for the overview cards.
