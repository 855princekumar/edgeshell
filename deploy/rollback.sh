#!/usr/bin/env bash
# Roll back to the previous release recorded by update.sh.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REL_DIR="$APP_DIR/releases"
cd "$APP_DIR"
if [ ! -f "$REL_DIR/previous" ]; then echo "No previous release recorded."; exit 1; fi
PREV="$(cat "$REL_DIR/previous")"
SHA="$(./venv/bin/python -c "import json;print(json.load(open('$REL_DIR/$PREV/manifest.json'))['git_sha'])" 2>/dev/null || echo '')"
[ -n "$SHA" ] && [ "$SHA" != "nogit" ] && git checkout "$SHA" 2>/dev/null || echo "git checkout skipped"
./venv/bin/pip install --quiet -r requirements.txt
cp "$REL_DIR/previous" "$REL_DIR/current"
sudo systemctl restart edgeshell.service 2>/dev/null || true
echo "==> Rolled back to release $PREV ($SHA)"
