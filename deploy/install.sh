#!/usr/bin/env bash
# Kept for compatibility. The installer is run.py (idempotent install/update).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec sudo python3 run.py "$@"
