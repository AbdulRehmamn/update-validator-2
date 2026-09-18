NUMVERIFY PRO v6.2 - NATIONWIDE LINE-TYPE & CARRIER INTELLIGENCE
========================================================

WHAT THIS VERSION DOES
- Separates Cell Phone, Landline, VoIP, Toll-Free, Fixed/VoIP subtypes,
  Premium Rate, Shared Cost, Pager, Voicemail, Personal, Universal Access,
  Satellite, Unknown, and Invalid numbers.
- Uses Telnyx live carrier and portability data when TELNYX_API_KEY is set.
- Uses Twilio Line Type Intelligence when Twilio credentials are set.
- Uses Telnyx first and Twilio as an error/unknown fallback when both are set.
- Shows current carrier, normalized/original carrier, ported status/date, LRN,
  OCN, SPID, MCC/MNC, city/state, lookup source, freshness, and confidence when
  the selected provider supplies those fields.
- Keeps the included NANPA prefix database as a clearly labelled offline
  fallback. It never silently presents a prefix estimate as live data.
- Correctly distinguishes Verizon regional fixed-line companies such as
  VERIZON NEW YORK, INC. from CELLCO / VERIZON WIRELESS mobile assignments.
- Expands offline carrier-family rules nationwide across Eastern, Central,
  Mountain, Pacific, Alaska, and Hawaii time zones, plus U.S. territories.
- Resolves major regional Bell, CLEC/fiber, cable/hosted VoIP, paging,
  satellite, and wireless carrier families while retaining an honest
  Fixed or Mobile result when the original assignment remains inconclusive.
- Caches live results for 24 hours by default to reduce duplicate lookup fees.
- Processes Excel, CSV, TXT, PDF, and pasted lists and exports detailed Excel
  worksheets and CSV files.

IMPORTANT ACCURACY FACT
No phone lookup tool can honestly guarantee 100% accuracy or prove that every
number is currently reachable. Numbers can be ported, reassigned, disconnected,
or changed after a provider's last data refresh. For the strongest result, use
live mode and check the Lookup Source, Data Freshness, and Fallback fields.

Offline mode validates number structure and numbering-plan assignments only.
It does not prove that a number is active. The NANPA database records original
NPA-NXX assignments and cannot reliably identify a number after porting.
Time zone has no effect on line type; the v6.2 improvement is nationwide
carrier-family coverage rather than a time-zone guess.

QUICK START - WINDOWS
1. Extract the complete ZIP.
2. For accurate current carrier data, double-click configure_live_lookup.bat.
3. Add either a Telnyx API key or Twilio credentials, then save and close.
4. Double-click start_portal.bat.
5. The first launch installs the required Python packages and opens the portal.
6. Keep the black command window open while using the portal.

QUICK START - macOS / LINUX
1. Open Terminal in this folder.
2. Run: chmod +x start_portal.sh configure_live_lookup.sh
3. Run: ./configure_live_lookup.sh
4. Add either a Telnyx API key or Twilio credentials and save.
5. Run: ./start_portal.sh

LIVE PROVIDER SETUP
The .env file is private and excluded by .gitignore. The local browser never
receives your provider secret.

Telnyx (recommended for detailed U.S./Canada portability fields):
  TELNYX_API_KEY=your_key_here

Twilio API key authentication (recommended Twilio option):
  TWILIO_API_KEY=SK...
  TWILIO_API_SECRET=your_secret_here

Twilio Account SID / Auth Token authentication also works:
  TWILIO_ACCOUNT_SID=AC...
  TWILIO_AUTH_TOKEN=your_auth_token_here

Provider lookups may incur per-number charges. Check your provider's current
pricing before running a large batch. The portal asks for confirmation before
submitting live batches.

LOOKUP MODES
- Best Available: live provider when configured, offline fallback on errors.
- Require Live: requests live data and clearly flags a missing/failed provider.
- Offline Only: never sends phone numbers to a lookup provider and has no
  per-number provider fee.

CACHE AND FRESHNESS
LOOKUP_CACHE_HOURS=24 reuses a recent live result for repeated numbers. Set it
to 0 for a fresh paid request every time. The single-inspection screen also has
a Force Fresh Live Lookup option.

REFRESH THE OFFLINE DATABASE
Run update_offline_database.bat on Windows or update_offline_database.sh on
macOS/Linux. This downloads NANPA's public Central Office Code Assignment report
and rebuilds nanpa_prefixes.sqlite3. Your phone-number lists are not uploaded by
this database-update process.

DATA SOURCES
- NANPA Central Office Code Assignment Records:
  https://www.nanpa.com/reports/co-code-reports/cocodes_assign
- Telnyx Number Lookup API:
  https://developers.telnyx.com/api-reference/number-lookup/lookup-phone-number-data
- Twilio Lookup v2 Line Type Intelligence:
  https://www.twilio.com/docs/lookup/v2-api/line-type-intelligence

LOCAL API
Interactive documentation: http://127.0.0.1:8000/docs

Single lookup example:
  GET /validate?phone=%2B16077341118&lookup_mode=auto

Bulk request fields:
  numbers, default_region, lookup_mode (auto/live/offline), refresh_live

RUN TESTS
After start_portal has created .venv:
  Windows: .venv\Scripts\python.exe -m unittest discover -s tests -v
  macOS/Linux: .venv/bin/python -m unittest discover -s tests -v
