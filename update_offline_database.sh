#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Run ./start_portal.sh once before updating the database."
  exit 1
fi

.venv/bin/python update_offline_database.py
echo "Update complete. Restart the portal to load the refreshed database."
