#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
  cp ".env.example" ".env"
fi

echo "Add either your Telnyx key or Twilio credentials, save, then restart NumVerify Pro."
"${EDITOR:-nano}" ".env"
