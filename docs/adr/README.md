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
