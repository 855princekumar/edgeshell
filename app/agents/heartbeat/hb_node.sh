#!/bin/bash
# EdgeShell's helper for the heartbeat agent on a node (ADR 0018). EdgeShell
# uploads it with the agent files into a private temp folder and runs it as
# root; the folder is removed afterwards.
#
#   hb_node.sh install PACKAGE SHA256 VERSION INTERVAL   (token and login files beside this script)
#   hb_node.sh test | login | config
#
# With systemd the agent is an esapp app: code and its own venv in
# /opt/edgeshell/apps/heartbeat/versions/VERSION, the node's settings in
# /opt/edgeshell/apps/heartbeat/data, the service es-heartbeat. A node with the
# classic install (/opt/edgeshell-heartbeat, apt packages) is moved over: the
# classic agent stops only for the switch and is removed once the new one runs;
# if the new one does not run, the classic one starts again. Without systemd
# the classic install stays (esapp apps are systemd services).
set -u
D="$(cd "$(dirname "$0")" && pwd)"
ESAPP=/opt/edgeshell/bin/esapp
A=/opt/edgeshell/apps/heartbeat
OLD=/opt/edgeshell-heartbeat
OLD_UNIT=/etc/systemd/system/edgeshell-heartbeat.service

hb() {  # setup.py of the running esapp agent, pointed at the app's data folder
  env HB_HOME="$A/data" HB_GROUP=es-heartbeat HB_UNIT=es-heartbeat "$@"
}
esapp_agent() { [ -L "$A/current" ]; }
classic() { [ -d "$OLD" ] || [ -f "$OLD_UNIT" ]; }

case "${1:-}" in
install)
  pkg="$2"; sha="$3"; ver="$4"; interval="$5"
  if [ ! -d /run/systemd/system ]; then
    rm -f "$pkg"
    echo "No systemd on this node: the classic install (background process)."
    exec python3 "$D/setup.py" --token-file "$D/token" --login-file "$D/login" --interval "$interval"
  fi
  # 1. the new version beside whatever runs now (nothing switches yet)
  python3 "$ESAPP" install "$pkg" --sha256 "$sha" --no-activate || { echo "HEARTBEAT_FAILED: esapp could not build $ver"; exit 1; }
  py="$A/versions/$ver/venv/bin/python"
  # 2. this node's settings into the app's data folder (the broker is tried on every way first)
  hb "$py" "$A/versions/$ver/app/setup.py" --files-only --token-file "$D/token" --login-file "$D/login" \
     --interval "$interval" || { echo "HEARTBEAT_FAILED: settings not written, nothing switched"; exit 1; }
  # 3. the switch: the classic agent pauses (one node identity sends from one place)
  was_classic=0
  if systemctl is-active --quiet edgeshell-heartbeat 2>/dev/null; then
    systemctl stop edgeshell-heartbeat; was_classic=1
  fi
  if pkill -f "$OLD/heartbeat_agent.py" 2>/dev/null; then was_classic=1; fi
  cur="$(basename "$(readlink "$A/current" 2>/dev/null)" 2>/dev/null)"
  if [ "$cur" = "$ver" ]; then
    systemctl restart es-heartbeat; sleep 8          # same version: new settings, a fresh start
    systemctl is-active --quiet es-heartbeat && ok=1 || ok=0
  else
    out="$(python3 "$ESAPP" activate heartbeat "$ver")"; echo "$out"
    echo "$out" | grep -q '^ESAPP_RESULT .*"ok": true' && ok=1 || ok=0
  fi
  # 4. keep what works
  if [ "$ok" = 1 ]; then
    if classic; then
      bash "$D/install.sh" --uninstall >/dev/null 2>&1 && \
        echo "Moved from the classic install: /opt/edgeshell-heartbeat, its service and user hbagent removed."
    fi
    echo "HEARTBEAT_OK $ver"
    echo "Done."
  else
    if [ "$was_classic" = 1 ]; then
      systemctl start edgeshell-heartbeat 2>/dev/null && echo "The classic agent runs again."
    fi
    echo "HEARTBEAT_FAILED: agent $ver did not stay up (esapp kept or restored the previous version)"
    exit 1
  fi
  ;;
test)
  if esapp_agent; then hb "$A/current/venv/bin/python" "$A/current/app/setup.py" --test
  elif [ -f "$OLD/setup.py" ]; then python3 "$OLD/setup.py" --test
  else echo NOT_INSTALLED_OR_OLD; exit 3; fi
  ;;
login)
  if esapp_agent; then hb "$A/current/venv/bin/python" "$A/current/app/setup.py" --set-credentials --login-file "$D/login"
  else python3 "$D/setup.py" --set-credentials --login-file "$D/login"; fi
  ;;
config)
  if esapp_agent; then hb "$A/current/venv/bin/python" "$A/current/app/setup.py" --merge-config "$D/config.yaml"
  elif [ -f "$OLD/setup.py" ]; then python3 "$OLD/setup.py" --merge-config "$D/config.yaml"
  elif [ -d "$OLD" ]; then
    install -m 0644 -o root -g root "$D/config.yaml" "$OLD/config.yaml.new" && mv -f "$OLD/config.yaml.new" "$OLD/config.yaml" && echo CONFIG_PUSHED
  else echo NOT_INSTALLED; exit 3; fi
  ;;
*)
  echo "usage: hb_node.sh install PACKAGE SHA256 VERSION INTERVAL | test | login | config"; exit 2
  ;;
esac
