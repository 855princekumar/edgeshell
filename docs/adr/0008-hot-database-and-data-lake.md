# 0008. Hot database plus a replicated, read-only data lake

## Status
Accepted (1.8.0, hardened in 1.8.1).

## Context
EdgeShell is the control plane for a whole fleet. Its database holds two kinds
of data with opposite needs:

- **Operational state** (nodes, users, sessions, the latest metrics) must be
  small and fast, because the collector writes every minute and every page
  reads it.
- **History** (per-minute metrics, hourly rollups, node events, the activity
  log, the API usage log) is what audits, incident reviews and future health
  prediction depend on. Losing it cannot be undone.

Keeping everything in one database forces a bad trade: either the hot
database grows without limit and slows down, or retention deletes the history.
An administrator mistake (clearing a log, deleting a node) or a compromised
admin account could also erase evidence.

Options considered:

| Option | Why not chosen |
|---|---|
| One database, keep everything | Grows without bound; every query and backup gets slower |
| Retention with periodic dumps | History lives in files nobody can query; gaps between dumps |
| ETL into a separate warehouse | Another system to run and secure; lag; schema drift |
| Object storage export | Not queryable by the app; needs cloud credentials on the host |
| **MariaDB replication into a read-only replica** | Same engine and schema, near-zero lag, no new credentials, queryable by the app |

## Decision
Run two MariaDB servers in the stack:

- **Hot database** (`mariadb`): what the app reads and writes.
- **Data lake** (`mariadb-lake`): a live GTID replica that keeps every row,
  on the internal network only, with no published port and `read_only=ON`.

Rules that make the lake trustworthy:

1. **Only the app's database is replicated.** The 2FA server's data (secrets)
   never leaves the hot database.
2. **Nothing the app does deletes from the lake.** Retention, clearing logs,
   deleting nodes or users and housekeeping of sessions and expired
   credentials run on the hot database only, on a connection with
   `sql_log_bin=0`, so the replica never sees them.
3. **No delete before the lake has the rows.** A delete is refused, before
   anything is changed, unless replication is healthy, at most 180 s behind,
   and the lake holds at least as many rows and the newest one.
4. **Deleted rows are renamed first** (`name~deleted-<id>`, cut on the name
   side so the suffix always survives), so a name can be reused without a
   duplicate key on the lake, which would stop replication.
5. **The app can only read the lake.** Its account there has SELECT only;
   changing the lake needs the root password, which lives only on the host.
6. **Unlogged connections are never reused dirty.** If turning the binary log
   back on fails, the connection is discarded instead of returned to the pool.
7. **Retention on the hot database is off by default.** Only an administrator
   can turn it on (week, month, quarter, semester or year).
8. Exports, history and the API read from the lake while it is caught up, and
   fall back to the hot database otherwise.

## Consequences
- History is kept for good and stays queryable by the app.
- Hot database size is controlled without losing data.
- Neither an administrator nor a compromised admin session can erase history
  through the app.
- A heartbeat table measures replication lag end to end. The UI shows it, and
  every delete checks it.
- Cost: a second database server (about 2 to 3 GB a year for 150 nodes), and
  deletes are refused while replication is down. That is intentional: the
  integrity of history matters more than freeing space.
- Accepted risk: whoever steals the hot database's app password can run
  statements that replicate. Disk snapshots of the lake cover this
  (ADR 0012, SECURITY.md).
- Verified on every change by the "Data lake replication" CI job against two
  real MariaDB servers (ADR 0011).
