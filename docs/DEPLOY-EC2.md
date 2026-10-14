# Deploying EdgeShell on AWS EC2

Two supported ways to run EdgeShell on an EC2 instance:

| | A. Docker stack (recommended) | B. Native install |
|---|---|---|
| Command | `docker compose up -d` | `python3 run.py` |
| Runs | nginx, EdgeShell, MariaDB, privacyIDEA (2FA), certbot, each in a container | MariaDB, Nginx, phpMyAdmin and EdgeShell as systemd services |
| Host stays clean | Yes: only Docker is installed; all state is in one folder | No: packages and services on the host |
| HTTPS | Automatic, on a domain or on the bare IP | Automatic, on a domain |

Every value in angle brackets is yours to fill in: `<SERVER_IP>` (the
instance's public IP), `<YOUR_IP>` (the public IP you administer from),
`your-key.pem` (your EC2 key pair). Never commit real values, keys or `.env`
files to a repository.

---

## A. Docker stack (recommended)

### A1. Launch the instance

| Item | Choice |
|---|---|
| AMI | Ubuntu Server 24.04 or 26.04 LTS (x86_64 or arm64/Graviton) |
| Size | t3.small or larger (2 GB RAM); 20 GB disk or more |
| Key pair | Create one and keep the `.pem` private (`chmod 400 your-key.pem`) |
| Elastic IP | Allocate one and associate it. A stop/start otherwise changes the public IP, and with it the URL and any IP certificate |

### A2. Security group (inbound)

| Type | Port | Source | Why |
|---|---|---|---|
| HTTP | 80 | 0.0.0.0/0 | Sign-in page; Let's Encrypt validation; redirect to HTTPS |
| HTTPS | 443 | 0.0.0.0/0 | EdgeShell over TLS |
| SSH | 22 | `<YOUR_IP>/32` only | Administration |
| anything else | | **none** | The app, database and 2FA service have no published ports |

Outbound: leave the default (all). EdgeShell reaches your nodes with outgoing
SSH, so nodes need no inbound rule here. The security group is the real
perimeter: ports Docker publishes are not filtered by the host firewall.

### A3. Install Docker

```bash
ssh -i your-key.pem ubuntu@<SERVER_IP>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit       # log in again for the group
```

Docker starts at boot, and every EdgeShell container restarts by itself.

### A4. Start EdgeShell

```bash
mkdir -p ~/edgeshell/nginx && cd ~/edgeshell
base=https://raw.githubusercontent.com/855princekumar/edgeshell/main/docker
curl -fsSLO $base/compose.yaml
curl -fsSLO $base/host-hardening.sh
curl -fsSLo nginx/entrypoint.sh $base/nginx/entrypoint.sh
curl -fsSLo nginx/certbot.sh $base/nginx/certbot.sh
curl -fsSLo .env $base/.env.example && chmod 600 .env
for k in SECRET_KEY CREDENTIAL_KEY DB_PASSWORD DB_ROOT_PASSWORD MFA_ADMIN_PASSWORD MFA_PEPPER MFA_DB_PASSWORD MFA_SECRET_KEY; do
  sed -i "s|^$k=.*|$k=$(openssl rand -hex 32)|" .env; done
docker compose up -d
docker compose ps          # all services "healthy" after about a minute
```

The secrets are generated on the server and never leave it. `.env` is
readable only by you.

What runs, and what is reachable:

| Service | Purpose | Reachable from |
|---|---|---|
| `nginx` | Reverse proxy, TLS, security headers, sign-in rate limit | Internet, ports 80 and 443 only |
| `edgeshell` | The application (non-root) | nginx only |
| `privacyidea` | Checks every 2FA code (non-root) | EdgeShell only (internal network) |
| `mariadb` | Database for EdgeShell and privacyIDEA | Internal network, no internet route |
| `certbot` | Free Let's Encrypt certificates, renewed automatically | Outbound only |

### A5. First sign-in

Open `http://<SERVER_IP>` and sign in as `admin` / `admin123`. You are walked
through, in this order:

1. **Two-factor authentication.** Scan the QR code with an authenticator app
   (Google Authenticator, Microsoft Authenticator, Authy, 1Password,
   Bitwarden...) or type the key shown under it, then enter the 6-digit code.
2. **Backup codes.** 10 one-time codes, shown once. Save them privately
   (password manager or printed). They replace the app code if you lose your
   phone and let you reset a forgotten password.
3. **Personal email.** Where password reset links for you are sent.
4. **New password** (at least 10 characters).

This first account is the **owner**: full control over every other account.
Add up to two more administrators (User Management) so the platform is never
locked out if one person loses access.

### A6. HTTPS (free certificate, automatic renewal)

Pick one, then run `docker compose up -d`. certbot gets the certificate within
a minute and nginx switches to HTTPS by itself; plain HTTP then redirects.

| | Domain (best) | IP address only |
|---|---|---|
| Needs | DNS `A` record to the Elastic IP | An Elastic IP |
| Certificate | Let's Encrypt, 90 days | Let's Encrypt short-lived IP certificate, about 6 days |
| Renewal | Automatic | Automatic (when a third of its life is left) |

In `.env`:

```bash
EDGESHELL_DOMAIN=fleet.yourdomain.com     # or <SERVER_IP>
ACME_EMAIL=you@yourdomain.com             # optional, expiry notices
SESSION_COOKIE_SECURE=true
PUBLIC_URL=https://fleet.yourdomain.com   # or https://<SERVER_IP>
```

TLS follows Mozilla's intermediate profile: TLS 1.2 and 1.3 only, forward
secret AEAD ciphers, ECDSA keys, HSTS. Check the result with
`curl -I https://<SERVER_IP>` or an online TLS test.

**One canonical address.** nginx serves HTTPS only for `EDGESHELL_DOMAIN` and
redirects every other name to it. If you add a domain later but leave
`EDGESHELL_DOMAIN` (and `PUBLIC_URL`) on the IP, visiting the domain shows "Not
secure" (the certificate is for the IP) and then jumps to the IP in the
address bar. DNS is not the problem in that case; switch the server to the
domain as below.

**Moving from the IP to a domain, without downtime.** Get the domain's
certificate first, while the site keeps running on the IP, then switch:

```bash
cd ~/edgeshell
nslookup fleet.yourdomain.com                      # must return the Elastic IP
cp .env backups/env-before-domain                  # keep the old settings
EMAIL=$(grep '^ACME_EMAIL=' .env | cut -d= -f2-)
sudo docker compose run --rm --no-deps --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot -d fleet.yourdomain.com --cert-name fleet.yourdomain.com \
  --email "$EMAIL" --key-type ecdsa --elliptic-curve secp256r1 \
  --agree-tos --non-interactive --no-eff-email
sed -i 's|^EDGESHELL_DOMAIN=.*|EDGESHELL_DOMAIN=fleet.yourdomain.com|; s|^PUBLIC_URL=.*|PUBLIC_URL=https://fleet.yourdomain.com|' .env
sudo docker compose up -d nginx certbot edgeshell
curl -sI https://fleet.yourdomain.com/login | head -1  # 200 with a valid certificate
curl -sI http://<SERVER_IP>/login | grep -i location   # redirects to the domain
```

The IP keeps working as a redirect to the domain, so old bookmarks still land
on the right page. Everyone signs in again once on the new address (cookies
belong to the address they were set on). certbot renews the domain
certificate from then on. To go back, restore `backups/env-before-domain` and
run the same `docker compose up -d`.

### A7. Harden the host

```bash
cd ~/edgeshell
sudo bash host-hardening.sh --admin-ip <YOUR_IP>
```

| Area | Result |
|---|---|
| SSH | Key-only; no root login; only your user; 3 attempts; no forwarding or X11. Checked with `sshd -t` before it is applied |
| fail2ban | SSH failures, repeat offenders (1 week), anyone tripping the sign-in rate limit, and scanners probing for `.env`, `.git`, `wp-login` and similar. Web bans go into Docker's `DOCKER-USER` chain, where they take effect |
| Logs | nginx logs in `data/nginx-logs`, rotated daily, 14 days kept |
| Updates | Unattended security upgrades |

`--admin-ip` keeps your own address from ever being banned. **Keep your SSH
session open and test a new login from a second terminal before closing it.**
Then check:

```bash
ssh -i your-key.pem ubuntu@<SERVER_IP> true                              # works
ssh -o PubkeyAuthentication=no ubuntu@<SERVER_IP>                        # Permission denied (publickey)
sudo fail2ban-client status                                              # 4 jails
sudo fail2ban-client set <jail> unbanip <ip>                             # unban someone
```

### A8. Tailscale (reach your fleet, live VPN status)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

EdgeShell reaches tailnet nodes through the host. The admin panel's **VPN
Status** page shows the tailnet live (state, IPs, every peer, direct or
relayed, last seen, traffic) by reading the host's `tailscaled` socket.
tailscaled allows non-root programs to read status only, so the panel can
never change the VPN.

### A9. Password reset email (optional, free)

Any SMTP server works; no paid API is needed. With Gmail: turn on 2-step
verification, create an app password, and add to `.env`:

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-character app password>
SMTP_FROM=you@gmail.com
```

then `docker compose up -d`. Other free options: Brevo (300 emails a day);
AWS SES is paid per use and needs a verified domain. Without email,
administrators recover with a backup code, and other users ask an
administrator.

### A10. Operate

| Task | Command (in `~/edgeshell`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f edgeshell nginx` |
| Upgrade | set `EDGESHELL_TAG` in `.env`, `docker compose pull && docker compose up -d --remove-orphans` |
| Back up | `docker compose stop && sudo tar czf edgeshell-$(date +%F).tgz .env data && docker compose start` |
| Back up databases only | `docker compose exec mariadb sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction --databases edgeshell privacyidea' \| gzip > db-$(date +%F).sql.gz` |
| Restart everything | `docker compose restart` |

Back up `.env` **and** `data/` together, and keep them off the server:
`CREDENTIAL_KEY` decrypts stored node passwords and `data/privacyidea` holds
the key for the 2FA secrets. Release notes list any new `.env` values; the
Docker Hub page has the upgrade steps.

### A11. Verify from outside

| Check | Expected |
|---|---|
| `curl -I http://<SERVER_IP>/login` | `200`, or `301` to HTTPS once a certificate exists |
| `curl -I https://<SERVER_IP>/login` | `200`, `Strict-Transport-Security` header, valid certificate |
| `curl -I http://<SERVER_IP>/.env` | `404` |
| `curl -m 5 http://<SERVER_IP>:5000` | no answer (only nginx is published) |
| `ssh -i your-key.pem ubuntu@<SERVER_IP>` | works; a login without the key is refused |

### A12. Troubleshooting

| Symptom | Fix |
|---|---|
| Page does not load from the internet, works on the server (`curl localhost`) | Security group: open 80 (and 443) |
| Certificate never arrives | `docker compose logs certbot`. Port 80 must be open and the domain must resolve to this IP; it retries every 20 minutes |
| Authenticator says the QR code is invalid | Type the key shown under the QR instead; each visit to the setup page makes a new key, so use the latest one |
| "Two-factor verification service is not responding" | `docker compose ps privacyidea` and `docker compose logs privacyidea` |
| Locked out of SSH by fail2ban | From the AWS console (EC2 Instance Connect or Session Manager): `sudo fail2ban-client set sshd unbanip <ip>` |
| VPN page says status unavailable | Tailscale is not running on the host: `sudo tailscale up` |
| Visiting the domain shows "Not secure", then the address bar shows the IP | `EDGESHELL_DOMAIN` and `PUBLIC_URL` are still the IP, so the certificate is for the IP and nginx redirects to it. Switch to the domain as in [A6](#a6-https-free-certificate-automatic-renewal) ("Moving from the IP to a domain") |
| Every node turns "login failed" at once while ping, Node-RED and the VPN page are fine; the message mentions Tailscale SSH | The nodes run Tailscale SSH, which answers port 22 and decides logins from the tailnet policy's `"ssh"` section. The default rule is `check` (browser approval), which EdgeShell can never give. Add an `accept` rule for EdgeShell and make sure no `check` rule matches the same login (a matching `check` overrides `accept`, whatever the order), or turn Tailscale SSH off on the nodes. Exact rules, safe steps and how to verify: [Tailscale SSH on the nodes](VPN-NETWORK.md#tailscale-ssh-on-the-nodes). Nodes resume by themselves within 30 minutes, or with **Retry paused logins** |

---

## B. Native install (`run.py`)

One command installs and updates: `python3 run.py`. It is idempotent; re-run
it after copying a new version of the folder. Two-factor codes are checked by
EdgeShell's built-in verifier in this mode.

### B1. Before you start

| Item | Requirement |
|---|---|
| OS | Ubuntu 26.04 / 24.04 / 22.04 LTS or Debian 12 (apt + systemd) |
| Size | t3.small or larger (2 GB RAM recommended for MariaDB + app) |
| Tailscale | Installed and logged in on the instance. EdgeShell only reads it |
| Elastic IP | Associate one with the instance |
| DNS | `A` record: `fleet.yourdomain.com` points at the Elastic IP |

Security group: as in A2 (80 and 443 open, 22 from `<YOUR_IP>` only). Never
open 5000, 8080 or 3306: the app, phpMyAdmin and MariaDB stay on loopback or
the tailnet.

### B2. Configure and run

In the folder's `.env` set:

```
EDGESHELL_DOMAIN=fleet.yourdomain.com
LETSENCRYPT_EMAIL=you@yourdomain.com
DISPLAY_TZ=UTC
```

Keep the existing `SECRET_KEY` and `CREDENTIAL_KEY`. Node SSH passwords are
encrypted with `CREDENTIAL_KEY`; losing it means re-entering them.

```bash
scp -i your-key.pem -r edgeshell ubuntu@<SERVER_IP>:~/
ssh -i your-key.pem ubuntu@<SERVER_IP>
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

Generated credentials are printed once and saved to
`/root/edgeshell-credentials.txt` (root only). First sign-in is the same as A5.

### B3. Access

| What | URL | Login |
|---|---|---|
| EdgeShell | `https://fleet.yourdomain.com` | your users (migrated) |
| phpMyAdmin | `http://<tailscale-ip>:8080` (from a tailnet device) | 1) basic auth 2) `edgeshell_dba` |
| MariaDB shell | `sudo mariadb edgeshell` on the instance | unix socket, no password |

### B4. Restart audit

Table `service_events` (and view `v_service_events_local`) records every start
and stop. Each also appears on the Activity Log page as user `system`.

| reason | Meaning |
|---|---|
| `first_start` | First start on this database |
| `host_boot` | Instance rebooted or stopped/started. Flagged `warning` if the previous boot ended without a clean stop |
| `deploy` | `run.py` installed or updated |
| `watchdog` | Health check failed 3 times in a row; watchdog restarted the app |
| `crash_restart` | App exited abnormally; systemd restarted it within 5 s |
| `service_restart` | Clean manual `systemctl restart edgeshell` |
| `host_shutdown` / `stopped` / `crash` | Stop events, with the systemd result code |

### B5. Operations

| Task | Command |
|---|---|
| Status | `systemctl status edgeshell edgeshell-watchdog` |
| Live logs | `journalctl -u edgeshell -f` |
| Update | copy the new folder, `python3 run.py` |
| Backup DB | `sudo mariadb-dump --single-transaction edgeshell \| gzip > edgeshell-$(date +%F).sql.gz` |
| Restart | `sudo systemctl restart edgeshell` |
| Remove | `sudo bash /opt/edgeshell/deploy/uninstall.sh` (add `--purge-data` to drop the DB) |

### B6. Troubleshooting

| Symptom | Fix |
|---|---|
| Certificate failed | Check the DNS A record and that port 80 is open, then re-run `python3 run.py` |
| phpMyAdmin bound to 127.0.0.1 | Tailscale was not up during install. Bring it up and re-run, or use `ssh -L 8080:127.0.0.1:8080` |
| Behind Cloudflare proxy | Client IPs appear as Cloudflare's. Use DNS-only (grey cloud) or add `real_ip` config |
| `Health check` warning at end | `journalctl -u edgeshell -n 100 --no-pager` |

---

## Sessions, roles and time (both ways)

- Every account, administrators included, may be signed in on **1 device** by
  default; an administrator can allow up to **4**. Each device is listed by
  type with its last interaction and can be signed out individually. Idle
  sessions are asked "still there?" after 10 minutes and signed out 5 minutes
  later. Details: [RBAC.md](RBAC.md).
- The database stores UTC; the UI renders `DISPLAY_TZ`.
