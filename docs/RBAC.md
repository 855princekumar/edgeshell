# Roles and Access Control

EdgeShell is an in-house multi-user tool. Many people can be logged in at once;
each session is isolated by Flask-Login's signed cookie, so concurrent logins do
not interfere. Roles decide what each person may do, enforced centrally in
`app/services/rbac.py`.

## Roles

| Role | Rank | Can do |
|---|---|---|
| monitor | 0 | View dashboards, monitoring, logs |
| operator | 1 | Everything monitor can, plus node CRUD, terminal, remote desktop, file deploy, bulk import, OpenRemoteGUI |
| administrator | 2 | Everything operator can, plus user management, VPN config, platform settings |

![RBAC matrix](assets/rbac-matrix.svg)

Monitor is strictly read-only: no terminal, no remote desktop, no file
deploy, no node control. Terminal, remote desktop, file deploy, bulk
import, and OpenRemoteGUI actions are hidden in the UI and blocked at the
route for monitors. User management, platform settings, VPN config, and
email (useSend) settings are admin only, hidden and route-blocked for
operators and monitors.


## How it is enforced

```python
from app.services.rbac import role_required, operator_required, admin_required

@nodes_bp.route('/add', methods=['GET', 'POST'])
@login_required
@operator_required          # rank >= operator
def add(): ...
```

`role_required(minimum)` compares the user's role rank against the minimum. AJAX
and API callers get a JSON 403; browser navigation gets a flash and redirect.

## Login protection

- After `MAX_FAILED_LOGINS` (default 5) failures, the account is locked for
  `LOCKOUT_MINUTES` (default 15). Both are configurable in `.env`. While
  locked, even the correct password is refused.
- The login route is rate-limited (20/min per IP).
- Every failure shows the same message and takes the same time, so the form
  does not reveal which usernames exist.
- `next` redirects are restricted to same-origin paths (open-redirect guard).
- Mandatory two-factor authentication for all users is coming in a separate
  build (in security testing).

## Sessions

- Each sign-in is a row in `user_sessions`; the browser holds only a random
  token in its signed cookie, and every request is checked against the row.
- Each account allows `max_sessions` devices at once: 1 by default (admins
  included), up to 4, set only by an administrator.
- Logout, password change, admin password reset, disabling a user, and
  "Sign out everywhere" revoke sessions server-side immediately.
- Idle sessions expire after `SESSION_HOURS` (default 8).
- Cookies are HttpOnly, SameSite=Lax, and Secure in production.
- CSRF tokens protect every state-changing request; the token rides the session.

## Why no per-user VPN

The control plane sits on a host already joined to the mesh. Users authenticate
to the portal over HTTPS at a domain. They never need mesh credentials, which
keeps mesh membership small and access control in one place (this app).
