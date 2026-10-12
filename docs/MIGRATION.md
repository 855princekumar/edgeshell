# Database Migration

EdgeShell starts on SQLite (WAL enabled automatically) and moves to PostgreSQL
or MariaDB with a two-step export/import when the fleet grows. Because every
table is defined as a SQLAlchemy model, the process is engine-agnostic.

## When to move

| Signal | Action |
|---|---|
| Single host, small fleet | Stay on SQLite (WAL) |
| Many concurrent writers, HA, or a shared DB | Move to PostgreSQL |
| Existing MySQL/MariaDB operations | Move to MariaDB |

## Export / import (recommended)

```bash
# 1. On the current (SQLite) deployment
python manage.py db-export snapshot.json

# 2. Point at the new DB (the MariaDB driver is already installed;
#    for PostgreSQL install it first: pip install psycopg2-binary)
export DATABASE_URL=mysql+mysqlconnector://user:pass@host:3306/edgeshell?charset=utf8mb4
# or: export DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/edgeshell

# 3. Load into the new DB
python manage.py db-import snapshot.json --wipe
```

`db-import` upserts by primary key (`session.merge`), so it is idempotent and
safe to re-run. Encrypted node passwords travel as ciphertext, so keep the same
`CREDENTIAL_KEY` with the snapshot, or run `manage.py reencrypt --old <KEY>`
afterward.

### PostgreSQL sequence note

Because rows are inserted with explicit primary keys, Postgres sequences are not
advanced automatically. After a load, reset them once:

```sql
SELECT setval(pg_get_serial_sequence('nodes','id'), (SELECT MAX(id) FROM nodes));
SELECT setval(pg_get_serial_sequence('users','id'), (SELECT MAX(id) FROM users));
-- repeat for other tables with a serial id
```

SQLite and MariaDB do not need this step.

## URL formats

| DB | DATABASE_URL |
|---|---|
| SQLite | `sqlite:///instance/edgeshell.db` |
| PostgreSQL | `postgresql+psycopg2://user:pass@host:5432/edgeshell` |
| MariaDB / MySQL (TCP) | `mysql+mysqlconnector://user:pass@host:3306/edgeshell?charset=utf8mb4` |
| MariaDB (local socket, as `run.py` sets it) | `mysql+mysqlconnector://user:pass@localhost/edgeshell?unix_socket=/run/mysqld/mysqld.sock&charset=utf8mb4` |

## What the snapshot contains

Users, nodes, settings, issues and their messages, activity logs, node logs,
deployments, system metrics, VPN status and service events, in a
foreign-key-safe order. Live sign-in sessions are not exported: everyone signs
in again after a migration. It is plain JSON; you can inspect it before
importing.

## Automatic migration on EC2

You rarely need the steps above on a server: `python3 run.py` finds an existing
`instance/edgeshell.db`, copies it into MariaDB once (`manage.py migrate-sqlite`)
and renames the file to `.migrated-<timestamp>`.
