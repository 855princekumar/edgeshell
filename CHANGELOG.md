# Changelog

All notable changes are documented here. Format loosely follows Keep a Changelog.

## [Unreleased]

### Fixed
- `python3 run.py` stopped at the Nginx step on Ubuntu 26.04 LTS: its `nginx.conf` sets `server_tokens` itself and nginx rejects the duplicate. The installer now switches that line to `off` (backup kept) instead. Verified on Ubuntu 26.04.1 (Python 3.14, MariaDB 11.8, PHP 8.5): fresh install, a re-run over a half-finished install, a repeat run and a reboot.

## [1.2.0] - 2026-10-12

Container-first release: multi-arch image, a full sandbox with Pi Fleet
Emulator, and stronger session security.

### Added
- Official multi-arch image `devprincekumar/edgeshell` (also `ghcr.io/855princekumar/edgeshell`) for linux/amd64 and linux/arm64, so it runs on x86 clouds, AWS Graviton, Ampere Altra and Raspberry Pi 4/5. Non-root, read-only code, tini as PID 1, SBOM and provenance attached.
- `sandbox/`: EdgeShell plus a 6-node [Pi Fleet Emulator](https://github.com/855princekumar/pi-fleet-emulator) fleet (32-bit and 64-bit across Buster, Bullseye and Bookworm) with an end-to-end suite covering import, SSH, architecture detection, fleet commands, monitoring, file push and pull, an OTA-style rollout, Ansible, session limits, brute-force protection, RBAC and restart persistence. Run it with `sandbox/run.sh`.
- CI on every push: lint, secret scan of the full history, unit tests on Python 3.11 to 3.13, amd64 and arm64 image builds with a hardening audit and vulnerability scan, and the sandbox suite on native amd64 and arm64 runners. Tagged releases publish only after all of it passes.
- Bulk import accepts JSON, including Pi Fleet Emulator's `pi-fleet-nodes.json`, plus a default SSH password and a `host.docker.internal` replacement option.
- Node addresses may be hostnames (container names, `host.docker.internal`, `pi.local`), not only IPv4.
- `run.py` enables a host firewall (ufw) on EC2: inbound only for the sshd port(s), 80, 443, Tailscale 41641/udp and the `tailscale0` interface. The SSH port is read from `sshd -T`, so the admin cannot be locked out. `EDGESHELL_FIREWALL=off` skips it.
- Production Docker Compose (`docker/compose.yaml`): EdgeShell, MariaDB 11.4 on an internal network and Caddy with automatic HTTPS; all state in a host `./data` folder; services start in health order and restart on reboot. Documented on the Docker Hub page.
- Exposure audit at the end of `run.py`: every listening socket is checked and anything reachable beyond loopback or the tailnet is reported.
- README screenshots, CODE_OF_CONDUCT, issue forms, pull request template, and private vulnerability reporting.
- Per-user session limits: each account allows 1 device by default (admins included). Administrators can set 1 to 4 when creating a user or later in User Management. Signing in past the limit asks to sign out the least recently active device.
- Sessions are stored server-side in a new `user_sessions` table; the MariaDB view `v_user_sessions` now lists every session.
- "Sign out everywhere" action per user in User Management.

### Security
- An admin password reset now signs the user out of every session. Previously an existing session stayed valid until it expired.
- Changing your own password signs out your other sessions.
- Disabling a user deletes their sessions, so re-enabling the account does not revive an old cookie.
- Logging out invalidates the session server-side even when `SINGLE_SESSION=false`.
- Login failures (unknown user, wrong password, locked account) show one identical message and take the same time, so usernames cannot be discovered from the login form.

### Fixed
- Enabling or disabling a user from User Management showed a raw JSON page; it now returns to the list with a message.
- Ansible fleet operations failed in the container: `ansible-core` was missing and node names with spaces broke the inventory. Both fixed; without Ansible, EdgeShell now falls back to plain SSH instead of failing.
- The login rate limit counted page loads, not just password attempts.
- `/api/v1/status` reported version 1.0.0; it now reads `VERSION`.
- Monitoring summary counted Docker and Node-RED as 0 because it ran before the node results arrived.
- Dashboard "Fleet Avg Temp" always showed n/a: the fleet summary never computed it.
- Getting Started, Migration and Architecture docs described the old installer, the wrong MariaDB driver and an outdated data model.
- The Docker image ran two Gunicorn workers, which split the in-memory rate limits; it now matches the systemd deployment (one worker, threads).

### Verified
- Full `run.py` install on a fresh Ubuntu 24.04 with systemd: MariaDB, Nginx, phpMyAdmin, ufw, all services enabled; services return after a reboot and the app restarts within seconds after being killed.
- Production Compose stack with the published image: HTTPS login, HSTS, HTTP to HTTPS redirect, and all data surviving removal and re-creation of every container.

### Upgrade notes
- Docker users: the image name is now `devprincekumar/edgeshell`. Existing volumes keep working.
- The schema upgrades itself on start (`users.max_sessions`, `user_sessions` table). The old `users.session_*` columns are no longer used.
- Everyone is signed out once after upgrading and signs in again.

## [1.1.0] - 2026-09-30

Production deployment on EC2.

### Added
- `python3 run.py` on Linux installs or updates production end to end: MariaDB, Nginx reverse proxy with Let's Encrypt, phpMyAdmin, systemd, watchdog. Idempotent.
- MariaDB via mysql-connector-python (unix socket); one-time automatic SQLite migration.
- Single active session per account with a confirm-before-takeover prompt; replaced devices are signed out with the reason, idle tabs within 20 s.
- `service_events` table: every service start/stop classified (host boot, crash restart, watchdog, deploy, shutdown) and mirrored to the Activity Log.
- phpMyAdmin bound to the Tailscale IP only, with basic auth, cookie login, root disabled, dedicated PHP-FPM pool.
- Local-time MariaDB views: `v_activity_local`, `v_service_events_local`, `v_user_sessions`.
- `docs/DEPLOY-EC2.md`.

### Fixed
- Timestamps displayed as UTC. DB still stores UTC; pages render `DISPLAY_TZ` (default Asia/Kolkata) and APIs emit ISO-8601 with `Z`.
- `db-export` wrote node SSH passwords in plaintext (read the decrypting property). Snapshots now carry ciphertext.
- Heartbeat timer was silently rejected by login and CSRF checks.
- Default rate limit (80/hour) was below the dashboards' own polling rate.
- Client IPs behind a proxy were all 127.0.0.1 (lockout, rate limits, audit); ProxyFix added.
- Health endpoint returned 503 on low disk, which would make the watchdog restart-loop.

## [1.0.0] - 2026-08-28

First deployment-focused release. Previous iterations were local proofs of
concept.

### Fixed
- OpenRemoteGUI detection now recognizes noVNC/wayvnc installs (port, directory, or process), so installed nodes are no longer reported as missing.
- Adding a user with a blank email no longer 500s; blank emails get a unique placeholder and duplicate emails/usernames show a friendly message.
- Terminal and Node-RED actions in the dashboard node popup are hidden from monitors.
- Docker detection uses sudo, so nodes where the login user is not in the docker group now report Docker correctly.
- Role checks are now case-insensitive and legacy capitalized roles are normalized, so operators are no longer treated as monitors.
- Monitoring cards were blank because the poll wrapper was not unwrapped and
  metric keys did not match the backend contract.
- Node-RED button opened localhost with a simulator-derived port; it now opens
  the node's real IP on its Node-RED port.
- SQLite WAL was assumed but never enabled; now set with busy_timeout for
  concurrent multi-user use.

### Added
- Add Node has a 'quick test node' toggle: SSH details only when on, full identification fields required when off.
- Node fleet: 32/64-bit architecture auto-detected on connection test and capability scan, shown as a badge and a filter.
- User creation now requires a unique username and unique email (case-insensitive), with a live 'already taken' check as you type.
- Admins can change any user's role inline from User Management.
- Fleet Setup: scan Docker, Node-RED, and OpenRemoteGUI across the fleet and install on nodes missing each.
- Issue portal: users report issues visible only to themselves and admins;
  admins see all. Threaded reply conversation per issue, status workflow, and
  email notifications through useSend (open-source, Resend-compatible).
- Central role-based access control with login lockout.
- CSRF protection across all state-changing requests.
- Fail-hard on missing production secrets; config-driven credential encryption.
- OpenRemoteGUI integration: per-node detect/install/open, fleet scan with
  counts, fleet-wide install via Ansible with per-host results.
- Per-node Node-RED and OpenRemoteGUI port configuration.
- Bulk node import from CSV/TXT with dynamic schema inference and a verify step,
  followed by ping, SSH test, and OpenRemoteGUI check.
- Backend-agnostic VPN status (Tailscale now, WireGuard later) with heartbeat.
- One-command database export/import for SQLite to Postgres/MariaDB migration.
- Health endpoint, watchdog, systemd units, and manifest-based update/rollback.
- Docker image (non-root, healthcheck) and compose with VPN/network options.
- Documentation: README with diagrams, getting started, architecture, RBAC,
  VPN/network, migration, and ADRs.

### Changed
- Removed the committed database and stray build directories from the tree.
