# Getting Started

Three ways to run EdgeShell: local development, Docker, and a production host
(EC2 or bare metal).

## 1. Local development

```bash
python run.py
```

`run.py` bootstraps a venv, installs dependencies, generates a `.env` with
random keys if missing, and starts the dev server. Default login is
`admin / admin123`, and you must change it on first login.

Development uses a dev-only fallback for secrets so you can start instantly.
Production does not (see below).

## 2. Docker

```bash
cp .env.example .env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python -c "import secrets; print('CREDENTIAL_KEY=' + secrets.token_hex(32))"
# paste both into .env, then:
docker compose up -d --build
```

- App is served on `http://<host>:5000`, health at `/healthz`.
- Data persists in named volumes `edgeshell_instance` and `edgeshell_uploads`.
- To use Postgres instead of SQLite, uncomment the `db` service and set
  `DATABASE_URL` in `.env`. See [MIGRATION.md](MIGRATION.md).
- Networking to reach nodes over the mesh is covered in
  [VPN-NETWORK.md](VPN-NETWORK.md).

## 3. Production host (EC2 or bare metal)

```bash
git clone <your-fork>/edgeshell.git
cd edgeshell
sudo bash deploy/install.sh
```

The installer:

1. Installs system packages (python venv, git, ssh client, sshpass, curl).
2. Creates the venv and installs dependencies.
3. Generates `.env` with random `SECRET_KEY` and `CREDENTIAL_KEY` if absent.
4. Initializes and seeds the database.
5. Installs and starts three systemd units.

| Unit | Role |
|---|---|
| `edgeshell.service` | Gunicorn app, `Restart=always`, starts on boot |
| `edgeshell-heartbeat.timer` | Posts a VPN status sample every minute |
| `edgeshell-watchdog.service` | Hits `/healthz`, restarts the app on failure |

Put a TLS-terminating reverse proxy (Caddy, nginx, or an ALB) in front, since
the production session cookie is secure-only. Point your domain at it. Users log
in at the domain; they do not need the mesh.

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

`run.py` (local) defaults to development, so LAN HTTP works out of the box.
`deploy/install.sh` sets production. During HTTP bring-up before TLS, set
`SESSION_COOKIE_SECURE=false` in `.env`, then flip it to true once TLS is in
front.

## Updating and rolling back

```bash
sudo bash deploy/update.sh     # pull, install deps, record a release, restart
sudo bash deploy/rollback.sh   # revert to the previous recorded release
```

Each update writes `releases/<timestamp>/manifest.json` and moves the `current`
pointer, so rollback is atomic. Schema in `deploy/manifest.schema.json`.
