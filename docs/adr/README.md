# Architecture Decision Records

Short records of the significant choices in EdgeShell and why they were made.

| ADR | Decision |
|---|---|
| [0001](0001-app-factory.md) | Flask app-factory pattern |
| [0002](0002-sqlite-wal-and-portability.md) | SQLite WAL by default, export/import to any SQL |
| [0003](0003-csrf-and-secret-handling.md) | CSRF on, fail hard on missing production secrets |
| [0004](0004-central-rbac.md) | Central role-based access control |
| [0005](0005-vpn-abstraction.md) | VPN behind an interface (Tailscale now, WireGuard later) |
| [0006](0006-ansible-per-host-results.md) | Ansible with real per-host results for fleet ops |
| [0007](0007-manifest-rollback.md) | Manifest-based update and rollback |
| [0008](0008-hot-database-and-data-lake.md) | Hot database plus a replicated, read-only data lake that the app cannot erase |
| [0009](0009-api-credentials-and-admin-control.md) | Public API: per-user credentials, least privilege, administrator control and audit |
| [0010](0010-fleet-safety-guards.md) | Fleet safety guards: paused logins, command policy, explicit bulk actions |
| [0011](0011-ci-as-a-release-gate.md) | CI as the release gate, with minimum counts and real dependencies |
| [0012](0012-cia-triad-for-the-control-plane.md) | Security model: the CIA triad for a central fleet control plane |
| [0013](0013-mqtt-heartbeat.md) | A second, VPN-independent liveness signal: encrypted MQTT heartbeats |
