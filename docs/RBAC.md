# Roles and Access Control

EdgeShell is an in-house multi-user tool. Many people can be logged in at once;
each session is isolated by Flask-Login's signed cookie, so concurrent logins do
not interfere. Roles decide what each person may do, enforced centrally in
`app/services/rbac.py`.

## Roles

| Role | Rank | Can do |
|---|---|---|
| monitor | 0 | View dashboards, monitoring, node logs |
| operator | 1 | Everything monitor can, plus node CRUD, terminal, remote desktop (opening it where it runs), file deploy, bulk import |
| administrator | 2 | Everything operator can, plus user management, VPN config, platform settings, the activity log (audit trail), and installing software on nodes (Fleet Setup, Install on Fleet, OpenRemoteGUI install) |

The activity log is the audit trail, so only administrators see it: the page, its
sidebar link, the Recent Activity and Recent Errors panels on the dashboard, and
`GET /api/v1/activity` (403 for other roles). Operators and monitors still read
node logs in the Log Viewer.

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

## Two-factor authentication (mandatory)

- Every account signs in with a password **and** a 6-digit code from an
  authenticator app (Google Authenticator, Microsoft Authenticator, Authy,
  1Password, Bitwarden, ...). Nothing is signed in before the code passes.
- First sign-in shows a QR code to scan, then 10 one-time **backup codes** to
  save privately. A backup code works once in place of the app code.
- Failed codes count toward the same lockout as failed passwords.
- Who checks the codes: in the Docker stack, [privacyIDEA](https://privacyidea.org)
  in its own container on the internal network (no published port, no internet
  route); EdgeShell keeps only the token serials. Without that container
  (`run.py`, single container) a built-in verifier is used: the secret is
  encrypted with `CREDENTIAL_KEY`, backup codes are stored hashed. Set with
  `MFA_PROVIDER`; if privacyIDEA is down, sign-in fails closed.
- My account: backup codes left, new backup codes, and "Reset 2FA" for a new
  phone (password plus a current code required).

## Password recovery

| Who | How |
|---|---|
| Monitor or operator | "Forgot password?" sends a request to the administrators. An admin sets a temporary password; this also removes the user's 2FA, so it is set up again at the next sign-in, and ends every session. |
| Administrator | A reset link to the personal email on the account (needs `SMTP_*` and `PUBLIC_URL`), single use, valid 30 minutes. Or "use a backup code": username + one backup code + new password. 2FA stays on either way. The owner can also reset another administrator. |
| Owner | Emailed link or a backup code. Nobody else can reset the owner. |

Administrators must add a personal email before using the platform: the
first sign-in goes 2FA, then the email, then a new password.

## Administrators

- At most **3** administrators.
- The first administrator is the **owner** (primary administrator), with
  full control: the only account that can grant or remove the administrator
  role, and it can change, reset (password and 2FA), sign out, disable or
  delete **any** other account, administrators included. The owner is
  prompted to add up to 2 more administrators so the platform is never
  locked out.
- **Other administrators** manage monitors and operators only. They cannot
  change another administrator, and **nobody can change the owner**.
- An administrator leaves the role from My account ("Step down"), confirming
  with password, a 2FA code and typing `STEP DOWN`. The last administrator
  cannot step down. When the owner steps down, ownership passes to the
  longest-serving remaining administrator.

## Sign-up and approval

With `SIGNUP_ENABLED=true` (default) anyone can request an account. It stays
inactive until an administrator approves it in User Management, choosing the
role (monitor or operator; administrator only by the owner) and how many
devices it may use, or rejects it.

## Sessions

- Each sign-in is a row in `user_sessions`; the browser holds only a random
  token in its signed cookie, and every request is checked against the row.
- Each account allows `max_sessions` devices at once: 1 by default (admins
  included), up to 4, set only by an administrator.
- Logout, password change, password reset, 2FA reset, disabling a user, and
  "Sign out everywhere" revoke sessions server-side immediately.
- Each session records its device type (Desktop, Mobile, Tablet), IP and the
  last real interaction (click, key press, page load; background polls do not
  count). My account and User Management list them, and any single device can
  be signed out. The activity log records the device for every action.
- After `IDLE_MINUTES` (default 10) without interaction the page asks "Are you
  still there?" and signs that session out unless answered within
  `IDLE_WARNING_MINUTES` (default 5). The server enforces the same deadline.
- Sessions also end after `SESSION_HOURS` (default 8) without any request.
- Cookies are HttpOnly, SameSite=Lax, and Secure in production.
- CSRF tokens protect every state-changing request; the token rides the session.

## Why no per-user VPN

The control plane sits on a host already joined to the mesh. Users authenticate
to the portal over HTTPS at a domain. They never need mesh credentials, which
keeps mesh membership small and access control in one place (this app).
