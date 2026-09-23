#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Biofeedback Play"
echo

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -z "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Checking for Biofeedback Play updates..."
    if ! git pull --ff-only origin main; then
      echo "Update check failed; continuing with the installed version."
    fi
  else
    echo "Local tracked edits detected; skipping automatic update."
  fi
  echo
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -q -r requirements.txt
exec python biofeedback_play.py
