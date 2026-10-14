#!/bin/sh
# EdgeShell data lake: make mariadb-lake a live replica of mariadb (the hot
# database). One-shot and idempotent: runs on every `docker compose up`.
#
#   first run   creates the replication user, copies the edgeshell database
#               to the lake (consistent snapshot with its GTID position) and
#               starts replication
#   later runs  makes sure replication is running and reports its state
#
# Only the edgeshell database is replicated: privacyIDEA's 2FA secrets stay
# on the hot database alone. The app's hot-database purges run with
# sql_log_bin=0, so the lake never receives them and keeps every row.
set -eu

PW="${MARIADB_ROOT_PASSWORD:?}"
# The replication password is derived from the root password, so .env needs
# no new secret; it is only valid on the internal database network.
REPL_PW=$(printf 'edgeshell-lake-repl:%s' "$PW" | sha256sum | cut -c1-40)

hot() { mariadb -h mariadb -u root -p"$PW" "$@"; }
lake() { mariadb -h mariadb-lake -u root -p"$PW" "$@"; }

if [ "$(hot -N -e 'SELECT @@log_bin')" != "1" ]; then
    echo "lake-init: the binary log is off on mariadb; it must run with --log-bin (see compose.yaml)" >&2
    exit 1
fi

hot -e "
CREATE USER IF NOT EXISTS 'lake_repl'@'%' IDENTIFIED BY '$REPL_PW';
ALTER USER 'lake_repl'@'%' IDENTIFIED BY '$REPL_PW';
GRANT REPLICATION SLAVE ON *.* TO 'lake_repl'@'%';
GRANT BINLOG ADMIN ON *.* TO 'edgeshell'@'%';"

if lake -e 'SHOW REPLICA STATUS\G' | grep -q 'Master_Host: mariadb'; then
    lake -e 'START REPLICA' 2>/dev/null || true
    sleep 2
    io=$(lake -e 'SHOW REPLICA STATUS\G' | sed -n 's/^ *Slave_IO_Running: //p')
    sql=$(lake -e 'SHOW REPLICA STATUS\G' | sed -n 's/^ *Slave_SQL_Running: //p')
    err=$(lake -e 'SHOW REPLICA STATUS\G' | sed -n 's/^ *Last_Error: //p')
    behind=$(lake -e 'SHOW REPLICA STATUS\G' | sed -n 's/^ *Seconds_Behind_Master: //p')
    echo "lake-init: replication io=$io sql=$sql behind=${behind}s ${err:+error: $err}"
    exit 0
fi

echo "lake-init: first run, copying the hot database to the lake"
lake -e 'STOP REPLICA' 2>/dev/null || true
mariadb-dump -h mariadb -u root -p"$PW" --single-transaction --master-data=2 --gtid \
    --routines --triggers --databases edgeshell > /tmp/hot.sql
gtid=$(grep -o "gtid_slave_pos='[^']*'" /tmp/hot.sql | head -1 | cut -d"'" -f2 || true)
lake < /tmp/hot.sql
rm -f /tmp/hot.sql
lake -e "
SET GLOBAL gtid_slave_pos = '${gtid}';
CHANGE MASTER TO MASTER_HOST='mariadb', MASTER_PORT=3306, MASTER_USER='lake_repl',
  MASTER_PASSWORD='$REPL_PW', MASTER_USE_GTID=slave_pos, MASTER_CONNECT_RETRY=10;
START REPLICA;"
sleep 3
lake -e 'SHOW REPLICA STATUS\G' | grep -E 'Slave_(IO|SQL)_Running:|Seconds_Behind_Master:|Last_Error:' | sed 's/^ */lake-init: /'
echo "lake-init: replication started from GTID '${gtid}'"
