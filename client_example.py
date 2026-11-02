"""Call the local NumVerify Pro API with an API key.

Start the server first (start_portal.bat / ./start_portal.sh), then run:
    python client_example.py "(415) 555-2671"
"""

import sys

import requests

url = "http://127.0.0.1:8000/validate"
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "val_live_215581c44c20f5a1ef9e114ea47c29419bd6fddb",
}
payload = {
    "phone_number": sys.argv[1] if len(sys.argv) > 1 else "(415) 555-2671",
    # auto    = live provider when configured, offline fallback otherwise
    # live    = require a live carrier lookup (Telnyx / Twilio)
    # offline = never send the number to a paid provider
    "lookup_mode": "auto",
}

response = requests.post(url, json=payload, headers=headers, timeout=30)
response.raise_for_status()
result = response.json()

print(f"Number        : {result['national_format']}  ({result['e164']})")
print(f"Status        : {result['numberStatus']}  (valid={result['valid']})")
print(f"Line type     : {result['lineType']} / {result['lineSubtype']}")
print(f"Carrier       : {result['carrier']}")
print(f"Location      : {result['location']}")
print(f"Ported        : {result['portedStatus']}")
print(f"Source        : {result['lookupSource']}  [{result['dataFreshness']}]")
print(f"Confidence    : {result['classificationConfidence']}")
print(f"Note          : {result['classificationNote']}")
if result.get("lookupError"):
    print(f"Lookup error  : {result['lookupError']}")
