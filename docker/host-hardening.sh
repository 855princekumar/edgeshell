#!/usr/bin/env bash
# Harden a Docker host that runs the EdgeShell stack (docker/compose.yaml).
# Idempotent: safe to run again after changes.
#
#   cd ~/edgeshell && sudo bash host-hardening.sh [--admin-ip 203.0.113.7]
#
# What it does
#   SSH     key-only, no root login, 3 attempts, short grace time, no
#           forwarding or X11, only the invoking user may log in. Validated
#           with `sshd -t` before reloading; existing sessions stay open.
#   fail2ban
#     sshd              3 failures in 10 min -> 1 h ban, doubling up to 1 week
#     recidive          banned 3 times in a day -> 1 week, every port
#     nginx-limit-req   tripping nginx's sign-in rate limit -> 1 h ban
#     edgeshell-probe   scanning for .env, .git, wp-login, phpMyAdmin, ... -> 1 day
#     Web bans go into Docker's DOCKER-USER chain: published container ports
#     bypass the host's INPUT chain (and ufw), so that is where they must go.
#   Logs     nginx access/error logs in ./data/nginx-logs, rotated daily, 14 kept.
#   Updates  unattended security upgrades on.
#
# --admin-ip   never ban this address (your office or home IP). Optional.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
STACK_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$STACK_DIR/data/nginx-logs"
SSH_USER="${SUDO_USER:-ubuntu}"
ADMIN_IP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-ip) ADMIN_IP="${2:-}"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "Packages: fail2ban, unattended-upgrades"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends fail2ban unattended-upgrades python3-systemd >/dev/null

say "SSH policy (key-only, $SSH_USER only)"
# 01- sorts before the cloud image's drop-ins; the first value sshd reads wins.
cat > /etc/ssh/sshd_config.d/01-edgeshell-hardening.conf <<EOF
# Managed by EdgeShell docker/host-hardening.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 30
MaxStartups 10:30:60
MaxSessions 4
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
ClientAliveInterval 300
ClientAliveCountMax 2
AllowUsers $SSH_USER
EOF
chmod 644 /etc/ssh/sshd_config.d/01-edgeshell-hardening.conf
if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
else
  rm -f /etc/ssh/sshd_config.d/01-edgeshell-hardening.conf
  echo "sshd rejected the policy; it was removed and nothing changed." >&2
  exit 1
fi

say "nginx logs for fail2ban ($LOG_DIR)"
mkdir -p "$LOG_DIR"
touch "$LOG_DIR/access.log" "$LOG_DIR/error.log"
cat > /etc/logrotate.d/edgeshell-nginx <<EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

say "fail2ban filters and jails"
cat > /etc/fail2ban/filter.d/edgeshell-probe.conf <<'EOF'
# Requests for files that exist on common stacks but never on EdgeShell.
[Definition]
failregex = ^<HOST> \S+ \S+ \[[^\]]+\] "(GET|POST|HEAD|PUT) /[^"]*(\.env|\.git/|\.aws/|\.ssh/|wp-login|wp-admin|xmlrpc\.php|phpmyadmin|/cgi-bin/|\.php|/vendor/phpunit|/actuator|/solr/|/boaform|/HNAP1)[^"]*" (400|401|403|404|405|444)
ignoreregex =
EOF

ignore="127.0.0.1/8 ::1 172.16.0.0/12"
[ -n "$ADMIN_IP" ] && ignore="$ignore $ADMIN_IP"
cat > /etc/fail2ban/jail.d/edgeshell.local <<EOF
# Managed by EdgeShell docker/host-hardening.sh
[DEFAULT]
ignoreip = $ignore
bantime = 1h
findtime = 10m
maxretry = 3
bantime.increment = true
bantime.factor = 2
bantime.maxtime = 1w
banaction = iptables-multiport
banaction_allports = iptables-allports

[sshd]
enabled = true
mode = aggressive
port = ssh
backend = systemd

[recidive]
enabled = true
logpath = /var/log/fail2ban.log
bantime = 1w
findtime = 1d
maxretry = 3
banaction = iptables-allports

# Web jails: ban in DOCKER-USER, where Docker's published ports are filtered.
[nginx-limit-req]
enabled = true
logpath = $LOG_DIR/error.log
backend = polling
port = http,https
action = iptables-multiport[name=%(__name__)s, port="80,443", protocol=tcp, chain=DOCKER-USER]

[edgeshell-probe]
enabled = true
filter = edgeshell-probe
logpath = $LOG_DIR/access.log
backend = polling
maxretry = 3
bantime = 1d
port = http,https
action = iptables-multiport[name=%(__name__)s, port="80,443", protocol=tcp, chain=DOCKER-USER]
EOF

fail2ban-client -t >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl restart fail2ban
sleep 2

say "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

say "Status"
fail2ban-client status | sed 's/^/  /'
echo "  SSH: $(sshd -T | grep -E '^(passwordauthentication|permitrootlogin|maxauthtries|allowusers) ' | tr '\n' ' ')"
echo
echo "Keep this SSH session open and test a new login from another terminal before closing it."
echo "Unban an address: sudo fail2ban-client set <jail> unbanip <ip>"
