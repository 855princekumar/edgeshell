# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.2.x | Yes |
| 1.1.x and older | No: upgrade with `git pull && python3 run.py`, or a newer image tag |

## Reporting a vulnerability

Report privately through GitHub: on this repository open **Security →
Report a vulnerability**
([direct link](https://github.com/855princekumar/edgeshell/security/advisories/new)).
Please do not open a public issue, pull request or discussion for a security
problem.

Include the version, how EdgeShell is installed (`run.py`, Docker Compose,
single container), the steps to reproduce and the impact you expect.

| Step | Target |
|---|---|
| Acknowledgement | within 3 working days |
| Initial assessment | within 7 days |
| Fix or mitigation for confirmed high or critical issues | within 30 days |

You will be credited in the release notes unless you ask not to be.

## Posture

- Production refuses to start without `SECRET_KEY` and `CREDENTIAL_KEY`.
- Node SSH passwords and API keys are encrypted at rest with Fernet, keyed by
  `CREDENTIAL_KEY`. Rotate with `python manage.py reencrypt --old <KEY>`.
- CSRF protection on every state-changing request, with a strict referrer check over HTTPS.
- Mandatory two-factor authentication for every account (authenticator app
  plus one-time backup codes). In the Docker stack, codes are checked by
  privacyIDEA in an isolated container with no published port.
- Login lockout after repeated failures (passwords and 2FA codes), per-IP rate
  limiting, and identical errors for unknown users, wrong passwords and
  locked accounts.
- At most 3 administrators. The owner (primary administrator) alone grants or
  removes the role and can manage every other account; other administrators
  cannot change an administrator, and nobody can change the owner. Stepping
  down requires password and 2FA.
- nginx is the only container with published ports; the app, database and
  privacyIDEA sit on internal Docker networks.
- Password reset links are single-use, expire in 30 minutes, go only to an
  address the user has verified, and are built from `PUBLIC_URL`, never from
  the request's Host header. 2FA stays on after a reset by email, so a mailbox
  alone never opens an account. An admin reset of a user's password also
  resets that user's 2FA.
- With mail set up, every account verifies its email by a signed link (24
  hours) before the app opens; a new address replaces the old one only once
  its link is opened.
- Sessions are tracked server-side: one device per user by default (up to 4,
  set by an administrator); logout, password change, password or 2FA reset and
  disabling a user revoke sessions immediately. Idle sessions are asked "still
  there?" and signed out.
- Cookies are HttpOnly, SameSite=Lax, and Secure behind HTTPS.
- Public API ([docs/API.md](docs/API.md)): password plus 2FA sessions and
  service keys created only after password plus 2FA again; 256-bit tokens stored
  as SHA-256 hashes; role intersected with scopes on every route; a browser
  cookie can only read (no CSRF exposure); strict schema validation that refuses
  unknown fields; per-credential rate limits; shell commands off unless
  `API_ALLOW_COMMANDS=true` and checked against the command blocklist; every
  action audited with the user and the credential. CI fuzzes every parameter
  with injection payloads and checks no response contains a secret.
- Data lake: a read-only replica on the internal network keeps all history;
  privacyIDEA's 2FA data is not replicated. Nothing done in the app can remove
  data from it: retention, clearing logs, and deleting nodes or users act on
  the hot database only (with `sql_log_bin=0`, after confirming the lake holds
  the rows), and the app's database account on the lake can only SELECT.
- API administration: administrators can turn the API or parts of it off,
  block any user's API access (revoking every credential), revoke any
  credential, and read a log of every API request (no bodies or values).
- Static and dependency scanning on every pull request: Bandit (no unreviewed
  medium or high finding), pip-audit (no dependency with a known
  vulnerability), CodeQL (Python and JavaScript), gitleaks over the full history.
- Node login policy (`app/services/login_policy.py`): after
  `LOGIN_PAUSE_AFTER_FAILURES` rejected passwords in a row (default 2), or one
  refusal by Tailscale SSH, EdgeShell stops logging in to that node, so it never
  repeats a rejected password (no guessing, no fail2ban bans or lockouts on the
  nodes). The pause is stored, survives restarts and is honoured by every SSH
  path (collector, bulk tests, fleet terminal, node terminal, deploys, scans).
  Recovery needs no guessing: every `LOGIN_RECHECK_MINUTES` the SSH banner of
  each paused node is read without logging in (a changed SSH server gets one
  new attempt), and one canary login tests nodes paused by Tailscale SSH. A
  person can retry once (Test on the card, Fix login, Retry paused logins).
  Pauses and resumes are in the activity log. A CI test fails the build if
  any code opens SSH outside `ssh_service.connect`, which also always closes a
  failed connection.
- The container image runs as a non-root user with read-only application code
  and no secrets baked in; releases carry an SBOM and build provenance.
- `run.py` enables a host firewall (ufw) and audits listening ports; the app,
  MariaDB and phpMyAdmin are never exposed to the internet.
- `main` is protected: pull requests, required CI checks (including a
  secret scan and end-to-end tests on amd64 and arm64) and secret-scanning push
  protection.

## Accepted risks

- **SSH host keys are trusted on first use** (`paramiko.AutoAddPolicy`): EdgeShell
  does not pin node host keys. Inside a Tailscale or WireGuard mesh, where every
  device is authenticated by the mesh, impersonating a node is impractical, but
  on an untrusted network a man in the middle could see the node password on
  first contact. Keep nodes on the mesh. Host key pinning is planned.
- **The data lake protects against app actions, not a stolen database password.**
  Nothing the app does deletes lake data, but whoever holds the hot database's
  app password can run statements that replicate. Keep regular disk snapshots
  (or dumps) of the lake as well.

## Social engineering

- No one at EdgeShell, no administrator and no vendor needs your password, your
  2FA code or a service key. Treat any request for them, or to approve a sign-in
  you did not start, as an attack and report it.
- Two-factor codes and backup codes are single use; a code read out over the
  phone is still a full sign-in for whoever has the password.
- Creating a service key needs the password and a fresh 2FA code, so a
  colleague's unlocked, signed-in browser is not enough to mint one.
- Secrets pasted into chat, tickets or email are exposed: revoke and replace them.
- The activity log records sign-ins, key creation and revocation, policy and
  retention changes, and who cleared the log, so misuse is visible afterwards.

## Hardening checklist for operators

- [ ] Change the seeded `admin` password at first login (it is enforced).
- [ ] Set strong, unique `SECRET_KEY` and `CREDENTIAL_KEY`, and back them up with the database.
- [ ] Serve over HTTPS only (`run.py` with a domain, or the production Compose stack).
- [ ] Keep the security group to 80 and 443 public, SSH from your IP only.
- [ ] Give each person their own account with the lowest role that works.
- [ ] Prefer SSH keys on nodes where possible; treat stored passwords as sensitive.
- [ ] Store your backup codes privately, and back up `.env` and `data/` (including `data/privacyidea`).
- [ ] Add a second administrator (up to 3) so the platform is never locked out.
- [ ] Set `PUBLIC_URL` and `SMTP_*` so users verify their email and can receive reset links (test delivery first).
- [ ] Set `SIGNUP_ENABLED=false` if accounts should only be created by administrators.
