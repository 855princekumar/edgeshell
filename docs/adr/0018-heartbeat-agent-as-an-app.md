# 0018: The heartbeat agent is an app

- **Status:** Accepted (1.17.0)
- **Date:** 2026-10-19

## Context

The MQTT heartbeat agent is on every node (ADR 0016), kept there by a routine
that installs, updates and repairs it. Its classic install broke the rules
apps follow since ADR 0017:

- `install.sh` installed `python3-paho-mqtt`, `python3-yaml` and
  `python3-cryptography` with apt, system-wide;
- an update overwrote `/opt/edgeshell-heartbeat` in place: a bad agent release
  would have reached every node the routine touched, with no way back but a
  manual fix.

The routine makes the second point serious: it updates the fleet by itself.

## Decision

**The agent is an esapp app named `heartbeat`.** Everything ADR 0017 gives an
app, the agent gets:

- its code and **its own venv** (paho-mqtt, cryptography, PyYAML from
  `requirements.txt`) in `/opt/edgeshell/apps/heartbeat/versions/VERSION`;
- user and service `es-heartbeat`, hardened, at boot, logs in the journal;
- the node's settings in `data/` (`agent.env`, `mqtt.env`, `config.yaml`,
  root:es-heartbeat 0640, the folder enterable by the agent's user only),
  kept across versions;
- an atomic switch watched for its health window, the previous version back
  by itself when the new one does not stay up, the newest 3 stable versions
  kept, the failed version never tried again on that node;
- the package in the Apps bucket (built from the agent in the image,
  reproducibly, so the same agent is always the same package), every install
  in the audit, and the Apps page's versions, rollback and removal.

**One helper on the node** (`hb_node.sh`, uploaded with each action and run as
root from a private temp folder) does install, test, login and settings for
both layouts:

1. esapp builds the new version beside whatever runs (`--no-activate`);
2. the agent's `setup.py --files-only` (agent 1.3) checks the token, tries the
   four ways to the broker with a real login and writes the settings into
   `data/` (`HB_HOME`, `HB_GROUP`, `HB_UNIT` point it at the app);
3. the classic agent, if any, stops for the switch (one node identity sends
   from one place);
4. the switch; if the new agent runs, the classic install is removed
   (`install.sh --uninstall`: folder, unit, user `hbagent`); if not, the
   classic agent starts again.

The OS packages a classic install used stay: they may have been there before
and other software may use them. Nodes without systemd keep the classic
install.

## Alternatives considered

- **Keep the classic install, add backups in install.sh:** a second way of
  doing what esapp already does, with its own bugs.
- **Remove the classic install first, then install:** a node with a broken
  new agent would be left with no heartbeat at all.
- **Purge the apt packages on the move:** they may predate the agent and be
  used by other software; removing them could break the node.
- **Bundle wheels in the package:** cryptography needs a wheel per CPU and
  Python version; pip on the node (piwheels on Raspberry Pi OS) is simpler,
  and a failed pip install fails the build before anything switches.

## Consequences

- An agent update can no longer take a node's heartbeat down: a version that
  does not stay up is rolled back on that node, and the routine leaves the
  node alone for a day (ADR 0016).
- The routine moves the fleet over by itself: nodes report agent 1.2, older
  than 1.3, so they get the update, 25 nodes per pass.
- Nodes need pip access for the venv (PyPI, or piwheels on Raspberry Pi OS)
  on the first install of each version; without it the build fails and the
  running agent stays.
- Tests: `tests/heartbeat_esapp_e2e.py` on a Debian 12 systemd node with a
  real broker in CI (classic install moved over, a broken first version
  leaving the classic agent running, the agent's own venv, private settings,
  the agent's test, a settings push, a bad update rolled back, a repair), and
  `tests/test_heartbeat.py` for what EdgeShell sends.
