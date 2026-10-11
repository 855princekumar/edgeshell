# Security Policy

## Reporting

Report vulnerabilities privately to the maintainer (see the GitHub profile
`855princekumar`). Please do not open public issues for security reports.

## Posture

- Production refuses to start without `SECRET_KEY` and `CREDENTIAL_KEY`.
- Node SSH passwords are encrypted at rest with Fernet; the key comes from
  `CREDENTIAL_KEY`. Rotate with `python manage.py reencrypt --old <KEY>`.
- CSRF protection is enabled for all state-changing requests.
- Login lockout after repeated failures; login is rate-limited.
- Session cookies are HttpOnly, SameSite=Lax, and Secure in production.
- Run behind TLS. Keep the mesh (Tailscale/WireGuard) on the host, not per user.

## Hardening checklist for operators

- [ ] Set strong, unique `SECRET_KEY` and `CREDENTIAL_KEY`.
- [ ] Terminate TLS at a reverse proxy or load balancer.
- [ ] Change the seeded admin password immediately.
- [ ] Use SSH keys where possible; treat stored passwords as sensitive.
- [ ] Restrict the portal to the mesh or a trusted network.
- [ ] Back up `CREDENTIAL_KEY` with any database snapshot.
