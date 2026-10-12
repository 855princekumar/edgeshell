# EdgeShell

Self-hosted fleet management for Raspberry Pi and edge devices. One secure web
portal to monitor, control and deploy to your whole fleet over SSH and Ansible:
live metrics, web terminal, file deployment, log browser, remote desktop,
Node-RED access, bulk onboarding and role-based access.

| | |
|---|---|
| **Platforms** | `linux/amd64`, `linux/arm64`: x86 clouds, AWS Graviton, Ampere Altra, Raspberry Pi 4/5. Docker picks the right one |
| **Source, docs, issues** | https://github.com/855princekumar/edgeshell |
| **Runs as** | non-root user `uid 10001`, read-only application code, no secrets baked in |
| **Ships with** | SBOM and build provenance attestations |
| **License** | MIT |

---

## Production setup (recommended)

EdgeShell, MariaDB and [Caddy](https://caddyserver.com) for automatic HTTPS, in
one Compose file. All state lives in a `./data` folder on the host, so backups,
upgrades and moving to another server are a folder copy.

**You need:** a Linux host with Docker and the Compose plugin, a DNS `A` record
pointing at it, and ports **80** and **443** open to the internet.

```bash
mkdir edgeshell && cd edgeshell
base=https://raw.githubusercontent.com/855princekumar/edgeshell/main/docker
curl -fsSLO $base/compose.yaml
curl -fsSLO $base/Caddyfile
curl -fsSLo .env $base/.env.example && chmod 600 .env

# Generate the four secrets
for k in SECRET_KEY CREDENTIAL_KEY DB_PASSWORD DB_ROOT_PASSWORD; do
  sed -i "s|^$k=.*|$k=$(openssl rand -hex 32)|" .env
done

nano .env          # set EDGESHELL_DOMAIN=fleet.yourdomain.com and ACME_EMAIL
docker compose up -d
```

Open `https://fleet.yourdomain.com` and sign in as **`admin` / `admin123`**.
You must set a new password before anything else.

### What you get

| Service | Role | Exposed |
|---|---|---|
| `caddy` | HTTPS with automatic Let's Encrypt certificates and renewal, HTTP→HTTPS redirect, HSTS and security headers | **80, 443** |
| `edgeshell` | The application | only to Caddy |
| `mariadb` | Database (MariaDB 11.4), on an internal network with no internet access | nothing |
| `init-data` | One-shot: prepares the host data folders for the non-root app user | nothing |

Containers start in order and only when the one before is healthy, and all
restart automatically after a crash or a host reboot.

### Where your data lives

```
edgeshell/
├── compose.yaml
├── Caddyfile
├── .env                 settings and secrets (chmod 600)
└── data/
    ├── mariadb/         database
    ├── instance/        app state
    ├── uploads/         files staged for deployment
    └── caddy/           TLS certificates and keys
```

> **Back up `.env` with `data/`.** `CREDENTIAL_KEY` decrypts the stored node SSH
> passwords; without it a restored database cannot use them.

### Configuration (`.env`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `EDGESHELL_DOMAIN` | yes | | Public hostname; Caddy gets its certificate |
| `ACME_EMAIL` | yes | | Let's Encrypt expiry notices |
| `SECRET_KEY` | yes | | Session signing key |
| `CREDENTIAL_KEY` | yes | | Encrypts node passwords and API keys |
| `DB_PASSWORD`, `DB_ROOT_PASSWORD` | yes | | MariaDB passwords (hex, so they are URL-safe) |
| `EDGESHELL_TAG` | no | `1.2.0` | Image version |
| `DISPLAY_TZ` | no | `UTC` | Time zone shown in the UI (the database stores UTC) |
| `SESSION_HOURS` | no | `8` | Idle session lifetime |
| `SINGLE_SESSION` | no | `true` | Enforce each user's device limit (1 by default, up to 4 per user) |
| `MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES` | no | `5` / `15` | Account lockout policy |
| `METRICS_RETENTION_DAYS` | no | `14` | Monitoring history kept |

The app refuses to start if any required value is missing.

### Operate

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f edgeshell` |
| Upgrade | set `EDGESHELL_TAG` in `.env`, then `docker compose pull && docker compose up -d` |
| Back up the database | `docker compose exec mariadb sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction edgeshell' \| gzip > edgeshell-$(date +%F).sql.gz` |
| Back up everything | `docker compose stop && tar czf edgeshell-backup.tgz .env data && docker compose start` |
| Restore the database | `gunzip -c edgeshell-YYYY-MM-DD.sql.gz \| docker compose exec -T mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" edgeshell'` |
| Stop | `docker compose down` (data in `./data` is kept) |

### Reaching your fleet

EdgeShell connects to nodes over SSH. Run your mesh VPN
([Tailscale](https://tailscale.com) or WireGuard) **on the host**: containers
reach tailnet addresses through the host. Nodes published on the host itself
are reachable as `host.docker.internal`.

---

## Quick try (single container)

```bash
docker run -d --name edgeshell -p 5000:5000 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e CREDENTIAL_KEY="$(openssl rand -hex 32)" \
  -e SESSION_COOKIE_SECURE=false \
  -v edgeshell_instance:/app/instance -v edgeshell_uploads:/app/uploads \
  devprincekumar/edgeshell:latest
```

Open http://localhost:5000. This uses SQLite and plain HTTP: fine for a test on
your machine, not for production. Note the `CREDENTIAL_KEY` you used if you
keep the volumes.

## Try it against a simulated fleet

[Pi Fleet Emulator](https://github.com/855princekumar/pi-fleet-emulator) spawns
Raspberry Pi-like nodes with real SSH and Node-RED (32/64-bit, Buster to
Bookworm). Export its fleet as JSON and import it in EdgeShell under
**Nodes → Import**.

## Security

- Required secrets, or the container will not start
- Login lockout, rate limiting, identical errors for unknown users
- One device per user by default (admins can allow up to 4); password resets and "sign out everywhere" end sessions immediately
- CSRF protection on every change; Secure, HttpOnly, SameSite cookies behind HTTPS
- Every release is published by CI only after unit tests, a secret scan, an image audit and an end-to-end run against an emulated fleet pass on **both** amd64 and arm64

Report vulnerabilities privately: see
[SECURITY.md](https://github.com/855princekumar/edgeshell/blob/main/SECURITY.md).

## Tags

| Tag | Meaning |
|---|---|
| `latest` | Newest release |
| `1.2.0`, `1.2`, `1` | Specific versions; pin one in production |
