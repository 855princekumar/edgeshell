#!/usr/bin/env bash
# Remove EdgeShell services and web config. Data is kept unless --purge-data.
#   sudo bash deploy/uninstall.sh              services + nginx/php config only
#   sudo bash deploy/uninstall.sh --purge-data also drops DB, users, /opt/edgeshell
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"
for u in edgeshell-heartbeat.timer edgeshell-heartbeat.service edgeshell-watchdog.service edgeshell.service; do
  systemctl disable --now "$u" 2>/dev/null || true
  rm -f "/etc/systemd/system/$u"
done
systemctl daemon-reload
rm -f /etc/nginx/sites-enabled/edgeshell.conf /etc/nginx/sites-enabled/edgeshell-pma.conf \
      /etc/nginx/sites-available/edgeshell.conf /etc/nginx/sites-available/edgeshell-pma.conf \
      /etc/nginx/conf.d/edgeshell-common.conf /etc/nginx/snippets/edgeshell-*.conf
rm -rf /etc/nginx/edgeshell /etc/phpmyadmin/conf.d/edgeshell.inc.php
rm -f /etc/php/*/fpm/pool.d/edgeshell-pma.conf /etc/sysctl.d/60-edgeshell.conf
nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
for v in /etc/php/*/fpm; do systemctl restart "php$(basename "$(dirname "$v")")-fpm" 2>/dev/null || true; done
if [ "${1:-}" = "--purge-data" ]; then
  read -r -p "Type DELETE to drop database 'edgeshell' and remove /opt/edgeshell: " a
  [ "$a" = "DELETE" ] || { echo "Aborted."; exit 1; }
  mariadb -e "DROP DATABASE IF EXISTS edgeshell; DROP USER IF EXISTS 'edgeshell_app'@'localhost'; DROP USER IF EXISTS 'edgeshell_dba'@'localhost';"
  rm -rf /opt/edgeshell /var/lib/edgeshell /etc/edgeshell /root/edgeshell-credentials.txt
  userdel edgeshell 2>/dev/null || true
  echo "EdgeShell removed including data."
else
  echo "Services and web config removed. Database, /opt/edgeshell and credentials kept."
fi
