# EdgeShell

Self-hosted fleet management for Raspberry Pi and edge devices. One secure web
portal to monitor, control, and deploy to your whole fleet over SSH and Ansible.

**Platforms:** `linux/amd64` and `linux/arm64` (x86 clouds, AWS Graviton,
Ampere Altra, Raspberry Pi 4/5). Docker picks the right one automatically.

Source, docs and issues: https://github.com/855princekumar/edgeshell

## Quick start

```bash
docker run -d --name edgeshell -p 5000:5000 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e CREDENTIAL_KEY="$(openssl rand -hex 32)" \
  -e SESSION_COOKIE_SECURE=false \
  -v edgeshell_instance:/app/instance \
  -v edgeshell_uploads:/app/uploads \
  --add-host host.docker.internal:host-gateway \
  devprincekumar/edgeshell:latest
```

Open http://localhost:5000 and sign in as `admin` / `admin123` (you must change
it on first login). Keep the same `CREDENTIAL_KEY` across restarts: it encrypts
the stored node SSH passwords.

Behind HTTPS (recommended for anything beyond a trusted LAN), drop
`SESSION_COOKIE_SECURE=false`.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes | Session signing key |
| `CREDENTIAL_KEY` | yes | Encrypts node passwords and API keys |
| `DATABASE_URL` | no | SQLite in `/app/instance` by default; MariaDB and PostgreSQL supported |
| `SESSION_COOKIE_SECURE` | no | `true` (default) behind HTTPS, `false` for plain HTTP |
| `BEHIND_PROXY` | no | `true` behind a reverse proxy, so client IPs are real |
| `SESSION_HOURS` | no | Idle session lifetime, default 8 |
| `DISPLAY_TZ` | no | UI time zone, default `Asia/Kolkata` |
| `GUNICORN_THREADS` | no | Worker threads, default 8 |

The container refuses to start without `SECRET_KEY` and `CREDENTIAL_KEY`. No
secrets are baked into the image, it runs as a non-root user (uid 10001), and
the application code is read-only.

## Volumes

| Path | Contents |
|---|---|
| `/app/instance` | Database (SQLite) |
| `/app/uploads` | Files staged for deployment |

## Try it against a simulated fleet

[Pi Fleet Emulator](https://github.com/855princekumar/pi-fleet-emulator)
spawns Raspberry Pi-like nodes with real SSH and Node-RED. Export its fleet as
JSON and import it in EdgeShell under **Nodes > Import**.

## Tags

| Tag | Meaning |
|---|---|
| `latest` | Newest release |
| `1.2.0`, `1.2`, `1` | Specific versions |

Every release is built by GitHub Actions only after unit tests, a secret scan,
an image audit and an end-to-end run against an emulated fleet pass on both
amd64 and arm64. Images ship with SBOM and build provenance.

License: MIT
