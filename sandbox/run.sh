#!/usr/bin/env bash
# EdgeShell sandbox: EdgeShell + a 6-node Pi Fleet Emulator fleet
# (32-bit and 64-bit x Buster, Bullseye, Bookworm), then the full
# end-to-end suite, an Ansible run, image hardening checks and a restart.
#
#   sandbox/run.sh              build EdgeShell from this checkout, test, tear down
#   sandbox/run.sh --keep       same, but leave everything running afterwards
#   EDGESHELL_IMAGE=devprincekumar/edgeshell:1.5.1 sandbox/run.sh   test a published image
#
# Needs Docker with Compose v2, curl and Python 3. Exit code 0 = all passed.
set -euo pipefail
cd "$(dirname "$0")"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
PY="$(for c in python3 python; do "$c" -c 1 >/dev/null 2>&1 && { echo "$c"; break; }; done)"
[ -n "$PY" ] || { echo "Python 3 is required" >&2; exit 1; }

# Throwaway keys for this run only. Never written to disk.
export SANDBOX_SECRET_KEY="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
export SANDBOX_CREDENTIAL_KEY="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
export SANDBOX_MFA_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
export SANDBOX_MFA_PEPPER="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
COMPOSE=(docker compose -f docker-compose.yml)
ES=edgeshell-sandbox
SPAWNER=edgeshell-sandbox-spawner
EXPECTED_NODES="$("$PY" -c 'import json; print(len(json.load(open("fleet.json"))["nodes"]))')"
export EXPECTED_NODES
PW_FILE="$(mktemp)"
export ADMIN_PASSWORD_FILE="$PW_FILE"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
wait_url() {  # url seconds
  for _ in $(seq 1 "$2"); do curl -sf -o /dev/null "$1" && return 0; sleep 1; done
  echo "Timed out waiting for $1" >&2; return 1
}

cleanup() {
  rc=$?
  if [ $rc -ne 0 ]; then
    step "Failure diagnostics"
    docker logs --tail 80 "$ES" 2>&1 || true
    docker logs --tail 40 edgeshell-sandbox-privacyidea 2>&1 || true
    docker logs --tail 40 "$SPAWNER" 2>&1 || true
  fi
  rm -f "$PW_FILE"
  if [ $KEEP -eq 0 ]; then
    step "Tear down"
    docker exec "$SPAWNER" pfe reset --yes >/dev/null 2>&1 || true
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo; echo "Sandbox left running: EdgeShell http://127.0.0.1:5050  Spawner http://127.0.0.1:7070"
    echo "Stop it with: docker exec $SPAWNER pfe reset --yes && docker compose -f sandbox/docker-compose.yml down -v"
  fi
  exit $rc
}
trap cleanup EXIT

if [ -z "${EDGESHELL_IMAGE:-}" ]; then
  export EDGESHELL_IMAGE=edgeshell:sandbox
  step "Build EdgeShell image from this checkout"
  "${COMPOSE[@]}" build edgeshell
fi

step "Start EdgeShell ($EDGESHELL_IMAGE) and the Pi Fleet Emulator spawner"
"${COMPOSE[@]}" up -d --no-build
wait_url http://127.0.0.1:5050/healthz 120
wait_url http://127.0.0.1:7070/api/health 120

step "Spawn the emulated fleet ($EXPECTED_NODES nodes: 32/64-bit x buster/bullseye/bookworm)"
curl -sf -X POST -H 'Content-Type: application/json' --data @fleet.json \
     --max-time 1500 http://127.0.0.1:7070/api/spawn >/dev/null
docker exec "$SPAWNER" pfe validate --wait 240

step "End-to-end suite"
EDGESHELL_URL=http://127.0.0.1:5050 PFE_URL=http://127.0.0.1:7070 "$PY" e2e.py

step "Ansible inside the image (ansible-core + sshpass + sudo, per-host results)"
docker exec -i "$ES" python - <<'PY'
from app import create_app
from app.models.node import Node
from app.services import ansible_service as a
app = create_app('production')
with app.app_context():
    nodes = Node.query.filter_by(is_active=True).all()
    assert a._runner_available(), 'ansible-runner / ansible-playbook missing in image'
    res = a.run_shell(nodes, 'cat /etc/os-release | grep VERSION_CODENAME', become=True)
    bad = {k: v for k, v in res.items() if not v['success']}
    for k, v in sorted(res.items()):
        print(f"  {'PASS' if v['success'] else 'FAIL'}  {k}: {v['output'].strip() or v['error'][:120]}")
    assert not bad, f'Ansible failed on {sorted(bad)}'
    print(f'  Ansible: {len(res)}/{len(nodes)} hosts OK')
PY

step "Image hardening"
uid="$(docker exec "$ES" id -u)"; [ "$uid" = 10001 ] && echo "  PASS  runs as non-root uid 10001" || { echo "  FAIL  uid=$uid"; exit 1; }
docker exec "$ES" sh -c 'test ! -e /app/.env' && echo "  PASS  no .env in image"
docker exec "$ES" sh -c '! touch /app/app/.write-test 2>/dev/null' && echo "  PASS  application code is read-only"
leaks="$(docker exec "$ES" sh -c 'find / -xdev \( -name "*.db" -o -name ".env" -o -name "*.pem" -o -name "id_rsa*" -o -name "snapshot*.json" \) -not -path "/proc/*" -not -path "/app/instance/*" -not -path "/opt/venv/*" -not -path "/usr/*" -not -path "/etc/ssl/*" 2>/dev/null' || true)"
[ -z "$leaks" ] && echo "  PASS  no databases, keys or env files outside the data volume" || { echo "  FAIL  found: $leaks"; exit 1; }

pi_ports="$(docker port edgeshell-sandbox-privacyidea 2>/dev/null || true)"
[ -z "$pi_ports" ] && echo "  PASS  privacyIDEA publishes no ports (reachable only by EdgeShell)" || { echo "  FAIL  privacyIDEA ports: $pi_ports"; exit 1; }
pi_uid="$(docker exec edgeshell-sandbox-privacyidea id -u)"; [ "$pi_uid" != 0 ] && echo "  PASS  privacyIDEA runs as non-root uid $pi_uid" || { echo "  FAIL  privacyIDEA runs as root"; exit 1; }

step "Data survives a container restart"
docker restart edgeshell-sandbox-privacyidea >/dev/null
docker restart "$ES" >/dev/null
wait_url http://127.0.0.1:5050/healthz 120
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' edgeshell-sandbox-privacyidea)" = healthy ] && break; sleep 2
done
ADMIN_FILE="$PW_FILE" "$PY" - <<'PY'
import json, os, sys
sys.path.insert(0, '.')
from e2e import Client
saved = json.load(open(os.environ['ADMIN_FILE']))
c = Client('E2E-restart')
# A backup code: the 2FA enrollment must survive the restart too.
status, location, body = c.login('admin', saved['password'], code=saved['backup'])
if status == 200 and 'already signed in' in body:   # admin is limited to 1 device
    status, _, _ = c.post_form('/login/confirm', {})
assert status == 302, f'login after restart failed: {status}'
status, _, body = c.request('GET', '/api/v1/nodes')
import json; n = len(json.loads(body))
assert n == int(os.environ['EXPECTED_NODES']), f'{n} nodes after restart'
print(f'  PASS  admin password, 2FA enrollment and {n} nodes kept after restart')
PY

step "Sandbox: ALL PASSED"
