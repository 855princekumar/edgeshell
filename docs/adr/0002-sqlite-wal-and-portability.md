# 0002. SQLite WAL by default, export/import to any SQL

## Status
Accepted.

## Context
Small fleets are fine on SQLite, but the tool is multi-user, so the default
rollback-journal mode causes "database is locked" errors under concurrent
writes. Larger fleets will outgrow SQLite entirely.

## Decision
Enable WAL, `synchronous=NORMAL`, and `busy_timeout` via a SQLAlchemy engine
`connect` event, guarded to SQLite so it is a no-op on other engines. Provide a
model-driven JSON export/import (`manage.py db-export` / `db-import`) that moves
data to Postgres or MariaDB by changing `DATABASE_URL`.

## Consequences
- Concurrent multi-user use works out of the box on SQLite.
- Migration is a two-command operation, no ORM rewrite.
- Import upserts by primary key, so it is idempotent.
- Postgres sequences need a one-time reset after import (documented).
