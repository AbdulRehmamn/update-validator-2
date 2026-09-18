#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3.10 or newer, then run this file again."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating the local Python environment..."
  python3 -m venv .venv
fi

echo "Installing or checking required packages..."
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

if [ ! -f "nanpa_prefixes.sqlite3" ]; then
  echo "Building the offline NANPA prefix database..."
  .venv/bin/python update_offline_database.py || echo "Database update failed; continuing with basic validation."
fi

if [ -f ".env" ]; then
  echo "Live lookup settings detected. The configured provider will be used."
else
  echo "Offline fallback mode. Run ./configure_live_lookup.sh for current carrier data."
fi
echo "Starting NumVerify Pro at http://127.0.0.1:8000"
echo "Keep this window open. Press Ctrl+C to stop the portal."
.venv/bin/python app.py
