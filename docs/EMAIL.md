# Email: verification, password resets and ticket notices

EdgeShell sends a few kinds of email:

| Email | When |
|---|---|
| Confirm your email address | At the first sign-in (after 2FA), and whenever a user changes their address |
| Password reset link | "Forgot password?" on the sign-in page, for any role, to the verified address only. It opens one page: new password plus a 2FA code (or a backup code) |
| Password changed | After a reset by link or backup code |
| Ticket notices | A new ticket (to administrators, plus a receipt to the reporter) and replies |

**Without email** nothing breaks. Users ask an administrator for a reset,
administrators recover with a backup code, and admins see waiting requests as a
badge on User Management.

**With email**, every account confirms its address once, by opening a link.
After that anyone can reset their own password by email, and 2FA stays on.

A small fleet sends a few emails a day, so every free plan below is enough.

## Contents

1. [Choose a provider](#choose-a-provider)
2. [What EdgeShell needs](#what-edgeshell-needs)
3. [Resend](#resend)
4. [Brevo](#brevo)
5. [useSend](#usesend)
6. [Gmail or Google Workspace](#gmail-or-google-workspace)
7. [Amazon SES](#amazon-ses)
8. [Put the settings on the server](#put-the-settings-on-the-server)
9. [Test before everyone depends on it](#test-before-everyone-depends-on-it)
10. [Troubleshooting](#troubleshooting)
11. [Keeping the credentials safe](#keeping-the-credentials-safe)

## Choose a provider

Free plans at the time of writing. Check each provider's pricing page, as plans
change.

| Provider | How EdgeShell connects | Free plan | Good to know |
|---|---|---|---|
| [Resend](https://resend.com) | SMTP | About 100 a day, 3,000 a month | Instant activation; the SMTP login is always `resend` |
| [Brevo](https://www.brevo.com) | SMTP | 300 a day | SMTP access is sometimes approved manually for new accounts |
| [useSend](https://usesend.com) | HTTPS API, set up in the web app | See their pricing; also open source and self-hostable | No server access needed: Issues, Email settings |
| Gmail or Google Workspace | SMTP | Around 500 a day | Needs an app password, which a Workspace admin can disable |
| [Amazon SES](https://aws.amazon.com/ses/) | SMTP | Paid per email (very cheap) | New accounts start in a sandbox until you request production access |

Any other SMTP server works too, including your own.

**Always verify your own domain** with the provider (DNS records it gives you).
Mail from an unverified domain is refused or lands in spam, and a confirmation
email that never arrives holds users at the confirmation step.

## What EdgeShell needs

| Setting (`.env`) | Meaning |
|---|---|
| `PUBLIC_URL` | The address people open, for example `https://fleet.example.org`. Links in emails use it; they are never built from the request. Without it no link email is sent |
| `SMTP_HOST`, `SMTP_PORT` | The provider's SMTP server. Use port **587**: many clouds block port 25 (on EC2 it is blocked by default) |
| `SMTP_SECURITY` | `starttls` (port 587), `ssl` (port 465) or `none` |
| `SMTP_USER`, `SMTP_PASSWORD` | The SMTP login and its password or key |
| `SMTP_FROM` | The sender, an address on your verified domain, for example `fleet-noreply@example.org` |

useSend is set up in the web app instead (Issues, Email settings), and its API
key is stored encrypted in the database. If both are set, account emails use
SMTP and ticket notices use useSend; set up only one to keep it simple.

## Resend

1. Sign up, then go to **Domains > Add Domain**: your domain, or a subdomain
   such as `mail.example.org`.
2. Add the DNS records Resend shows at your DNS provider:
   - **DKIM**: a TXT record at `resend._domainkey`;
   - **SPF**: an MX and a TXT record on the `send` subdomain.

   They do not touch your normal mail records. Wait for **Verified**.
3. **API Keys > Create API Key**: permission **Sending access**, limited to that
   domain. It starts with `re_` and is shown once.
4. Settings:

   ```
   SMTP_HOST=smtp.resend.com
   SMTP_PORT=587
   SMTP_SECURITY=starttls
   SMTP_USER=resend
   SMTP_PASSWORD=<the API key>
   SMTP_FROM=fleet-noreply@<your verified domain>
   ```

The **Emails** page in Resend shows each message as delivered or bounced.

## Brevo

1. Sign up, then go to **Senders, Domains & Dedicated IPs > Domains > Add a
   domain**. Add the TXT (`brevo-code`), DKIM and (if you have none) DMARC
   records it shows, and wait for **Authenticated**. Then add a sender address
   on that domain.
2. **SMTP & API > SMTP**: note the **login** (like `xxxx@smtp-brevo.com`; it is
   not your own address) and **generate an SMTP key** (`xsmtpsib-...`). If SMTP
   shows as not activated, request activation there.
3. Settings:

   ```
   SMTP_HOST=smtp-relay.brevo.com
   SMTP_PORT=587
   SMTP_SECURITY=starttls
   SMTP_USER=<the Brevo SMTP login>
   SMTP_PASSWORD=<the SMTP key>
   SMTP_FROM=<your verified sender>
   ```

The **Transactional > Logs** page in Brevo shows every message.

## useSend

1. Sign up at useSend (or self-host it), add and verify your domain with the
   DNS records it shows, and create an API key with permission to send.
2. In EdgeShell, sign in as an administrator and open **Issues, Email
   settings**:
   - **Enabled**: on;
   - **Base URL**: `https://app.usesend.com`, or your own server;
   - **From**: your verified sender;
   - **API key**: the key.
3. **Save**, then **Send test**: the result or the exact error shows on screen.

Nothing goes in `.env`. Turn it off by unticking **Enabled**.

## Gmail or Google Workspace

1. On the sending account, turn on 2-Step Verification, then create an **app
   password** (Google Account, Security, App passwords).
2. Settings:

   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_SECURITY=starttls
   SMTP_USER=<the full address>
   SMTP_PASSWORD=<the 16-character app password>
   SMTP_FROM=<the same address>
   ```

## Amazon SES

1. In SES (in your region), verify your domain (DKIM records), and request
   production access so it can send to any address.
2. Create **SMTP credentials** (they are not your AWS keys).
3. Settings: `SMTP_HOST=email-smtp.<region>.amazonaws.com`, port 587,
   `starttls`, the SMTP user and password, and a `SMTP_FROM` on the verified
   domain.

## Put the settings on the server

Docker stack, on the host, in the folder with `compose.yaml`. The commands ask
for the secret without showing it, so it never appears on screen, in the shell
history or in a chat:

```bash
cp .env .env.bak-$(date -u +%Y%m%d-%H%M)
read -rp  'SMTP server: ' H
read -rp  'SMTP login: ' U
read -rp  'From address: ' F
read -rsp 'SMTP password or key: ' P; echo
setkey() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
setkey SMTP_HOST "$H"; setkey SMTP_PORT 587; setkey SMTP_SECURITY starttls
setkey SMTP_USER "$U"; setkey SMTP_FROM "$F"; setkey SMTP_PASSWORD "$P"
unset P
grep -E '^(PUBLIC_URL|SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_FROM|SMTP_SECURITY)=' .env
docker compose up -d edgeshell </dev/null
```

Only the app container restarts. If the secret contains `|` or `&`, edit `.env`
with an editor instead.

`run.py` installs: put the same keys in the `.env` of the source folder (or
`/opt/edgeshell/.env`), then re-run `sudo python3 run.py`.

## Test before everyone depends on it

As soon as mail works, every user is asked to confirm their address at their
next sign-in. So test with your own account first:

1. Sign in as an administrator. After 2FA, **Add and verify your email** shows.
   Press **Send the link**.
2. The email arrives within a minute (check spam the first time). Open the link.
3. Try **Forgot password?** with a test user that has confirmed its address.

If nothing arrives:

- **Issues, Email settings, Send test** shows the error. It stays reachable for
  an administrator even at the confirmation step.
- The app log has the error too:

  ```bash
  docker compose logs edgeshell | grep -i smtp
  ```

To turn email off again, set `SMTP_HOST=` (empty) and run
`docker compose up -d edgeshell`. Everything works as before.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Timeout connecting | Port blocked: use 587, not 25. Outbound traffic must be allowed (it is by default on EC2) |
| `535 Authentication failed` | Wrong login or key. Resend's login is the word `resend`; Brevo's is the `@smtp-brevo.com` login, not your address |
| `550` or "domain not verified" | The From address is not on a verified domain. Finish the DNS step at the provider |
| Delivered but in spam | The domain is not verified (DKIM), or the From address is on another domain |
| Users stuck at "verify your email" | Mail does not deliver. Fix it via Send test, or turn email off (see above) while you do |
| Links point to the wrong address | `PUBLIC_URL` is wrong or empty |

## Keeping the credentials safe

- The SMTP password or key lives only in `.env` on the host (mode `600`, not in
  the image). useSend's key is stored encrypted in the database.
- Never commit them, paste them into tickets or chat, or send them by email. If
  one was exposed, create a new key at the provider, delete the old one, and
  replace it with the commands above.
- Give the key the least permission the provider offers (sending only, limited
  to your domain).
- Each provider's log shows every message sent; check it if you suspect misuse.
