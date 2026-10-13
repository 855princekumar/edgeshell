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
- At most 3 administrators; only the owner grants the role; no administrator
  can change another; stepping down requires password and 2FA.
- Password reset links are single-use, expire in 30 minutes and are built from
  `PUBLIC_URL`, never from the request's Host header. An admin reset of a
  user's password also resets that user's 2FA.
- Sessions are tracked server-side: one device per user by default (up to 4,
  set by an administrator); logout, password change, password or 2FA reset and
  disabling a user revoke sessions immediately. Idle sessions are asked "still
  there?" and signed out.
- Cookies are HttpOnly, SameSite=Lax, and Secure behind HTTPS.
- The container image runs as a non-root user with read-only application code
  and no secrets baked in; releases carry an SBOM and build provenance.
- `run.py` enables a host firewall (ufw) and audits listening ports; the app,
  MariaDB and phpMyAdmin are never exposed to the internet.
- `main` is protected: pull requests, eight required CI checks (including a
  secret scan and end-to-end tests on amd64 and arm64) and secret-scanning push
  protection.

## Hardening checklist for operators

- [ ] Change the seeded `admin` password at first login (it is enforced).
- [ ] Set strong, unique `SECRET_KEY` and `CREDENTIAL_KEY`, and back them up with the database.
- [ ] Serve over HTTPS only (`run.py` with a domain, or the production Compose stack).
- [ ] Keep the security group to 80 and 443 public, SSH from your IP only.
- [ ] Give each person their own account with the lowest role that works.
- [ ] Prefer SSH keys on nodes where possible; treat stored passwords as sensitive.
- [ ] Store your backup codes privately, and back up `.env` and `data/` (including `data/privacyidea`).
- [ ] Add a second administrator (up to 3) so the platform is never locked out.
- [ ] Set `PUBLIC_URL` and `SMTP_*` so administrators can receive reset links.
- [ ] Set `SIGNUP_ENABLED=false` if accounts should only be created by administrators.
