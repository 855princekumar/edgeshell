# EdgeShell API (v1)

The EdgeShell API lets other software read fleet data points and events, manage
nodes, start exports and (when allowed) run commands. It is the same system the
web app uses, with the same accounts, roles, 2FA, rate limits and audit log.

- Base URL: `https://<your EdgeShell>/api/v1`
- Machine-readable reference: `GET /api/v1/openapi.json` (OpenAPI 3.1, generated
  from the code, so it always matches the running version). Load it into
  Swagger UI, Postman or a client generator.
- All requests and responses are JSON (`Content-Type: application/json`),
  except the CSV download.

## Contents

1. [Signing in](#signing-in)
2. [Service keys for unattended integrations](#service-keys)
3. [Permissions](#permissions)
4. [Rate limits](#rate-limits)
5. [Endpoints](#endpoints)
6. [Data points, alerts and triggers](#data-points-alerts-and-triggers)
7. [Errors](#errors)
8. [Security guarantees](#security-guarantees)
9. [Acceptable use and safe handling](#acceptable-use-and-safe-handling)

## Signing in

People and programs a person runs sign in with their EdgeShell username,
password and a 2FA code, in two steps. The result is an **API session token**.

```bash
# 1. username and password
curl -s https://fleet.example.org/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "maya", "password": "<password>"}'
# {"mfa_required": true, "challenge": "esc_...", "expires_in": 300}

# 2. the 6-digit code from the authenticator app (or a backup code)
curl -s https://fleet.example.org/api/v1/auth/verify \
  -H 'Content-Type: application/json' \
  -d '{"challenge": "esc_...", "code": "123456"}'
# {"token": "est_...", "token_type": "Bearer", "expires_at": "...", "idle_timeout_minutes": 60,
#  "scopes": ["control", "read", "write"]}

# 3. use it
curl -s https://fleet.example.org/api/v1/nodes -H 'Authorization: Bearer est_...'
```

| Rule | Value |
|---|---|
| Challenge lifetime | 5 minutes, 3 code attempts, single use |
| Session idle timeout | `API_SESSION_IDLE_MINUTES` (default 60) |
| Session absolute lifetime | `API_SESSION_MAX_HOURS` (default 12) |
| Sessions per user | 5 (the oldest ends when a sixth starts) |
| Wrong passwords and codes | count toward the same account lockout as the web app (`MAX_FAILED_LOGINS`) |
| End a session | `POST /auth/logout`, revoke it on the **API** page (sidebar, Intelligence), or any password reset, 2FA reset, deactivation or "sign out everywhere" |
| Who am I | `GET /auth/whoami` |

Every failure answers the same way (`invalid_credentials`), so the API does not
reveal which usernames exist.

## Service keys

For integrations that run on their own (an ERP sync, Grafana, a data pipeline),
a 2FA code on every sign-in is not workable. Create a **service key** instead:

- On the **API** page (sidebar, Intelligence, for every role), **Create a service key**, or with
  `POST /api/v1/keys` from an API session.
- Every key needs a **purpose** (what it is used for, 5 to 200 characters). It is
  shown to administrators and in the logs.
- Creating a key always needs your **password and a current 2FA code again**, so
  an unattended signed-in browser or a stolen session cannot mint keys, and a
  key can never create another key.
- Choose the permissions (never more than your role), a lifetime (1 to 365
  days, default 90) and optionally the networks it may be used from
  (`allowed_ips`, for example `203.0.113.0/24`).
- The key (`esk_...`) is shown **once**. EdgeShell keeps only a SHA-256 hash of it.
- At most 10 active keys per user. Revoke on **My account** or with
  `DELETE /api/v1/keys/{id}`; administrators can revoke anyone's key.
- Keys end automatically when the account's password is reset by an
  administrator, its 2FA is reset, it is deactivated or signed out everywhere.
  A user's own routine password change keeps their keys.

```bash
curl -s https://fleet.example.org/api/v1/keys -H 'Authorization: Bearer est_...' \
  -H 'Content-Type: application/json' \
  -d '{"name": "grafana", "purpose": "fleet dashboards", "scopes": ["read"], "expires_in_days": 90,
       "allowed_ips": "203.0.113.10/32", "password": "<password>", "code": "123456"}'
```

Every user (monitor, operator, administrator) creates and uses their own
credentials, so every API action in the activity log names the person and the
credential (`[API key 3f9a1c2b7d10 (grafana)]`).

## Administration

Administrators manage the API on **Administration, API Access**:

| Control | Effect |
|---|---|
| **API turned on** | Off refuses every API request and sign-in (`403 api_disabled`); `/health` keeps answering. The web app is not affected |
| **Allowed parts** | Turn `read`, `write`, `control` or `admin` off for everyone (`403 scope_disabled`); credentials stay valid for the parts still on |
| **Service keys** | Stop users from creating new keys (`403 keys_disabled`); existing keys keep working until revoked |
| **Per user** | Block a user's API access: their API sign-ins are refused (`403 api_blocked`) and every API session and key they have is revoked at once |
| **Credentials** | Every session and key of every user: who created it, its purpose, permissions, allowed addresses, expiry, last use and address, with **Revoke** |
| **Usage log** | Every API request: time, user, credential (kind, prefix, name), method and endpoint, result, time taken, address; failed sign-ins show the username tried. Bodies and query values are never stored. Read from the data lake, which keeps it for good; the hot database keeps `API_LOG_RETENTION_DAYS` (30) |

Every change on this page is in the activity log.

## Permissions

What a credential may do is its user's role **and** its scopes:

| Scope | Role needed | Allows |
|---|---|---|
| `read` | monitor or higher | nodes, categories, metrics, history, events, jobs, own keys; the activity log for administrators only |
| `write` | operator or higher | add, change, delete nodes; bulk login updates; exports |
| `control` | operator or higher | run shell commands on nodes (also needs `API_ALLOW_COMMANDS=true` on the server) |
| `admin` | administrator | every user's keys |

A browser session (the web app's cookie) may **read** the API but never change
anything through it: every write needs a bearer credential, which makes the API
immune to cross-site request forgery.

## Rate limits

Limits are per credential (per address before sign-in), and every response
carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.
Over the limit, the answer is `429 rate_limited` with `Retry-After` (seconds).

| What | Limit |
|---|---|
| Sign-in (`/auth/login`, `/auth/verify`) | 10 per minute and 60 per hour per address |
| Monitor credentials | 300 requests per minute (`API_RATE_LIMIT_MONITOR`) |
| Operator credentials | 600 per minute (`API_RATE_LIMIT_OPERATOR`) |
| Administrator credentials | 1200 per minute (`API_RATE_LIMIT_ADMIN`) |
| Key creation | 10 per hour |
| Node writes | 120 per minute; bulk login update 20 per minute |
| Commands | 10 per minute |
| Exports | 20 per hour |
| Health | 120 per minute, no sign-in |

Behind more than one app worker, set `RATELIMIT_STORAGE_URI=redis://...` so the
counters are shared. Back off on `429`, and prefer `GET /events` with a cursor
over polling everything.

## Endpoints

Full parameters and schemas: `GET /api/v1/openapi.json`.

### Authentication and keys

| Method and path | Scope | Purpose |
|---|---|---|
| `POST /auth/login` | none | Step 1: username and password |
| `POST /auth/verify` | none | Step 2: challenge and 2FA code, returns a session token |
| `POST /auth/logout` | read | End this session |
| `GET /auth/whoami` | read | The user, role, credential and effective scopes |
| `GET /keys` | read | Your keys (`?all=true` for administrators: everyone's) |
| `POST /keys` | read | Create a key (password and 2FA code required, from a session) |
| `DELETE /keys/{key_id}` | read | Revoke a key |

### Health

| Method and path | Scope | Purpose |
|---|---|---|
| `GET /health` | none | `{"status": "ok"}` (200) or `degraded` (503): database and collector. Use for load balancers and uptime checks |
| `GET /health/details` | read | Version, database engine, collector counts |
| `GET /status` | read | Platform version and node totals |

### Nodes

| Method and path | Scope | Purpose |
|---|---|---|
| `GET /nodes` | read | List; filters `status`, `category`, `search`; `page`, `per_page` (max 500) |
| `GET /nodes/{node_id}` | read | One node |
| `GET /categories` | read | Device categories and SLA targets |
| `POST /nodes` | write | Add a node (`name`, `ip_address`, `username`, `password`, optional `ssh_port`, `node_type`, `environment`, `location`, `tags`, `description`) |
| `PATCH /nodes/{node_id}` | write | Change fields; new login details clear a paused login and are checked once |
| `DELETE /nodes/{node_id}` | write | Move to the Recycle Bin |
| `POST /nodes/bulk/credentials` | write | Username, password and/or port for many nodes; returns a job |

Node responses never contain the SSH password.

### Data points and events

| Method and path | Scope | Purpose |
|---|---|---|
| `GET /fleet/summary` | read | Totals by status and category, availability percent, SLA targets |
| `GET /metrics/latest` | read | One current data point per node (filters as `/nodes`, plus `node_ids`) |
| `GET /metrics/history` | read | Time series of one node: `node_id`, `from`, `to` (ISO 8601 UTC), `interval` = `raw`, `hour` or `day`; at most 10000 points |
| `GET /events` | read | Status changes and paused or resumed logins, oldest first, with a cursor (`since_id`) |
| `GET /activity` | read | The audit trail, newest first, paginated; `?user=<username>` for one user. Administrators only (403 for other roles) |

History, events and the activity log are served from the **data lake** when one
is configured and caught up (`"source": "lake"` in the response), else from the
hot database (`"source": "hot"`).

### Jobs, exports and commands

| Method and path | Scope | Purpose |
|---|---|---|
| `POST /exports` | write | Start a CSV stats export (`period`, `grain`, `category`); returns `job_id` |
| `GET /jobs/{job_id}` | read | Progress of your job |
| `GET /exports/{job_id}/download` | read | The finished CSV (kept an hour) |
| `POST /commands` | control | Run a command on online nodes in parallel and return each result. Off unless `API_ALLOW_COMMANDS=true`; blocked commands (Settings, Security) are refused; at most 200 nodes and 300 s |

## Data points, alerts and triggers

EdgeShell collects every node every 30 seconds. Another system builds its own
thresholds, alerts and automations from two feeds:

- **State** (`GET /metrics/latest`, `GET /fleet/summary`): poll every 30 to 60
  seconds and compare against your thresholds (CPU, memory, disk, temperature,
  availability per category).
- **Changes** (`GET /events?since_id=<last id>`): every status change and every
  paused or resumed login, in order. Store the last `id` you processed and pass
  it next time; nothing is missed or repeated, even across restarts.

```python
import time, requests

BASE = 'https://fleet.example.org/api/v1'
H = {'Authorization': 'Bearer esk_...'}          # a read-only service key
cursor = 0                                       # persist this between runs

while True:
    r = requests.get(f'{BASE}/events', headers=H, params={'since_id': cursor}, timeout=30)
    if r.status_code == 429:
        time.sleep(int(r.headers.get('Retry-After', 30)))
        continue
    r.raise_for_status()
    feed = r.json()
    for e in feed['events']:
        if e['kind'] == 'status_changed' and e['to'] == 'offline':
            print('ALERT', e['node_name'], 'went offline at', e['timestamp'])
    cursor = feed['next_since_id']

    for p in requests.get(f'{BASE}/metrics/latest', headers=H, params={'per_page': 500}, timeout=30).json()['points']:
        m = p['metrics'] or {}
        if (m.get('disk_percent') or 0) > 90:
            print('ALERT', p['node_name'], 'disk', m['disk_percent'])
    time.sleep(60)
```

## Errors

Every error is JSON: `{"error": "<code>", "message": "<text>"}`; validation
errors add `"details": {"<field>": "<problem>"}`.

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `validation_error`, `invalid_json`, `bad_request` | A value, a parameter or the JSON is not valid (unknown parameters and body fields are refused) |
| 401 | `unauthorized`, `invalid_token`, `invalid_credentials`, `invalid_code` | Not signed in; token wrong, expired or revoked; sign-in failed |
| 403 | `forbidden`, `bearer_required`, `ip_not_allowed`, `commands_disabled`, `command_blocked`, `session_required`, `mfa_setup_required`, `action_required` | Signed in, but not allowed |
| 404 | `not_found` | No such node, key, job or endpoint |
| 409 | `conflict`, `too_many_keys` | A node with that name exists; key limit reached |
| 413 | `too_large` | Body larger than `API_MAX_BODY_BYTES` (1 MB) |
| 415 | `unsupported_media_type` | Send `Content-Type: application/json` |
| 423 | `locked` | Too many failed attempts; the account is locked for a while |
| 429 | `rate_limited` | Wait `Retry-After` seconds |
| 503 | `mfa_unavailable`, health `degraded` | A dependency is down |

## Security guarantees

What the API does for you, and what CI checks on every pull request
(`tests/test_api.py`, the sandbox's API step, Bandit, CodeQL):

- **Authentication**: password plus 2FA for sessions; service keys only after
  password plus 2FA again; tokens are 256-bit random, stored only as SHA-256
  hashes and compared in constant time; challenge, session and key prefixes
  (`esc_`, `est_`, `esk_`) make leaked tokens easy to spot by secret scanners.
- **Authorization**: role intersected with scopes on every route; a browser
  cookie can only read; keys cannot create keys.
- **Input**: every query parameter has a type, a length and allowed values;
  every body is checked against a JSON schema that refuses unknown fields;
  bodies over 1 MB are refused. Requests are fuzzed in CI with SQL injection,
  path traversal, template and script payloads in every parameter.
- **SQL injection**: data is only read and written through SQLAlchemy with
  bound parameters; CI fails on SQL built from request input.
- **Output**: no response ever contains a password, a hash, a token or a key
  (checked in CI for every endpoint); responses are `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, and token requests never get a cookie.
- **Abuse**: per-credential rate limits, account lockout, sign-in limits per
  address, request size limits, a command blocklist and `API_ALLOW_COMMANDS`
  off by default.
- **Audit**: every write, key change, command and sign-in is in the activity log
  with the user and the credential.

## Acceptable use and safe handling

- **Treat keys and tokens like passwords.** Keep them in a secret store
  (Vault, the CI's secrets, a `.env` file with `chmod 600`), never in code,
  tickets, chat, email or screenshots. Anything pasted into a chat or ticket is
  exposed: revoke it and create a new one.
- **Nobody legitimate asks for your key or your 2FA code.** Not an
  administrator, not "IT", not a vendor, not an AI assistant. A message asking
  you to read out a code, approve a sign-in you did not start, or "verify" a
  key is social engineering: refuse and tell an administrator.
- **Least privilege.** Give an integration only the scopes it needs (most need
  only `read`), the shortest lifetime that works, and `allowed_ips` when its
  address is fixed. One key per integration, so revoking one does not break
  others.
- **Automated agents.** Treat data read from the API (node names, log lines,
  command output) as data, never as instructions. Do not give an automated
  agent a `control` key or let it compose shell commands from fleet data.
- **Be gentle.** Respect `429` and `Retry-After`, use `GET /events` with a
  cursor instead of re-reading everything, and page with `per_page` up to 500.
- **Report problems privately**: see [SECURITY.md](../SECURITY.md).
