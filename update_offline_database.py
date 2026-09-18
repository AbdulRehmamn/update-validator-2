"""Build the local NANPA prefix-estimation database.

The source is NANPA's public Central Office Code Assignment report. It contains
prefix assignments and carrier names, not live porting data. Classification is
therefore an estimate of the original prefix network type.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import tempfile
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "nanpa_prefixes.sqlite3"
RULES_VERSION = "6.2 nationwide carrier-family rules"
NANPA_DOWNLOAD_URL = (
    "https://reports.nanpa.com/public/"
    "CoCodeAssignment_Utilized_AllStates_Public.zip"
)

MOBILE_MARKERS = (
    "WIRELESS", "CELLULAR", "MOBILE", "MOBILITY", " PCS", "METROPCS",
    "CINGULAR", "SPRINT SPECTRUM", "SPRINTCOM", "T-MOBILE", "POWERTEL",
    "OMNIPOINT", "AERIAL COMMUNICATIONS", "AIRTOUCH", "CRICKET", "NEXTEL",
    "CELLCO PARTNERSHIP", "UNITED STATES CELLULAR", "US CELLULAR",
    "U.S. CELLULAR", "DISH WIRELESS", "CLEARWIRE", "RURAL CELLULAR",
    "ALLTEL", "EVOLVE CELLULAR", "AMERICAN CELLULAR", "MCCAW",
    # Nationwide and regional mobile brands whose legal assignment names do
    # not necessarily contain the words wireless, cellular, or mobile.
    "BOOST SUBSCRIBERCO", "VOICESTREAM GSM", "STPCS JOINT VENTURE",
    "SOUTHERN COMMUNICATIONS SERVICES", "NEW CELL DBA CELLCOM",
)

VOIP_MARKERS = (
    "IP PHONE", "VOIP", "VOICE OVER IP", "BANDWIDTH.COM", "ONVOY",
    "INTELIQUENT", "LEVEL 3 COMMUNICATIONS", "TWILIO", "TELNYX", "SINCH",
    "PEERLESS NETWORK", "NEUTRAL TANDEM", "BROADVOX", "VONAGE", "COMMIO",
    "PLIVO", "YMAX", "MAGICJACK", "COMCAST", "CHARTER FIBERLINK",
    "TIME WARNER CABLE", "CABLEVISION LIGHTPATH", "COX COMMUNICATIONS",
    "SPECTRUM ADVANCED SERVICES", "GOOGLE VOICE",
    # Cable-phone, hosted-voice, and IP-numbering carrier families found in
    # NANPA records throughout the United States and its territories.
    "IP ENABLED SERVICES", "FRACTEL", "TON80 COMMUNICATIONS", "NUSO",
    "IP HORIZON", "CSC VOICE", "SKYETEL", "MCC TELEPHONY",
    "MEDIACOM TELEPHONY", "FIVE9", "CALLWORKS", "PEERING HUB",
    "OPENTACT", "INVOXIO", "WIDE VOICE", "INTRADO COMMUNICATIONS",
    "THE VOICE APPLICATION NETWORK", "HD CARRIER", "LEVEL 3 TELECOM",
    "LEVEL 3 OF TELECOM", "EON TELECOM", "AHOI", "WAVENATION",
    "EXIANT", "NETCARRIER TELECOM", "AIRESPRING", "COMPUTER TELEPHONY",
    "ZCALL.COM", "DIGITALIPVOICE", "CABLE ONE VOIP",
    "RCN TELECOM SERVICES", "WOW! INTERNET, CABLE AND PHONE",
    "GRANDE COMMUNICATIONS NETWORKS",
)

PAGER_MARKERS = (
    "PAGING", "BEEPERS", "BEEPER SERVICE", "PAGER", "SPOK",
    "AMERICAN MESSAGING", "AQUIS COMMUNICATIONS", "USA MOBILITY",
    "ARCH WIRELESS",
)

SATELLITE_MARKERS = ("GLOBALSTAR", "IRIDIUM")

LANDLINE_MARKERS = (
    "TELEPHONE", "TEL & TEL", "TELECOMM", "FRONTIER", "CENTURYLINK",
    "QWEST", "WINDSTREAM", "LUMEN", "SOUTHERN BELL", "PACIFIC BELL",
    "SOUTHWESTERN BELL", "BELL ATLANTIC", "BELL OF PENNSYLVANIA",
    "AMERITECH", "SBC", "CONSOLIDATED COMM", "UNITED TELEPHONE",
    "CENTRAL TELEPHONE", "CINCINNATI BELL", "BELLSOUTH", "ILEC",
    # Verizon's regional operating companies (for example Verizon New York)
    # are incumbent fixed-line carriers. The MOBILE rules are checked first,
    # so CELLCO / VERIZON WIRELESS assignments remain classified as mobile.
    "VERIZON",
    # Incumbent and competitive fixed-network families. These legal carrier
    # names occur across all continental time zones, Alaska, Hawaii, and U.S.
    # territories. More specific VoIP/mobile rules above always win.
    "PAETEC", "MCIMETRO", "BELL TEL", "BELL INC", "NEVADA BELL",
    "TELEPACIFIC", "TPX COMMUNICATIONS", "AT&T ENTERPRISES",
    "AT&T - LOCAL", "AT&T CORP", "AT&T ALASKA",
    "TELEPORT COMMUNICATIONS AMERICA", "ZIPLY FIBER",
    "ELECTRIC LIGHTWAVE", "ALLSTREAM", "BRIGHTSPEED", "TC SYSTEMS",
    "HAWAIIAN TELCOM", "PUERTO RICO TEL", "MCI WORLDCOM",
    "TELNET WORLDWIDE", "CORE COMMUNICATIONS", "MATRIX TELECOM",
    "CORETEL", "FIDIUM", "TDS METROCOM", "ACS OF ANCHORAGE",
    "ACS OF THE NORTHLAND", "TELEGUAM", "FIRST COMMUNICATIONS",
    "GREAT PLAINS COMMUNICATIONS", "STRATUS NETWORKS",
    "TELEPAK NETWORKS", "PIONEER TEL", "UNITED UTILITIES",
    "GRAND RIVER MUTUAL TEL", "CONSOLIDATED TEL", "FIBER NETWORK",
    # Common rural/local exchange abbreviations in NANPA legal names.
    " TEL. CO", " TEL CO", " TEL. ASSN", " TEL ASSN", " TEL. COOP",
    " TEL COOP", " TEL. MEMBERSHIP", " TEL MEMBERSHIP", "RURAL TEL",
    " TELCO", " TELCOM",
)


def estimate_company_type(company: str) -> tuple[str, str]:
    """Return (estimated type, rule confidence) from an assigned company name."""
    name = " ".join(company.upper().split())
    if any(marker in name for marker in PAGER_MARKERS):
        return "Pager", "High"
    if any(marker in name for marker in SATELLITE_MARKERS):
        return "Satellite", "High"
    if any(marker in name for marker in VOIP_MARKERS):
        return "VoIP", "Medium"
    if any(marker in name for marker in MOBILE_MARKERS):
        return "Cell Phone", "Medium"
    if any(marker in name for marker in LANDLINE_MARKERS):
        return "Landline", "Medium"
    return "Fixed or Mobile", "Low"


def download_source(destination: Path) -> None:
    request = urllib.request.Request(
        NANPA_DOWNLOAD_URL,
        headers={"User-Agent": "NumVerify-Pro/6.2"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)


def build_database(source_zip: Path, database_path: Path = DATABASE_PATH) -> Counter:
    temporary_database = database_path.with_name(database_path.name + ".building")
    if temporary_database.exists():
        temporary_database.unlink()

    counts: Counter = Counter()
    connection = sqlite3.connect(temporary_database)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE prefixes (
                prefix TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                ocn TEXT NOT NULL,
                company TEXT NOT NULL,
                rate_center TEXT NOT NULL,
                pooled INTEGER NOT NULL,
                estimated_type TEXT NOT NULL,
                confidence TEXT NOT NULL
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

        with zipfile.ZipFile(source_zip) as archive:
            text_members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if not text_members:
                raise RuntimeError("The NANPA archive does not contain a text report.")

            with archive.open(text_members[0]) as raw_file:
                text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", errors="replace", newline="")
                reader = csv.reader(text_file, delimiter="\t")
                header = next(reader)
                update_label = header[-1].strip() if header else ""
                batch: list[tuple] = []

                for row in reader:
                    if len(row) < 11:
                        continue
                    values = [value.strip().strip('"') for value in row]
                    state, prefix, ocn, company, rate_center = values[:5]
                    use_status = values[6].strip()
                    pooled = values[9].strip().upper() == "Y"
                    in_service = values[10].strip().upper() == "YES"
                    if use_status != "AS" or not in_service or len(prefix) != 7:
                        continue

                    estimated_type, confidence = estimate_company_type(company)
                    # Pooling permits a thousands block to move to another carrier.
                    if pooled and confidence == "High":
                        confidence = "Medium"
                    elif pooled and confidence == "Medium":
                        confidence = "Low"

                    batch.append(
                        (
                            prefix, state, ocn, company, rate_center,
                            int(pooled), estimated_type, confidence,
                        )
                    )
                    counts[estimated_type] += 1

                    if len(batch) >= 5000:
                        connection.executemany(
                            "INSERT OR REPLACE INTO prefixes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            batch,
                        )
                        batch.clear()

                if batch:
                    connection.executemany(
                        "INSERT OR REPLACE INTO prefixes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        batch,
                    )

                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    [
                        ("source", "NANPA Central Office Code Assignment Records"),
                        ("source_url", NANPA_DOWNLOAD_URL),
                        ("file_updated", update_label),
                        ("method", "Carrier-name prefix estimate; not live porting data"),
                        ("rules_version", RULES_VERSION),
                    ],
                )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()

    os.replace(temporary_database, database_path)
    return counts


def reclassify_database(database_path: Path = DATABASE_PATH) -> Counter:
    """Apply the latest carrier-family rules to an existing prefix database."""
    if not database_path.exists():
        raise FileNotFoundError(f"Database not found: {database_path}")

    counts: Counter = Counter()
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT prefix, company, pooled FROM prefixes"
        ).fetchall()
        updates: list[tuple[str, str, str]] = []
        for prefix, company, pooled in rows:
            estimated_type, confidence = estimate_company_type(company)
            # Number pooling can place a thousands block with another carrier,
            # so reduce confidence even when the carrier family is known.
            if pooled and confidence == "High":
                confidence = "Medium"
            elif pooled and confidence == "Medium":
                confidence = "Low"
            updates.append((estimated_type, confidence, prefix))
            counts[estimated_type] += 1

        connection.executemany(
            "UPDATE prefixes SET estimated_type = ?, confidence = ? WHERE prefix = ?",
            updates,
        )
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('rules_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (RULES_VERSION,),
        )
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('method', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("Nationwide carrier-family prefix estimate; not live porting data",),
        )
        connection.commit()
    finally:
        connection.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Update NumVerify's offline NANPA database")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source-zip", type=Path,
        help="Use an existing NANPA ZIP instead of downloading",
    )
    source_group.add_argument(
        "--reclassify-only", action="store_true",
        help="Apply current carrier rules to the existing database without downloading",
    )
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    args = parser.parse_args()

    if args.reclassify_only:
        counts = reclassify_database(args.database.resolve())
    elif args.source_zip:
        source_zip = args.source_zip.resolve()
        counts = build_database(source_zip, args.database.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="numverify-nanpa-") as temporary_dir:
            source_zip = Path(temporary_dir) / "nanpa-utilized.zip"
            print("Downloading official NANPA prefix assignments...")
            download_source(source_zip)
            counts = build_database(source_zip, args.database.resolve())

    print(f"Database ready: {args.database.resolve()}")
    print("Prefix classifications:")
    for label, count in counts.most_common():
        print(f"  {label}: {count:,}")
    print("Reminder: classifications are estimates and cannot detect number porting.")


if __name__ == "__main__":
    main()
