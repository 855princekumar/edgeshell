# 0017: Apps on the edge: one folder, one service, versions side by side, a way back

- **Status:** Accepted (1.16.0)
- **Date:** 2026-10-19

## Context

Services reached the nodes in many ways: Deploy Files to any path (with
`sudo cp` into `/opt`, `/etc` or `/usr/local/bin`), scripts run by hand,
`pip install` system-wide. Nothing recorded what was installed, an update
overwrote the running version in place, a broken update stayed broken, and
removing a service meant guessing what it had left behind.

OpenRemoteGUI already showed a better way on the node (its ADR-0007): record
every change, undo exactly that, keep restore points outside the delete
boundary. EdgeShell's own deploys do the same on the server (ADR 0007). The
fleet needed it for any app, driven from EdgeShell, safe on hundreds of nodes.

## Decision

**esapp** (`app/agents/esapp/esapp.py`, Python 3.7+, standard library only) is
the one way apps are installed on nodes. EdgeShell sets it up in
`/opt/edgeshell/bin` and drives it over SSH; the same file packs and checks
packages on any computer and in EdgeShell.

1. **One place for uploads, one place for apps.** EdgeShell writes files only
   to `~/Downloads/edgeshell` of the SSH user (Deploy Files too: no more
   arbitrary paths). Apps live only in `/opt/edgeshell/apps/NAME`. The only
   files outside are the unit `es-NAME.service` and the user `es-NAME`.
2. **A package is a checked tarball** with `manifest.json`. Links, devices and
   paths outside the folder are refused; the manifest is strict (unknown keys,
   unsafe commands, hostile environment values and non-local health URLs are
   refused). Versions are immutable: same name and version, other contents,
   is refused on the server and on the node.
3. **Versions side by side, an atomic switch.** A version is built in a hidden
   folder (files, its own venv with packages installed as the app user, its
   unit, its own install/rollback/uninstall scripts) and renamed into place
   only when complete. `current` is a symlink replaced in one rename; the unit
   is replaced in one rename.
4. **Watched, and reverted by itself.** After a switch the service must stay up
   for `health.within_s` without a crash and pass its health check, or the
   previous version comes back and the new one is marked failed (never
   installed again on that node). After `stable_after_s` without a restart a
   version is stable.
5. **Always a way back.** The running version and the newest 3 stable versions
   are never removed by a cleanup. One-click rollback goes to the newest
   stable version before the running one and removes the one rolled back
   from (its OTA copy stays).
6. **Isolated services.** Each app runs as its own system user, from read-only
   code, writable only in its `data/`, with `ProtectSystem=strict`,
   `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, optional memory and CPU
   limits, restart on failure and logs in the journal.
7. **Full, exact removal.** Uninstall removes the service, the unit, the app
   folder (versions, OTA copies, data unless kept) and the user esapp created.
   It refuses any folder outside `/opt/edgeshell/apps`.
8. **Server side: a bucket and an audit.** Every package uploaded is kept with
   its checksum until an administrator removes it after the audit; every
   rollout and node action is recorded with the result per node.
9. **Careful rollouts.** Canary nodes first (one at a time; any failure stops
   the rollout), then the rest a few at a time, stopping by itself when more
   than a set share of finished nodes failed. The package's checksum is
   checked before anything is sent; offline nodes are skipped.

Roles: operators see everything and read nodes again; administrators upload,
roll out, roll back, switch, clean up, remove and empty the bucket.

## Alternatives considered

- **Docker on every node:** heavy for small boards, a second runtime to keep
  patched, and not on every node; systemd is.
- **apt packages from a private repository:** needs a repository, signing and
  root-run maintainer scripts; rollback is not built in.
- **Overwrite in place with a backup copy:** not atomic, and a failed update
  leaves a broken service until someone notices.
- **One shared venv per node:** apps would break each other on upgrades.

## Consequences

- Any team can ship an edge app the same way: a folder, a manifest, `esapp.py
  pack`, upload, roll out. No SSH by hand, nothing installed system-wide.
- A bad version costs one canary node about 20 seconds, then it is back on
  the previous version by itself; the fleet is not touched.
- Disk: each node keeps at least 3 stable versions per app with their venvs;
  Clean up removes the rest.
- Deploy Files no longer writes outside `~/Downloads/edgeshell`. Anything that
  must land elsewhere becomes an app.
- Next: ship the MQTT heartbeat agent as an esapp app, so its updates get the
  same atomic switch and automatic way back.
- Tests: `tests/test_esapp.py` (manifests, packages, the unit, what is kept),
  `tests/test_apps.py` (bucket, roles, canary and stop rules, safe commands,
  upload folder) and `tests/esapp_e2e.py` on a Debian 12 systemd node in CI.
