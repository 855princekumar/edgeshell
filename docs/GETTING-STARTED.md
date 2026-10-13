# Getting Started

Four ways to run EdgeShell: local development, Docker, the production Docker
stack, and a production host (EC2 or bare metal) with `run.py`.

## 1. Local development

```bash
python run.py --dev        # on Windows and macOS --dev is the default
```

`run.py` bootstraps a venv, installs dependencies, generates a `.env` with
random keys if missing, and starts the dev server. Default login is
`admin / admin123`. The first sign-in sets up two-factor authentication (any
authenticator app; save the backup codes), a new password and a personal
email.

Development uses a dev-only fallback for secrets so you can start instantly.
Production does not (see below).

## 2. Docker (single container)

```bash
cp .env.example .env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python -c "import secrets; print('CREDENTIAL_KEY=' + secrets.token_hex(32))"
# paste both into .env, then:
docker compose up -d            # pulls devprincekumar/edgeshell (amd64 or arm64)
```

- App is served on `http://<host>:5000`, health at `/healthz`.
- Data persists in named volumes `edgeshell_instance` and `edgeshell_uploads`.
- SQLite and plain HTTP: good for trying it out. For production use section 3 or 4.
- Networking to reach nodes over the mesh is covered in
  [VPN-NETWORK.md](VPN-NETWORK.md).

## 3. Production with Docker (HTTPS + MariaDB)

[`docker/compose.yaml`](../docker/compose.yaml) runs EdgeShell, MariaDB 11.4
(internal network only), privacyIDEA for 2FA, and nginx as the only public
entry point, with certbot for Let's Encrypt once a domain is set. All state
lives in `./data` on the host and only ports 80 and 443 are exposed.
Step-by-step instructions, backups and upgrades are on the
[Docker Hub page](https://hub.docker.com/r/devprincekumar/edgeshell) and in
[docker/DOCKERHUB.md](../docker/DOCKERHUB.md).

## 4. Production host (EC2 or bare metal) with `run.py`

On Ubuntu 22.04, 24.04 or 26.04, or Debian 12, with Tailscale already up, a DNS record
pointing at the host and ports 80 and 443 open:

```bash
git clone https://github.com/855princekumar/edgeshell.git
cd edgeshell
cp .env.example .env
nano .env                  # EDGESHELL_DOMAIN and LETSENCRYPT_EMAIL
python3 run.py             # asks for sudo; re-run any time to update
```

On Linux, `run.py` without `--dev` hands over to `deploy/provision.py`, which:

1. Installs system packages: MariaDB, Nginx, PHP-FPM, phpMyAdmin, certbot, ufw.
2. Creates the `edgeshell` system user and copies the code to `/opt/edgeshell`
   (root-owned, read-only to the app).
3. Builds the virtualenv and generates `SECRET_KEY` and `CREDENTIAL_KEY` if absent.
4. Secures MariaDB, creates the database and least-privilege users, and
   migrates an existing SQLite database once.
5. Configures Nginx for your domain only, with a Let's Encrypt certificate and HSTS.
6. Binds phpMyAdmin to the Tailscale IP only.
7. Enables the host firewall (ufw): inbound only for your SSH port, 80, 443,
   41641/udp and the Tailscale interface (`EDGESHELL_FIREWALL=off` skips it).
8. Installs, enables and starts the systemd units, then checks health and
   audits every listening port.

| Unit | Role |
|---|---|
| `edgeshell.service` | Gunicorn on 127.0.0.1, `Restart=always`, starts on boot |
| `edgeshell-heartbeat.timer` | Posts a VPN status sample every minute |
| `edgeshell-watchdog.service` | Hits `/healthz`, restarts the app after 3 failures |

Users sign in at the domain; they do not need the mesh. The full guide,
security group rules and troubleshooting are in [DEPLOY-EC2.md](DEPLOY-EC2.md).

### First-run checklist

- [ ] Change the admin password.
- [ ] Create per-person accounts with the right role (see [RBAC.md](RBAC.md)).
- [ ] Configure the VPN backend in settings (Tailscale by default).
- [ ] Add nodes (single, or bulk via Import CSV).
- [ ] Run Test All, then Check OpenRemoteGUI, to confirm reachability.


## Accessing over HTTP vs HTTPS

If login fails with **"CSRF session token is missing"** when you open the app at
a LAN IP (like `http://10.x.x.x:5000`) but works at `http://localhost:5000`, the
cause is the secure session cookie. Production marks the cookie `Secure`
(HTTPS-only). Browsers accept it on `localhost` even over HTTP, but drop it on a
LAN IP over plain HTTP, so no session is stored and the CSRF token goes missing.

| Situation | Setting |
|---|---|
| Local or LAN testing over HTTP | `FLASK_ENV=development` or `SESSION_COOKIE_SECURE=false` |
| Production behind HTTPS (reverse proxy / ALB) | `FLASK_ENV=production` and `SESSION_COOKIE_SECURE=true` |

`run.py --dev` uses development, so LAN HTTP works out of the box. The
production installer sets `SESSION_COOKIE_SECURE` to match: `true` once the
certificate is issued, `false` while the host is served over plain HTTP.

## Updating and rolling back

Installed with `run.py`:

```bash
sudo mariadb-dump --single-transaction edgeshell | gzip > ~/edgeshell-$(date +%F).sql.gz
git pull && python3 run.py                      # update

git checkout <previous-tag> && python3 run.py   # roll back the code
gunzip -c ~/edgeshell-YYYY-MM-DD.sql.gz | sudo mariadb edgeshell   # and the data
```

Docker: change `EDGESHELL_TAG` in `.env` and run `docker compose up -d`.

`deploy/update.sh` and `deploy/rollback.sh` belong to the older
`deploy/install.sh` layout (releases recorded under `releases/`). They do not
apply to hosts installed with `run.py`.
