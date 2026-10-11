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

# 2. Install the driver and point at the new DB
pip install psycopg2-binary                       # or PyMySQL for MariaDB
export DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/edgeshell

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
| MariaDB / MySQL | `mysql+pymysql://user:pass@host:3306/edgeshell` |

## What the snapshot contains

Users, nodes, settings, activity logs, node logs, deployments, system metrics,
and VPN status, in a foreign-key-safe order. It is plain JSON; you can inspect
it before importing.
