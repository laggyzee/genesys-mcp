#!/bin/bash
# provision_users.command — double-click launcher (macOS).
# Opens Terminal and runs provision_users.py in interactive mode.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
cd "$REPO"

if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

if [ -z "${PY:-}" ]; then
  echo "ERROR: Python 3 not found. Install Python 3, or create the repo venv (.venv)."
  echo
  read -r -p "Press Enter to close…"
  exit 1
fi

"$PY" scripts/provision_users.py --interactive || true
echo
read -r -p "Press Enter to close…"
