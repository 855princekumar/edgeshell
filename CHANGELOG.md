# Changelog

All notable changes are documented here. Format loosely follows Keep a Changelog.

## [Unreleased]

### Changed
- **CI runs in two stages.** The gate (lint, secret scan, unit tests on Python 3.11 to 3.13, and the browser UI test with 1500 nodes on every screen size and zoom level) runs first; the image build and the amd64 and arm64 sandbox start only when it passed. The browser UI test is now a required check on `main`.
- **Test counts are enforced.** Unit tests fail the gate unless every test passed, none was skipped and at least 110 ran (`tools/test_gate.py`); the browser test needs at least 375 checks (`--min-checks`). Counts appear on each run's summary page.
- The cd-persistence unit test is skipped on Windows only (it needs a POSIX shell); CI runs it on Linux.

## [1.7.3] - 2026-10-14

### Fixed
- **Recycle Bin: "Delete all permanently" accepted no typing.** The open side panel kept the keyboard focus inside itself, so DELETE could not be typed in the confirmation. Dialogs now close the panel first and put the cursor in their first field. The browser UI test types DELETE with real key presses and empties the bin.
- **Monitoring polled every node live, one request per node, every 15 seconds.** An older second `pollAll` on the page replaced the fast one, so each refresh opened an SSH collection on every node from every open Monitoring tab (and, before 1.7.1, retried failing logins that often). With a large fleet the browser ran out of connections and dropped other requests. The page now reads the background collector's latest results in one request, as designed; a test fails the build if a page defines a function twice.
- **Monitoring "Auto" refresh is remembered.** Switching it off used to last only until the page was left. The choice is now saved on the account and kept until changed: across pages, sign-outs, sign-ins and devices (each user has their own), also when switched while the page is still loading.

## [1.7.2] - 2026-10-14

### Added
- **Bulk actions on Node Fleet.** Selecting nodes shows a bar with **Edit login** and **Delete**. Edit login sets the SSH username, password and/or port of every selected node (empty fields keep each node's value), clears paused logins and checks each node once in the background with a progress card; the password is never logged. Delete moves the nodes to the Recycle Bin (monitoring and SSH stop, nothing on the nodes changes). Select All and every bulk action use only the nodes shown by the current filters.
- **Recycle Bin:** Restore all, and Delete all permanently (administrators, type DELETE), which also removes the nodes' monitoring history.
- **Re-adding deleted nodes by import:** a node whose name is in the Recycle Bin comes back with the imported details (same node, history kept) instead of being skipped as already present.

### Fixed
- Permanent delete failed on MariaDB for a node with monitoring history (rows still referenced it); the history is now deleted with it.
- Node names in the delete confirmations are escaped for JavaScript.

## [1.7.1] - 2026-10-14

### Added
- **Node login policy.** EdgeShell no longer keeps logging in to a node whose login fails: after 2 rejected passwords in a row (`LOGIN_PAUSE_AFTER_FAILURES`), or one refusal by Tailscale SSH, automatic and bulk logins to that node pause, and the pause survives restarts. Recovery without guessing: every 30 minutes (`LOGIN_RECHECK_MINUTES`) the SSH banner of each paused node is read without logging in, a changed SSH server gets one new attempt, and one canary login tests the nodes paused by Tailscale SSH (when it works, they all resume). Fix login, Test on a card and "Retry paused logins" try once. The Node Fleet page lists paused nodes, cards show a badge, Test All skips them, and every pause and resume is in the activity log. Nodes already failing when 1.7.1 starts are paused at once instead of retried.
- A CI test that fails the build if any code opens SSH without the guarded helper.

### Fixed
- **File handle leak in SSH connections.** A login that failed (wrong password, time-out, refused) left its half-open connection and transport thread behind. With a few dozen nodes failing every 10 minutes the web process ran out of file handles after about three hours, then crashed and restarted, and new connections failed. Every SSH connection now goes through one helper that closes it on any failure.
- **Tailscale SSH is named as the cause.** When port 22 of a node is answered by Tailscale SSH and the login is refused or held for a browser check (an SSH policy rule with action `check`), the card says so instead of "check the SSH username and password", which no password change can fix. See the EC2 guide's troubleshooting table.

## [1.7.0] - 2026-10-14

### Added
- **Stats export on the dashboard.** Choose a period (last 24 hours, 7 days, 30 days, quarter, semester or year), a category (or all) and the detail (one row per node per hour, per day, or for the whole period). The CSV is built in the background with a progress bar, then downloads; the job card on every page also offers the download. Columns: uptime percent, samples online, offline and with a login failure, average and peak CPU, memory, disk, temperature and load, reboots, network traffic, and each node's category, type, location and environment, ready for a data lake and health-prediction models.
- **Long-term history.** One sample per node per minute whatever its state, so uptime is measured, not guessed (offline and login-failed minutes used to leave no trace), now with temperature and network counters. Hourly rollups are kept for good.
- **VPN / Tailscale peer filters:** status, operating system, link (direct, relayed, idle, none) and last seen (last hour, 24 hours, 7 days, or not for 7+ days), clickable column sorting, a "showing X of Y" count and Clear; choices are remembered in the browser.
- **Browser UI test** (`tools/ui_test.py`, a CI job): 1500 nodes, every page on phone, tablet, laptop and desktop sizes and at 67 to 200 percent zoom. It checks that nothing sticks out of its card, no page scrolls sideways, no word or number is broken over lines and no script fails, then drives the filters, Select All, dialogs, the fleet terminal, the VPN filters, the export and a real SSH terminal on an emulated Raspberry Pi.
- A menu button on phones and narrow windows: the sidebar was unreachable below 768 px.

### Fixed
- Monitoring: the Restart button was hidden from everyone (a role check compared against capitalised role names); node names in its buttons are now escaped for JavaScript, and long names truncate.
- OpenRemoteGUI install runs `sudo bash install.sh` (`./install.sh` fails when the clone has no execute bit).
- Node Fleet card actions wrap inside the card (Delete no longer sticks out).
- Phone and zoomed layouts: the top bar, the Node Fleet and Monitoring header buttons, the fleet terminal (its output pane was squeezed to 36 px on phones), Monitoring and Users stat cards (numbers and words broke mid-word), table columns (emails and IPs broke letter by letter; wide tables now scroll inside their card), dashboard metric values and the import sample.

## [1.6.0] - 2026-10-14

### Added
- **Interactive SSH terminal per node.** A terminal button on each Node Fleet card opens a real shell on that node in a window (xterm.js over a WebSocket, a real PTY on the node): colors, `top`, `nano`, tab completion, Ctrl+C, and sudo prompting like any SSH session. Closing the window ends the SSH session on the node; it also closes after 30 minutes without input or as soon as the EdgeShell sign-in ends. Operators and administrators only, same-origin only, at most 4 per user and 12 per server, and every open and close is in the activity log.
- **Guided OpenRemoteGUI install** with live steps and log, one node at a time: safety checks (OS, free disk, network to GitHub, desktop session, sudo) that skip unsuitable nodes without touching them, then `git clone`, then `cd OpenRemoteGUI && sudo ./install.sh`, then a check that the page answers. "Install on Fleet" stops at the first failed install so a problem cannot spread across production nodes. The confirmation says what the installer changes (it turns off Raspberry Pi Connect's own VNC).
- A live job window (steps per node and the log) for long jobs, opened from the progress card's Details link.

### Changed
- **Fleet terminal runs in parallel and streams.** Offline and login-failed nodes are skipped up front (and listed), the rest run 16 at a time over the collector's open SSH connections, and each node's output appears as soon as it finishes instead of after the slowest one.

### Fixed
- `sudo` hung until the timeout on nodes that ask for a password (the OpenRemoteGUI install showed no progress for 10 minutes and the terminal's sudo switch did the same): there was no way to answer the prompt. sudo now gets the node's stored password on stdin, never on a command line and never echoed.

## [1.5.1] - 2026-10-13

### Fixed
- **OpenRemoteGUI was reported as installed on every reachable Raspberry Pi.** The check counted any `wayvnc` or `websockify` process, but Raspberry Pi OS runs its own `wayvnc` (and Raspberry Pi Connect's `rpi-connectd`) whenever VNC is enabled, and `pgrep -f` also matched the checking shell's own command line. OpenRemoteGUI now counts only when its install (`/opt/openremotegui`, `/etc/openremotegui` or a `~/OpenRemoteGUI` checkout) is present and its web gateway port (6080 by default) is listening; "installed but not running" is reported separately. On the live 73 online nodes this changed the result from 73 installed to the 2 nodes that really serve the OpenRemoteGUI page.
- The metrics collector now refreshes every node's OpenRemoteGUI state on each pass, so the Remote button is accurate without a manual scan. Earlier wrong results are cleared once on upgrade.
- Node-RED "running" now matches the process name (`pgrep -x node-red`) or the `nodered` service instead of a command-line search that could match the checking shell.

## [1.5.0] - 2026-10-13

### Added
- **Device categories on the dashboard.** A row of cards, one per category (default: Data Loggers, Floc Detectors, Bio Health Trackers, matched by node type, plus an automatic "Other"), each with online/total, availability against its own SLA target, average CPU and RAM, and login-failed and offline counts. Clicking a card scopes the SLA bar, stat cards, charts and node grid to that category.
- Administrators edit categories on the dashboard: add, rename, remove and reorder them, choose which node types belong to each, an icon and an SLA target. The editor lists the node types found in the fleet with their counts.
- Node Fleet has a Category filter (also `/nodes/?category=<name>`), and the status filter has "login failed".

### Fixed
- The last-seen time could overflow node cards on Node Fleet; the badge row now wraps.
- Node tags separated by semicolons (`datalogger;ts-online;tailscale`) showed as one tag; commas and semicolons both separate tags now.

## [1.4.0] - 2026-10-13

Fast fleet monitoring. Measured on a live 122-node Tailscale fleet, one full
refresh went from about 11 minutes to under 30 seconds cold and 5 to 8
seconds warm.

### Added
- **Background metrics collector.** One SSH connection per node is kept open and reused; metrics come from a single shell command reading `/proc` (no Python started on the node); the fleet is polled in parallel (`COLLECTOR_WORKERS`, default 48) every `COLLECTOR_INTERVAL` seconds (default 30), outside any web request. Pages read the stored results, so the dashboard and Monitoring load instantly; "Refresh All" collects live, in parallel.
- **Login problems show on the card.** Failures are classified: wrong SSH username or password shows "Login failed" with a **Fix login** button on the dashboard, Node Fleet and Monitoring cards, which saves the new username, password and port and re-checks the node at once. Unreachable nodes show the reason.
- Failing nodes back off instead of being retried every cycle; a wrong password is retried only every 10 minutes, so a node's own fail2ban does not lock EdgeShell out. Saving new credentials retries immediately.
- Adding a node returns at once; its login is checked in the background and the card updates within seconds.
- `tools/perf.py`: times every page, API and node action against a large simulated fleet. CI fails if any read endpoint's median exceeds 300 ms with 300 nodes.

### Changed
- Node password encryption derived its key (PBKDF2, 100,000 rounds) on every encrypt and decrypt, 17 ms each: a 500-node import or metrics pass spent over 8 seconds on it. The derived key is now cached per process (ciphertext unchanged): 0.01 ms per call. A 500-row import commit went from 8.6 s to 0.1 s.
- Bulk import checks the new nodes in batches through the collector (parallel SSH, one database writer): 500 nodes in about 4 s at 300 ms per node. It no longer runs the OpenRemoteGUI check per node; use "Check OpenRemoteGUI" on Node Fleet.
- Metrics history is stored at most once a minute per node, and history older than `METRICS_RETENTION_DAYS` is purged automatically.

## [1.3.5] - 2026-10-13

Ships the 1.3.4 changes below; the 1.3.4 tag was never published because a
timing-sensitive test stopped its release.

### Fixed
- The bulk import test read job progress through the app while the job was writing; with the in-memory test database that could roll a write back. It now watches the job registry directly.
- Terminal commands, Select All and other page actions failed on 1.3.3 servers with `Unexpected token '<' ... is not valid JSON`: the shared page script did not run, so requests lacked their CSRF token (fixed in the changes below).

## [1.3.4] - 2026-10-13 (not published)

### Fixed
- **Buttons and actions driven by JavaScript stopped working on every page in 1.3.0** (Select All, toasts, the session watcher, the CSRF header on background requests): an apostrophe in the "still there?" prompt ended a JavaScript string early, so the shared page script never ran. Fixed, and a test now renders every main page and checks each inline script with Node.js.
- **Bulk import returned 502 for larger files.** Every parsed row was stored in the session cookie, which outgrew nginx's header buffer (and browsers' 4 KB cookie limit). Rows now wait on the server under a single-use token; nginx also has larger header buffers.
- **Bulk import waited for SSH checks of the whole fleet** inside the request, one node at a time. It now creates the nodes at once and checks only the new ones in the background (8 in parallel on MariaDB), with a progress card at the bottom of every page; you can keep working or refresh.
- Long node names pushed status badges out of the dashboard cards and under the checkbox on Node Fleet: a missing `min-w-0` utility meant flex items could not shrink. Names now truncate (full name on hover), and cards, badges and table cells are guarded against overflow app-wide.
- The Terminal node list grew past the screen with a large fleet. The list and the output now scroll inside their panes, and on phones the panes stack.

## [1.3.3] - 2026-10-13

### Fixed
- **Log Viewer, Activity Log and node log pages returned 500 (Internal Server Error) in the published image.** A `.gitignore` rule meant for runtime `logs/` folders also matched `app/templates/logs/`, so those three templates were never committed; local builds had them, the CI-built image did not. The rule now applies only to `/logs/` at the project root, the templates are in the repository, and a test fails CI if any template the code renders is missing from git.

## [1.3.2] - 2026-10-13

### Fixed
- Browsers logged a 404 for `/favicon.ico` on every page. EdgeShell now has an icon, linked from every page, and `/favicon.ico` redirects to it.
- nginx reported itself unhealthy in HTTPS mode after the server's address changed: its health check followed the HTTP to HTTPS redirect to the public address. `/healthz` is now answered directly on port 80.
- privacyIDEA signed its internal session tokens with a random 24-character key and warned about the key length. The stack now passes a dedicated `MFA_SECRET_KEY` (add it to an existing `.env` with `echo "MFA_SECRET_KEY=$(openssl rand -hex 32)" >> .env`).

## [1.3.1] - 2026-10-13

### Fixed
- **2FA setup QR code could not be scanned.** The page sized the code with CSS but the SVG had no `viewBox`, so browsers cropped it instead of scaling it, cutting off two of its three corner markers. It now scales, with the standard quiet zone; verified by decoding a browser screenshot of the page. The manual setup key was always correct.
- The VPN Status page never showed data: it read field names the service did not return, and in Docker there is no `tailscale` CLI. It now reads tailscaled's LocalAPI through the host's socket (mounted read-only; tailscaled grants non-root users status only), falls back to the CLI on native installs, and escapes peer names.

### Added
- VPN Status refreshes every 10 seconds: connection state, this server's Tailscale IPs and DNS name, tailnet, version, health warnings, exit node, and every peer with online state, direct or relayed link, last seen and traffic, with a filter.
- `docker/host-hardening.sh`: fail2ban (SSH, repeat offenders, nginx rate-limit trips and vulnerability scanners, with web bans in Docker's `DOCKER-USER` chain), a key-only SSH policy, log rotation and automatic security updates.
- HTTPS without a domain: set `EDGESHELL_DOMAIN` to the server's public IPv4 address and certbot gets a free Let's Encrypt certificate for the IP (short-lived profile, renewed automatically when a third of its life is left).
- nginx TLS follows Mozilla's intermediate profile (TLS 1.2 and 1.3, forward-secret AEAD ciphers, ECDSA keys), with HSTS and `Cross-Origin-Opener-Policy`. Access and error logs are also written to `data/nginx-logs` for fail2ban.
- CI fails if an em dash appears anywhere in code, docs or the UI.

## [1.3.0] - 2026-10-13

Accounts release: mandatory two-factor authentication, account recovery,
administrator governance, sign-up approval and device-aware sessions.

### Added
- **Mandatory two-factor authentication** for every account: an authenticator app (TOTP, any standard app) plus 10 one-time backup codes shown once to save privately. Set up at first sign-in; nothing is signed in until the second factor passes, and failed codes count toward the account lockout.
- In the Docker stack the codes are checked by [privacyIDEA](https://privacyidea.org) (open-source MFA server) in its own container on the internal network, with no published port and no internet route. EdgeShell stores only token serials; the secrets stay in privacyIDEA, encrypted with its own key in `./data/privacyidea`. Installs without that container (`run.py`, single container) use a built-in verifier with the secret encrypted by `CREDENTIAL_KEY` and backup codes hashed.
- **My account** page: 2FA status, backup codes left, new backup codes, reset 2FA for a new phone, personal email, signed-in devices with "Sign out" for each.
- **Password recovery.** Users send a reset request from the sign-in page; an administrator sets a temporary password, which also removes that user's 2FA so it is set up again. Administrators recover by an emailed reset link (any SMTP server, `SMTP_*` and `PUBLIC_URL` in `.env`) or with one of their backup codes. Reset links are single-use, expire after 30 minutes and are never built from the request's Host header.
- **Administrator governance.** At most 3 administrators. The first administrator is the owner (primary administrator) with full control: the only one who can grant or remove the administrator role, and able to change, reset, sign out, disable or delete any other account. Other administrators manage monitors and operators only, and nobody can change the owner. Administrators step down themselves with their password, a 2FA code and a typed confirmation; the last administrator cannot. When the owner steps down, ownership passes to the longest-serving administrator. The owner is prompted to add more administrators.
- Administrators must add a personal email (where reset links go) before using the platform. The first sign-in goes: 2FA, personal email, new password.
- **nginx replaces Caddy** in the Docker stack and is the only container with published ports. Plain HTTP on the server's IP until `EDGESHELL_DOMAIN` is set; then a certbot container gets a Let's Encrypt certificate and nginx switches to HTTPS by itself, redirecting HTTP and the bare IP. Security headers, HSTS with TLS, a rate limit on sign-in and recovery form posts, and client IPs that cannot be spoofed through `X-Forwarded-For`.
- **Self sign-up** (`SIGNUP_ENABLED`, on by default): requests wait for an administrator, who approves with a role (monitor or operator; administrator only by the owner) and a device limit, or rejects them.
- **Device-aware sessions.** Each session records its device type (Desktop, Mobile, Tablet) and the time of the last real interaction (click, key press, page load; background polls do not count). Users and administrators see and close individual devices. The activity log records which device did what.
- **Inactivity sign-out.** After `IDLE_MINUTES` (10) without interaction a session shows "Are you still there?" with a countdown, and signs out unless answered within `IDLE_WARNING_MINUTES` (5). The server enforces the same deadline, so a closed laptop or forgotten tab is signed out too. Tabs of the same session share the timer.

### Security
- The default `admin` / `admin123` account is created only in an empty database. Previously it was re-created whenever no user was named `admin`.
- Passwords are at least 10 characters (temporary ones included) and cannot be reused on change.
- Admin password resets in User Management had no password field and always failed; resets now ask for a temporary password (a random one is suggested).

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
