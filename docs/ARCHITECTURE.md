# Architecture

EdgeShell is a Flask application built with the app-factory pattern. One control
plane process manages many nodes over SSH and Ansible. State lives in a SQL
database: MariaDB in production (`run.py` on a server, or the production
Compose stack), SQLite with WAL for local development and the single-container
image.

## Components

```mermaid
flowchart TB
    U[Users: browser over HTTPS] --> PX
    subgraph Host[Control-plane host on the mesh]
        PX[Nginx + Let's Encrypt<br/>native or in Docker] --> G[Gunicorn 127.0.0.1:5000]
        G --> APP[Flask factory]
        APP --> RT[Routes: auth, dashboard, nodes, monitoring, terminal,<br/>files, logs, users, issues, settings, vpn, api, health]
        RT --> SV[Services]
        SV --> SSH[ssh_service]
        SV --> ANS[ansible_service]
        SV --> ORG[orgui_service]
        SV --> VPN[vpn_service]
        SV --> SES[session_service]
        SV --> IMP[import_service]
        SV --> DBIO[dbio_service]
        APP --> DB[(MariaDB / SQLite WAL)]
        SD[systemd: app, watchdog, heartbeat timer] -.-> G
        FW[ufw: ssh, 80, 443, tailscale only] -.-> PX
    end
    SSH -->|SSH / Ansible over Tailscale| NODES[(Fleet of edge nodes)]
```

## Data model

```mermaid
erDiagram
    USER ||--o{ USER_SESSION : "signs in on"
    USER ||--o{ ISSUE : reports
    ISSUE ||--o{ ISSUE_MESSAGE : contains
    NODE ||--o{ SYSTEM_METRICS : samples
    USER {
      int id
      string username
      string role
      int max_sessions
      int failed_logins
      datetime locked_until
    }
    USER_SESSION {
      int id
      int user_id
      string token
      string ip
      string agent
      datetime seen_at
      datetime revoked_at
      string revoked_reason
    }
    NODE {
      int id
      string name
      string ip_address
      int ssh_port
      string arch
      int nodered_port
      bool orgui_installed
      bool is_deleted
    }
    SYSTEM_METRICS {
      int id
      int node_id
      float cpu_percent
      float ram_percent
      float disk_percent
    }
    ISSUE {
      int id
      int reporter_id
      string subject
      string priority
      string status
    }
    ISSUE_MESSAGE {
      int id
      int issue_id
      int author_id
      text body
    }
```

Stand-alone tables: `activity_logs` (audit of user actions), `node_logs`,
`deployments` (per-node results of file deploys), `vpn_status` (heartbeat
samples), `service_events` (every service start and stop with its reason) and
`settings` (key/value). On MariaDB, the views `v_activity_local`,
`v_service_events_local` and `v_user_sessions` show timestamps in `DISPLAY_TZ`.

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
