#!/usr/bin/env bash
# Manifest-based release + rollback (mirrors the OpenRemoteGUI pattern).
# Records each deploy as releases/<timestamp>/manifest.json and points the
# 'current' marker at it, so rollback.sh can revert atomically.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
REL_DIR="$APP_DIR/releases"
TS="$(date +%Y%m%d-%H%M%S)"
GITSHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
mkdir -p "$REL_DIR/$TS"

git pull --ff-only 2>/dev/null || echo "no git remote; using working tree"
./venv/bin/pip install --quiet -r requirements.txt

cat > "$REL_DIR/$TS/manifest.json" <<JSON
{
  "version": "$(cat VERSION 2>/dev/null || echo 1.0.0)",
  "release": "$TS",
  "git_sha": "$GITSHA",
  "timestamp": "$(date -Is)",
  "requirements_sha": "$(sha256sum requirements.txt | cut -d' ' -f1)"
}
JSON

# Keep a pointer to the previous release for rollback
[ -f "$REL_DIR/current" ] && cp "$REL_DIR/current" "$REL_DIR/previous" || true
echo "$TS" > "$REL_DIR/current"

sudo systemctl restart edgeshell.service 2>/dev/null || true
echo "==> Deployed release $TS ($GITSHA)"
