# Apps on the edge

How to build, ship, update, roll back and remove a service or app on the
fleet, the same way every time, without touching anything else on a node
([ADR 0017](adr/0017-apps-on-the-edge.md)).

## The rules in one screen

| Rule | Why |
| ---- | --- |
| Files from EdgeShell land only in `~/Downloads/edgeshell` of the node's SSH user | one known place, nothing scattered over the system |
| Apps live only in `/opt/edgeshell/apps/NAME` | one folder per app; removing it removes the app |
| Each app runs as its own systemd service `es-NAME`, as its own user `es-NAME` | starts at boot, restarts on failure, logs in the journal, cannot touch other apps |
| Python packages only in the version's own venv | nothing installed system-wide, no clash between apps or versions |
| Code is read-only for the app; it writes only to `$APP_DATA` | a bug cannot damage its own code or the system |
| A version never changes; a version that failed on a node is never installed there again | what ran is what was audited |
| The newest 3 stable versions always stay on the node | one-click rollback, even offline |
| Every package uploaded stays in EdgeShell's bucket until an administrator removes it | audit: what was sent, by whom, to which nodes, with what result |

## 1. Build the package

A folder with your app and a `manifest.json`:

```
my-sensor/
  manifest.json
  requirements.txt      optional: Python packages for the venv
  app/main.py
  wheels/               optional: wheel files, for nodes without internet
```

```json
{
  "name": "my-sensor",
  "version": "1.0.0",
  "description": "Reads the sensor and publishes it",
  "run": "python app/main.py",
  "requirements": "requirements.txt",
  "env": {"INTERVAL_S": "10"},
  "health": {"within_s": 20, "http": "http://127.0.0.1:8080/health"},
  "stable_after_s": 600,
  "groups": ["dialout"],
  "ports": [8080],
  "memory_max": "256M",
  "cpu_quota": "50%",
  "restart_sec": 5
}
```

| Key | Required | Meaning |
| --- | -------- | ------- |
| `name` | yes | 3 to 32 of `a-z 0-9 -`, starting with a letter. The service is `es-NAME`. |
| `version` | yes | Like `1.2.0` (up to four numbers, an optional `-suffix`). Never reused. |
| `run` | yes | The command, relative to the version folder. `python` means the version's venv. |
| `requirements` | no | A pip requirements file in the package. Installed as the app user, into the venv only. With a `wheels/` folder, pip installs offline from it. |
| `env` | no | Plain settings (`KEY` in capitals). Secrets do not belong in a package. |
| `health` | no | `within_s` (5 to 300, default 20): how long the service must stay up after a switch. Optional `http` (a local URL that must answer) or `cmd` (a command that must succeed, run as the app user). |
| `stable_after_s` | no | After this long (default 600 s) without a restart, the version counts as stable. |
| `groups` | no | Hardware access: `dialout gpio i2c spi video audio plugdev input bluetooth netdev`. |
| `ports`, `memory_max`, `cpu_quota`, `restart_sec`, `description` | no | Shown in EdgeShell; limits for systemd. |

Every app also gets `APP_NAME`, `APP_VERSION` and `APP_DATA` in its environment.

Pack it on any computer with Python 3 (download `esapp.py` from the Apps page):

```
python3 esapp.py pack my-sensor          # my-sensor-1.0.0.esapp
python3 esapp.py check my-sensor-1.0.0.esapp
```

`pack` leaves out `.git`, `venv`, `__pycache__`, `node_modules` and links, and
checks the result the way a node will. Packages with links, devices or paths
outside the folder are refused everywhere.

An example: [examples/apps/hello-sensor](../examples/apps/hello-sensor). EdgeShell's own
MQTT heartbeat agent is an app too ([HEARTBEAT.md](HEARTBEAT.md), ADR 0018): its package,
built from the agent in the image, is in the bucket as `heartbeat`.

## 2. Upload and roll out

**Apps, Packages, Upload package** (administrators). EdgeShell checks it, keeps
it in the bucket (`data/uploads/packages/NAME/VERSION/`) with its checksum, and
records who uploaded it.

**Roll out**: pick the nodes, the number of canary nodes (default 1) and when
to stop (default: more than 20 % of finished nodes failed).

1. The canary nodes go first, one at a time. If any of them fails, the rest of
   the fleet is not touched.
2. Then the rest, 4 at a time. Offline nodes are skipped (roll out again later).
3. EdgeShell checks the package against its checksum before anything is sent.

On each node:

1. esapp itself is set up or updated in `/opt/edgeshell/bin/esapp` if needed.
2. The package goes to `~/Downloads/edgeshell`, esapp checks its checksum and
   moves it to `/opt/edgeshell/apps/NAME/ota/`.
3. The new version is built next to the running one (`versions/.build-VERSION`):
   files, venv, its unit, and its own `install.sh`, `rollback.sh`,
   `uninstall.sh`. Only a complete build is renamed to `versions/VERSION`; a
   failed build leaves nothing and the running version keeps running.
4. **The switch is atomic**: `current` points to the new version in one step,
   the unit is replaced in one step, the service restarts.
5. **The watch**: the service must stay up for `health.within_s` without a
   crash (and answer its health check). If not, `current` goes back to the
   previous version by itself and the new one is marked failed. The job log
   says "not healthy (reason): rolled back to X".

## 3. On the node

```
/opt/edgeshell/
  bin/esapp
  apps/my-sensor/
    versions/1.0.0/       read-only: app files, venv/, manifest.json, es-my-sensor.service,
                          install.sh, rollback.sh, uninstall.sh
    versions/1.1.0/
    current -> versions/1.1.0
    data/                 $APP_DATA, kept across versions
    ota/                  every package received, as received
    state.json            each version: installed, active, stable or failed, and when
    esapp.log             what esapp did
  system.json             shared system changes esapp made (python3-venv, if it had to)
/etc/systemd/system/es-my-sensor.service
```

The unit runs from `current`, as user `es-my-sensor`, with `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, `NoNewPrivileges` and only `data/` writable.
Logs: `journalctl -u es-my-sensor`.

Permissions: `/opt/edgeshell` and `apps/` can be passed through but not listed;
an app's folder and versions are `root:es-NAME`, readable only by its own
user (never writable by it); `data/` belongs to the app's user alone (0700);
esapp, the OTA copies, the per-version scripts and the unit are root only.
Other apps and users on the node cannot read an app's code or data.

On the node, by hand (as root), every version carries its own scripts, and they
work even if `/opt/edgeshell/bin/esapp` changes later:

```
sudo /opt/edgeshell/apps/my-sensor/versions/1.0.0/install.sh     # switch back to 1.0.0
sudo /opt/edgeshell/apps/my-sensor/current/rollback.sh           # back to the newest stable before
sudo /opt/edgeshell/apps/my-sensor/current/uninstall.sh          # remove the app completely
sudo /opt/edgeshell/bin/esapp inventory                          # what is installed (JSON)
```

## 4. Roll back, switch, clean up, remove

**Apps, On the nodes** shows each node's apps, the running version, the service
state and every version with its state. **Read nodes** reads them again.

| Action | What happens on the node |
| ------ | ------------------------ |
| **Roll back** (one click) | Back to the newest stable version before the running one (watched like any switch). The version rolled back from is marked failed and removed; its OTA copy stays for the audit. |
| **Switch** | To any version still on the node. |
| **Clean up** | Pick versions to remove with their OTA copies. The running version and the newest 3 stable versions cannot be picked. |
| **Remove** | Stops and removes the service, every version, every OTA copy and the app's user. The data goes too, unless you keep it (then it moves to `/opt/edgeshell/kept/`). Nothing outside `/opt/edgeshell` and the unit file is touched. |

**Audit** lists every upload, rollout and node action with the result per node.
**Remove from bucket** (after the audit) deletes the package file from the
server; the records stay.

## 5. What esapp never does

- Install Python packages system-wide (only `python3-venv` itself, once, when a
  node lacks it; recorded in `system.json` and never removed by one app).
- Write outside `/opt/edgeshell`, `/etc/systemd/system/es-NAME.service` and the
  `es-NAME` user.
- Run an app as root, or let it write to its own code.
- Delete the running version or the newest 3 stable versions in a cleanup.
- Reinstall a version that failed on that node.

## Tested

`tests/esapp_e2e.py` runs on a Debian 12 node with systemd in CI (job
**esapp**), with real services and a real venv:

| Check | Result |
| ----- | ------ |
| First version: package to `~/Downloads/edgeshell`, venv, hardened service, healthy | about 18 s |
| A version that crashes rolls itself back, and the earlier version serves again | about 22 s |
| Other users cannot read the app, its data or esapp | checked |
| One-click rollback to the newest stable version before | about 12 s |
| Full removal: no folder, user or service left, the rest of the system exactly as before | under 1 s |

Plus a package that tries to write outside its folder is refused; the app
cannot change its own code; its data survives updates; cleanup keeps the
running and newest stable versions; a failed build is never installed again.
