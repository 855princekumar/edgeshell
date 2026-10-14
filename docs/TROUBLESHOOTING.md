# Troubleshooting log

Every problem met while deploying and running EdgeShell in production (on AWS
EC2 and on bare metal), with how it showed up, why it happened, the fix, and
what now prevents it. Addresses and names are placeholders.

Quick index:

| Area | Problems |
|---|---|
| [Domain, DNS and HTTPS](#domain-dns-and-https) | "Not secure" on a new domain, address jumps to the IP, certificate never arrives, IP certificates, server IP changed |
| [Reaching the server](#reaching-the-server) | SSH times out, locked out by fail2ban |
| [Nodes and SSH](#nodes-and-ssh) | every node "login failed" while the VPN works, the server runs out of file handles, a policy change is undone, password failures repeated, sudo hangs, OpenRemoteGUI |
| [Database and data lake](#database-and-data-lake) | permanent delete fails, stale reads after a purge, lake behind, which database is in use |
| [Web app](#web-app) | 2FA QR does not scan, scripts dead on every page, log viewer 500, bulk import 502, phone layout, dialogs, monitoring settings |
| [Development and CI](#development-and-ci) | Windows path rewriting, scanners on new Python, flaky tests, secrets in chat |

---

## Domain, DNS and HTTPS

### A new domain shows "Not secure", then the address bar shows the IP

- **Symptom**: `https://fleet.example.org` warns "Not secure" (certificate name
  mismatch); after continuing, the browser is on `https://<SERVER_IP>`.
- **Cause**: DNS was fine. The server was still configured for IP mode
  (`EDGESHELL_DOMAIN=<SERVER_IP>`, `PUBLIC_URL=https://<SERVER_IP>`): the
  certificate covered only the IP, and nginx redirects every other host name to
  the configured one.
- **Check**: `nslookup fleet.example.org` (must return the server's IP);
  `echo | openssl s_client -connect <SERVER_IP>:443 -servername fleet.example.org | openssl x509 -noout -ext subjectAltName`
  (shows which names the certificate covers); `curl -sI http://fleet.example.org | grep -i location`.
- **Fix, without downtime**: issue the domain's certificate first while the site
  keeps running, then switch `.env` and restart nginx, certbot and EdgeShell:
  [DEPLOY-EC2.md, A6](DEPLOY-EC2.md#a6-https-free-certificate-automatic-renewal).
  The IP keeps working as a redirect to the domain.
- **After**: everyone signs in once more (cookies belong to the address that set
  them); password reset links use the new `PUBLIC_URL`.

### The certificate never arrives

- **Cause**: port 80 closed (security group or firewall), or the DNS record
  does not point to this server yet. Let's Encrypt validates over port 80.
- **Fix**: open 80 and 443; check DNS; `docker compose logs certbot`. certbot
  retries every 20 minutes (to stay inside Let's Encrypt's failure limits).

### HTTPS on a bare IP address

- Works with `EDGESHELL_DOMAIN=<SERVER_IP>`: Let's Encrypt issues short-lived
  IP certificates (about 6 days) and certbot renews them when a third of their
  life is left. Use a domain when you can: 90-day certificates and a name that
  survives an IP change.

### The server's public IP changed

- **Symptom**: the site and SSH are unreachable after an instance stop/start or
  attaching an Elastic IP.
- **Fix**: use an **Elastic IP** (it never changes); update DNS if you use a
  domain, or `EDGESHELL_DOMAIN`, `PUBLIC_URL` and the certificate if you use
  the IP; check the new SSH host key before trusting it.

## Reaching the server

### SSH to the server times out

- Usually transient (try again), or the security group allows SSH only from an
  address you are no longer on. Allow your current address in the security
  group (port 22), or use EC2 Instance Connect or Session Manager.

### Locked out by fail2ban

- From the AWS console (Instance Connect or Session Manager):
  `sudo fail2ban-client set sshd unbanip <your IP>`.

## Nodes and SSH

### Every node shows "login failed" at once, while ping, Node-RED and the VPN page are fine

- **Cause**: the nodes run **Tailscale SSH**, which answers port 22 and decides
  logins from the tailnet policy, not the password. The default rule is
  `check` (browser approval), which a server can never give; an approval lasts
  12 hours, so fleets often work for a while and then fail together.
- **Recognise it**: the port 22 banner is `SSH-2.0-Tailscale`; from the server,
  `ssh <user>@<node tailnet IP>` hangs or prints "requires an additional check";
  EdgeShell shows the nodes as "Tailscale check" under Automatic logins paused.
- **Fix**: an `accept` rule for the EdgeShell server that **no `check` rule also
  matches** (a matching `check` overrides `accept`, whatever the order), or
  tags, or `tailscale set --ssh=false` on the nodes. Exact rules and safe steps:
  [VPN-NETWORK.md](VPN-NETWORK.md#tailscale-ssh-on-the-nodes).
- **Removing and re-adding the nodes does not help**: the block is in the
  tailnet, not in EdgeShell.

### The web app crashes and restarts; logs say "Too many open files"

- **Cause** (fixed in 1.7.1): failed SSH logins left their half-open connections
  behind. With a few dozen failing nodes the process reached the 1024 file
  handle limit in about three hours.
- **Now**: every SSH connection goes through `ssh_service.connect`, which closes
  it on any failure; a CI test fails the build if code opens SSH another way.
- **Check on a running server**: count the worker's open files in the container
  (`ls /proc/<pid>/fd | wc -l`).

### A tailnet policy change made through the API is undone

- **Cause**: an admin console editor window was open and saved its older copy of
  the whole policy file over the API change.
- **Prevention**: close console policy editors before changing the policy by
  API; save through the API with `If-Match` (the version you read) so a newer
  edit is never overwritten; read the policy back after saving.

### EdgeShell kept trying a wrong password

- **Now** (1.7.1, login policy): after 2 rejected passwords (or one Tailscale
  SSH refusal) automatic and bulk logins to that node stop, also across
  restarts, so nodes never see repeated failed logins (no fail2ban bans or
  lockouts). They resume on Fix login, Test, Retry paused logins, a changed
  SSH server, or (Tailscale) a successful canary login.

### sudo commands hang (for example the OpenRemoteGUI installer)

- **Cause**: sudo waited for a password on a terminal that was never answered;
  priming the credential cache with `sudo -v` did not carry across the pipe.
- **Now**: commands that use sudo get the node's stored password on stdin
  through a small `sudo()` wrapper (never on a command line). If sudo then fails
  with "incorrect password", the stored password is wrong: Fix login.

### OpenRemoteGUI shown as installed on nodes that do not have it

- **Cause**: detection matched Raspberry Pi OS's own `wayvnc` and
  `rpi-connectd`, and `pgrep -f` matched itself.
- **Now**: installed means its install folder exists and its page answers on
  port 6080.

### The OpenRemoteGUI install fails at `./install.sh`

- **Cause**: the cloned script has no execute bit.
- **Now**: the steps are `git clone`, `cd OpenRemoteGUI`, `sudo bash install.sh`.

## Database and data lake

### Which database is production using?

- **MariaDB** in the Docker stack (`DATABASE_URL`); SQLite only for development
  and tests. Settings, System Info shows the engine and its version (it used
  to be hardcoded and showed "SQLite"; fixed in 1.8.0).

### Permanently deleting a node fails on MariaDB

- **Cause** (fixed in 1.7.2): monitoring history rows still referenced the node.
- **Now**: the node's history is deleted with it.

### Reads right after a purge still see the old rows

- **Cause**: MariaDB's REPEATABLE READ isolation keeps a session on its snapshot
  while the purge runs on another connection.
- **Now**: the purge ends the reading transaction first and refreshes the
  session afterwards.

### The data lake is behind or not replicating

- **Check**: Settings, Data lake (lag in seconds), or
  `docker compose exec mariadb-lake mariadb -uroot -p -e 'SHOW REPLICA STATUS\G'`
  (`Slave_IO_Running` and `Slave_SQL_Running` must be `Yes`).
- **Safe by design**: while the lake is behind or stopped, history reads fall
  back to the hot database and **no purge deletes anything**.
- **Restart replication**: `docker compose up -d lake-init` (idempotent). If the
  lake was down longer than the hot binary logs are kept (7 days), stop it,
  move `data/mariadb-lake` aside and run `docker compose up -d` to copy it
  fresh.

## Web app

### The authenticator app says the 2FA QR code is invalid

- **Cause** (fixed): the QR SVG had no `viewBox` and was cropped.
- **Workaround on any version**: type the key shown under the QR code.

### Select All, toasts and the terminal stopped working on every page

- **Cause** (fixed): one apostrophe in a message broke the shared page script.
- **Now**: CI checks every page's scripts with `node --check` and fails if a
  page defines the same function twice.

### Log Viewer returned 500

- **Cause** (fixed): `.gitignore` rule `logs/` also hid `app/templates/logs`, so
  the image had no template. Now anchored as `/logs/`, and a test checks every
  template exists.

### Bulk import of a large file returned 502

- **Cause** (fixed): the staged rows went into the session cookie, which nginx
  refused. Rows are now staged on the server; nginx proxy buffers raised.

### On a phone there was no menu, and the terminal output was a thin strip

- Fixed in 1.7.0. A browser test now checks every page on phone, tablet, laptop
  and desktop sizes and at 67 to 200 percent zoom on every pull request.

### DELETE could not be typed in the Recycle Bin's confirmation

- **Cause** (fixed in 1.7.3): the open side panel kept the keyboard focus.

### The Monitoring "Auto" switch turned itself back on

- **Cause** (fixed in 1.7.3): the choice was never saved; and an old duplicate
  `pollAll` polled every node live, one request per node, every 15 seconds,
  flooding the browser (`ERR_INSUFFICIENT_RESOURCES`). The choice is now kept
  on the account, and the page reads the collector's results in one request.

### Settings showed security options that did nothing

- **Cause** (fixed in 1.8.0): login attempts, rate limit, blocked commands, sudo
  confirmation and command audit were stored but never used. Limits are now
  shown read-only as the server enforces them; blocked commands are enforced in
  the fleet terminal and the API.

## Development and CI

### Windows: paths like `/api/v1/` become `C:/Program Files/Git/...`

- Git Bash rewrites arguments that look like paths. Prefix the command with
  `MSYS_NO_PATHCONV=1`, or run it from PowerShell.

### Bandit fails to parse files

- Bandit may not support the newest Python yet. CI runs it on Python 3.12; run
  it locally the same way (`docker run --rm -v "$PWD:/src" -w /src python:3.12-slim ...`).

### Tests flaky around threads or time

- An in-memory SQLite database shares one connection between threads, so
  background jobs lost writes: tests use a temporary SQLite file. TOTP tests use
  a code step nobody used yet (each code works once).

### A secret was pasted into a chat, ticket or log

- Treat it as exposed: revoke it at the source (Docker Hub token, Tailscale
  OAuth client, EdgeShell API key, SSH password) and create a new one. Prefer
  putting secrets in a file only the receiving tool reads.
