# Deploying EdgeShell on AWS EC2

One command installs and updates: `python3 run.py`. It is idempotent; re-run it after copying a new version of the folder.

## 1. Before you start

| Item | Requirement |
|---|---|
| OS | Ubuntu 24.04 / 22.04 LTS or Debian 12 (apt + systemd) |
| Size | t3.small or larger (2 GB RAM recommended for MariaDB + app) |
| Tailscale | Already installed and logged in on the instance. EdgeShell only reads it, never reconfigures it |
| Elastic IP | Associate one with the instance |
| DNS | `A` record: `fleet.yourdomain.com` points at the Elastic IP |

### Security group (inbound)

| Port | Source | Why |
|---|---|---|
| 80 | 0.0.0.0/0 | Let's Encrypt validation, redirect to HTTPS |
| 443 | 0.0.0.0/0 | EdgeShell |
| 22 | Your IP only (or none, if you use Tailscale SSH) | Admin |
| 5000, 8080, 3306 | **Never open** | App, phpMyAdmin, MariaDB are loopback or tailnet only |

## 2. Configure

In the folder's `.env` set:

```
EDGESHELL_DOMAIN=fleet.yourdomain.com
LETSENCRYPT_EMAIL=you@yourdomain.com
DISPLAY_TZ=Asia/Kolkata
```

Keep the existing `SECRET_KEY` and `CREDENTIAL_KEY`. Node SSH passwords are encrypted with `CREDENTIAL_KEY`; losing it means re-entering them.

## 3. Transfer and run

```bash
# from your PC (over Tailscale or SSH)
scp -r edgeshell ubuntu@<ec2>:~/
# on the instance
cd ~/edgeshell && python3 run.py
```

`run.py` re-launches itself with `sudo`, then:

| Step | Result |
|---|---|
| Packages | MariaDB, Nginx, PHP-FPM, phpMyAdmin, certbot (no Apache) |
| Timezone | System clock set to `DISPLAY_TZ` (journal and logs readable). DB stays UTC |
| User | System user `edgeshell`, code in `/opt/edgeshell` (read-only to the app) |
| Database | Secures MariaDB, creates `edgeshell` DB and two least-privilege users |
| Migration | Existing `instance/edgeshell.db` copied into MariaDB once, then renamed `.migrated-*` |
| Nginx | App on the domain only, TLS + HSTS; raw IP and unknown hosts are dropped (444 / TLS rejected) |
| phpMyAdmin | Bound to the Tailscale IP, basic auth + DB login, root login disabled |
| systemd | App, watchdog and heartbeat enabled at boot; every start/stop logged to the DB |
| Firewall | ufw: inbound only for the SSH port, 80, 443, 41641/udp and `tailscale0`; skip with `EDGESHELL_FIREWALL=off` |
| Exposure audit | Lists every listening port after start-up and warns about anything beyond loopback or the tailnet |

Credentials are printed once and saved to `/root/edgeshell-credentials.txt` (root only).

## 4. Access

| What | URL | Login |
|---|---|---|
| EdgeShell | `https://fleet.yourdomain.com` | your existing users (migrated) |
| phpMyAdmin | `http://<tailscale-ip>:8080` (from a tailnet device) | 1) basic auth `pma-xxxxxx` 2) `edgeshell_dba` |
| MariaDB shell | `sudo mariadb edgeshell` on the instance | unix socket, no password |

## 5. Session limits per account

Every account, including administrators, may be signed in on **1 device** by default. An administrator can allow up to **4** (Add User form, or the Sessions column in User Management).

Signing in past the limit shows which device will be signed out (device, IP, last active, in local time) and asks to confirm. Confirming signs that device out immediately; an open tab on it shows a "Signed out" notice within 20 seconds. Password resets, disabling a user, and **Sign out everywhere** end all of a user's sessions at once. `SINGLE_SESSION=false` lifts the limit, but sessions are still tracked and revocable.

## 6. Restart audit

Table `service_events` (and view `v_service_events_local`) records every start and stop. Each also appears on the Activity Log page as user `system`.

| reason | Meaning |
|---|---|
| `first_start` | First start on this database |
| `host_boot` | Instance rebooted or stopped/started. Flagged `warning` if the previous boot ended without a clean stop |
| `deploy` | `run.py` installed or updated |
| `watchdog` | Health check failed 3 times in a row; watchdog restarted the app |
| `crash_restart` | App exited abnormally; systemd restarted it within 5 s |
| `service_restart` | Clean manual `systemctl restart edgeshell` |
| `host_shutdown` / `stopped` / `crash` | Stop events, with the systemd result code |

## 7. Operations

| Task | Command |
|---|---|
| Status | `systemctl status edgeshell edgeshell-watchdog` |
| Live logs | `journalctl -u edgeshell -f` |
| Update | copy the new folder, `python3 run.py` |
| Backup DB | `sudo mariadb-dump --single-transaction edgeshell \| gzip > edgeshell-$(date +%F).sql.gz` |
| Restart | `sudo systemctl restart edgeshell` |
| Remove | `sudo bash /opt/edgeshell/deploy/uninstall.sh` (add `--purge-data` to drop the DB) |

## 8. Time handling

The database stores UTC (portable, no DST ambiguity). The UI renders `DISPLAY_TZ`; browsers convert API times to local time. In phpMyAdmin use the `v_*_local` views for local timestamps.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Certificate failed | Check the DNS A record and that port 80 is open, then re-run `python3 run.py` |
| phpMyAdmin bound to 127.0.0.1 | Tailscale was not up during install. Bring it up and re-run, or use `ssh -L 8080:127.0.0.1:8080` |
| Behind Cloudflare proxy | Client IPs appear as Cloudflare's. Use DNS-only (grey cloud) or add `real_ip` config |
| `Health check` warning at end | `journalctl -u edgeshell -n 100 --no-pager` |
