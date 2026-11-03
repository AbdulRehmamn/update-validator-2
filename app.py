"""NumVerify Pro hybrid phone-number intelligence server.

The application always performs local structural validation first. When a
Telnyx or Twilio credential is configured it then uses current carrier data,
including portability data when Telnyx supplies it. If a live lookup cannot be
completed, the response falls back to clearly labelled offline metadata.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
import phonenumbers
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from phonenumbers import PhoneNumberType, carrier, geocoder
from phonenumbers import timezone as phone_timezone
from pydantic import AliasChoices, BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "nanpa_prefixes.sqlite3"
CACHE_DATABASE_PATH = BASE_DIR / "lookup_cache.sqlite3"
DEFAULT_REGION = "US"

load_dotenv(BASE_DIR / ".env")


def _bounded_env_number(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


LOOKUP_PROVIDER = os.getenv("LOOKUP_PROVIDER", "auto").strip().lower()
if LOOKUP_PROVIDER not in {"auto", "telnyx", "twilio", "offline"}:
    LOOKUP_PROVIDER = "auto"

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "").strip()
TWILIO_USERNAME = (
    os.getenv("TWILIO_API_KEY", "").strip()
    or os.getenv("TWILIO_ACCOUNT_SID", "").strip()
)
TWILIO_PASSWORD = (
    os.getenv("TWILIO_API_SECRET", "").strip()
    or os.getenv("TWILIO_AUTH_TOKEN", "").strip()
)
LOOKUP_CACHE_HOURS = _bounded_env_number("LOOKUP_CACHE_HOURS", 24, 0, 720)
LOOKUP_CONCURRENCY = int(_bounded_env_number("LOOKUP_CONCURRENCY", 8, 1, 30))
LOOKUP_TIMEOUT_SECONDS = _bounded_env_number(
    "LOOKUP_TIMEOUT_SECONDS", 20, 5, 60
)

def _parse_api_keys(raw: str) -> tuple[str, ...]:
    return tuple(key.strip() for key in re.split(r"[,\s]+", raw or "") if key.strip())


# Keys that external scripts must send in the X-API-Key header (or as
# "Authorization: Bearer <key>"). When empty, the API stays open as before.
API_KEYS = _parse_api_keys(os.getenv("API_KEYS", ""))

# The browser portal served at "/" receives this per-process token instead of a
# real API key, so the portal keeps working when API_KEYS is enabled.
# Set PORTAL_TOKEN in the environment when running several worker processes
# so every worker accepts the same portal token.
PORTAL_TOKEN = os.getenv("PORTAL_TOKEN", "").strip() or secrets.token_urlsafe(32)

# Requests may contain at most this many numbers (same limit as the
# standalone phone-validator-api).
MAX_BATCH_NUMBERS = 10_000

# U.S. regions covered by the bundled NANPA Central Office Code records.
# Canada and the Caribbean NANP countries are administered separately.
NANPA_DATABASE_REGIONS = {"US", "PR", "VI", "GU", "AS", "MP"}

PROVIDER_LABELS = {
    "telnyx": "Telnyx Number Lookup",
    "twilio": "Twilio Line Type Intelligence",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    _initialize_cache()
    yield


app = FastAPI(
    title="NumVerify Pro Live Intelligence",
    description=(
        "Local structural validation with optional live carrier, line-type, "
        "and portability intelligence from Telnyx or Twilio."
    ),
    version="6.4.0",
    lifespan=lifespan,
)


class BulkValidationRequest(BaseModel):
    numbers: list[str] = Field(default_factory=list)
    default_region: str = Field(
        default=DEFAULT_REGION,
        min_length=2,
        max_length=2,
        validation_alias=AliasChoices("default_region", "region"),
    )
    lookup_mode: Literal["auto", "live", "offline"] = "auto"
    refresh_live: bool = False


class SingleValidationRequest(BaseModel):
    phone_number: str = Field(
        ...,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("phone_number", "phone", "number"),
        description="Phone number in any common format, e.g. (415) 555-2671 or +14155552671",
    )
    default_region: str = Field(
        default=DEFAULT_REGION,
        min_length=2,
        max_length=2,
        validation_alias=AliasChoices("default_region", "region"),
    )
    lookup_mode: Literal["auto", "live", "offline"] = "auto"
    refresh_live: bool = False


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
) -> None:
    """Enforce API keys on validation endpoints when API_KEYS is configured."""
    if not API_KEYS:
        return
    if x_portal_token and hmac.compare_digest(x_portal_token, PORTAL_TOKEN):
        return
    supplied = (x_api_key or "").strip()
    if not supplied and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()
    if not supplied:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Send it in the X-API-Key header.",
        )
    if not any(hmac.compare_digest(supplied, key) for key in API_KEYS):
        raise HTTPException(status_code=401, detail="Invalid API key.")


class LiveLookupError(RuntimeError):
    """A safe, user-displayable live-provider error."""


def utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(
        timestamp if timestamp is not None else time.time(), timezone.utc
    ).isoformat(timespec="seconds")


def configured_providers() -> list[str]:
    available: list[str] = []
    if TELNYX_API_KEY:
        available.append("telnyx")
    if TWILIO_USERNAME and TWILIO_PASSWORD:
        available.append("twilio")
    return available


def provider_chain(lookup_mode: str = "auto") -> list[str]:
    if lookup_mode == "offline" or LOOKUP_PROVIDER == "offline":
        return []
    available = configured_providers()
    if LOOKUP_PROVIDER in {"telnyx", "twilio"}:
        return [LOOKUP_PROVIDER] if LOOKUP_PROVIDER in available else []
    # Telnyx is preferred for NANP records because its response can include
    # porting date, LRN, OCN, SPID, city, and state. Twilio is the fallback.
    return [provider for provider in ("telnyx", "twilio") if provider in available]


def provider_display_name(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider.title())


def map_libphonenumber_type(number: phonenumbers.PhoneNumber) -> tuple[str, str]:
    number_type = phonenumbers.number_type(number)
    mapping = {
        PhoneNumberType.MOBILE: ("Cell Phone", "Mobile"),
        PhoneNumberType.FIXED_LINE: ("Landline", "Fixed Line"),
        PhoneNumberType.FIXED_LINE_OR_MOBILE: (
            "Fixed or Mobile",
            "Ambiguous Fixed/Mobile",
        ),
        PhoneNumberType.VOIP: ("VoIP", "VoIP"),
        PhoneNumberType.TOLL_FREE: ("Toll-Free", "Toll-Free"),
        PhoneNumberType.PREMIUM_RATE: ("Premium Rate", "Premium Rate"),
        PhoneNumberType.SHARED_COST: ("Shared Cost", "Shared Cost"),
        PhoneNumberType.PERSONAL_NUMBER: ("Personal Number", "Personal Number"),
        PhoneNumberType.PAGER: ("Pager", "Pager"),
        PhoneNumberType.UAN: ("Universal Access", "Universal Access Number"),
        PhoneNumberType.VOICEMAIL: ("Voicemail", "Voicemail"),
    }
    return mapping.get(number_type, ("Unknown", "Unknown"))


def normalize_provider_type(raw_type: Any) -> tuple[str, str]:
    raw = str(raw_type or "").strip()
    key = re.sub(r"[^a-z0-9]+", "", raw.lower())
    mapping = {
        "mobile": ("Cell Phone", "Mobile"),
        "cell": ("Cell Phone", "Mobile"),
        "cellular": ("Cell Phone", "Mobile"),
        "wireless": ("Cell Phone", "Mobile"),
        "landline": ("Landline", "Fixed Line"),
        "fixed": ("Landline", "Fixed Line"),
        "fixedline": ("Landline", "Fixed Line"),
        "wireline": ("Landline", "Fixed Line"),
        "voip": ("VoIP", "VoIP"),
        "fixedvoip": ("VoIP", "Fixed VoIP"),
        "nonfixedvoip": ("VoIP", "Non-Fixed VoIP"),
        "tollfree": ("Toll-Free", "Toll-Free"),
        "premium": ("Premium Rate", "Premium Rate"),
        "premiumrate": ("Premium Rate", "Premium Rate"),
        "sharedcost": ("Shared Cost", "Shared Cost"),
        "uan": ("Universal Access", "Universal Access Number"),
        "universalaccess": ("Universal Access", "Universal Access Number"),
        "voicemail": ("Voicemail", "Voicemail"),
        "pager": ("Pager", "Pager"),
        "personal": ("Personal Number", "Personal Number"),
        "personalnumber": ("Personal Number", "Personal Number"),
        "satellite": ("Satellite", "Satellite"),
        "unknown": ("Unknown", "Unknown"),
    }
    return mapping.get(key, ("Unknown", raw or "Unknown"))


def lookup_nanpa_prefix(national_digits: str) -> dict[str, Any] | None:
    """Look up the original NPA-NXX assignment in the local database."""
    if not DATABASE_PATH.exists() or len(national_digits) != 10:
        return None
    prefix = f"{national_digits[:3]}-{national_digits[3:6]}"
    try:
        with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT prefix, state, ocn, company, rate_center, pooled,
                       estimated_type, confidence
                FROM prefixes WHERE prefix = ?
                """,
                (prefix,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


@lru_cache(maxsize=1024)
def nanpa_area_code_known(npa: str) -> bool:
    """True when the bundled database holds at least one prefix for this NPA.

    The unassigned-exchange check is only applied to area codes the database
    actually covers, so a brand-new area code missing from an older snapshot
    is never wrongly reported as unassigned.
    """
    if not DATABASE_PATH.exists() or len(npa) != 3:
        return False
    try:
        with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT 1 FROM prefixes WHERE prefix >= ? AND prefix < ? LIMIT 1",
                (f"{npa}-", f"{npa}."),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def nanp_reserved_status(national_digits: str) -> tuple[str, str] | None:
    """Detect NANP numbers that are reserved and can never reach a subscriber.

    Returns (number_status, explanation) or None.
    """
    if len(national_digits) != 10:
        return None
    nxx, line = national_digits[3:6], national_digits[6:]
    if nxx != "555":
        return None
    if "0100" <= line <= "0199":
        return (
            "fictional",
            "555-0100 through 555-0199 are reserved by NANPA for fictional use "
            "(films, TV, documentation). This number cannot be assigned to anyone.",
        )
    if line == "1212":
        return (
            "special_service",
            "555-1212 is the directory-assistance service code, not a subscriber line.",
        )
    return (
        "unassigned_exchange",
        "The 555 exchange is not assigned to telephone carriers for ordinary "
        "subscriber lines, so this number is not in service.",
    )


def invalid_result(raw: str, message: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "number": raw,
        "e164": "",
        "national_format": raw,
        "country_code": "",
        "country": "",
        "valid": False,
        "possible": False,
        "lineType": "Unknown",
        "lineSubtype": "Unknown",
        "carrier": "Invalid format",
        "normalizedCarrier": "",
        "originalCarrier": "",
        "location": "N/A",
        "city": "",
        "state": "",
        "ported": None,
        "portedStatus": "Unknown",
        "portedDate": "",
        "lrn": "",
        "ocn": "",
        "spid": "",
        "altSpid": "",
        "mobileCountryCode": "",
        "mobileNetworkCode": "",
        "prefix": "",
        "rateCenter": "",
        "pooledPrefix": None,
        "lookupProvider": "offline",
        "lookupSource": "Offline structural validation",
        "liveLookup": False,
        "cacheHit": False,
        "lookupTimestamp": "",
        "dataFreshness": "Offline metadata",
        "classificationConfidence": "None",
        "estimated": False,
        "fallbackUsed": False,
        "classificationNote": message,
        "lookupError": "",
        "numberStatus": "invalid_format",
        "assignedExchange": None,
        "error": message,
    }


def validate_number_offline(
    phone: str,
    default_region: str = DEFAULT_REGION,
) -> dict[str, Any]:
    raw = str(phone or "").strip()
    region = (default_region or DEFAULT_REGION).strip().upper()
    try:
        parsed = phonenumbers.parse(raw, None if raw.startswith("+") else region)
    except phonenumbers.NumberParseException as exc:
        return invalid_result(raw, str(exc))

    possible = phonenumbers.is_possible_number(parsed)
    valid = phonenumbers.is_valid_number(parsed)
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    national = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.NATIONAL
    )
    country = phonenumbers.region_code_for_number(parsed) or ""
    location = geocoder.description_for_number(parsed, "en") or country or "Unknown"
    offline_carrier = carrier.name_for_number(parsed, "en") or "Not available offline"
    national_digits = str(parsed.national_number)

    line_type, line_subtype = map_libphonenumber_type(parsed)
    special_types = {
        "Toll-Free",
        "Premium Rate",
        "Shared Cost",
        "Personal Number",
        "Pager",
        "Universal Access",
        "Voicemail",
    }
    confidence = (
        "High"
        if line_type in special_types
        else "Low"
        if line_type in {"Fixed or Mobile", "Unknown"}
        else "Medium"
    )
    source = "libphonenumber numbering metadata"
    estimated = line_type in {"Fixed or Mobile", "Unknown"}
    note = (
        "Offline numbering-plan classification. It validates structure but "
        "does not prove that the number is active or reveal a recent port."
    )

    prefix_record: dict[str, Any] | None = None
    if valid and parsed.country_code == 1:
        prefix_record = lookup_nanpa_prefix(national_digits)

    # Ordinary NANP numbers are intentionally marked ambiguous by
    # libphonenumber. The bundled prefix record can estimate the original
    # assignment, but it is never presented as current live carrier data.
    if valid and line_type == "Fixed or Mobile" and prefix_record:
        offline_carrier = prefix_record["company"] or offline_carrier
        estimated_type = prefix_record["estimated_type"]
        confidence = prefix_record["confidence"]
        if estimated_type != "Fixed or Mobile":
            line_type = estimated_type
            line_subtype = f"Original-prefix estimate: {estimated_type}"
        source = "NANPA original-prefix estimate"
        estimated = True
        pooled_note = (
            " This prefix is pooled, so its thousands block may use another carrier."
            if prefix_record["pooled"]
            else ""
        )
        note = (
            f"Estimated from original prefix {prefix_record['prefix']} assigned to "
            f"{prefix_record['company']}.{pooled_note} Ported numbers may differ."
        )

    number_status = "valid" if valid else ("invalid_number" if possible else "invalid_format")
    assigned_exchange: bool | None = True if prefix_record else None

    geographic = line_type in {"Fixed or Mobile", "Landline", "Cell Phone", "VoIP"}
    if valid and parsed.country_code == 1 and geographic:
        # Toll-free and other non-geographic NANP codes are administered
        # separately and may legitimately use 555, so they are excluded.
        reserved = nanp_reserved_status(national_digits)
        if reserved:
            number_status, note = reserved
            assigned_exchange = False
            prefix_record = None
            confidence = "High"
            estimated = False
            source = "NANPA numbering rules"
            if number_status == "special_service":
                line_type, line_subtype = "Unknown", "Directory Assistance"
                offline_carrier = "Directory assistance service code"
            else:
                valid = False
                line_type = "Unknown"
                line_subtype = (
                    "Fictional (reserved)"
                    if number_status == "fictional"
                    else "Unassigned exchange"
                )
                offline_carrier = "Not assigned to any carrier"
        elif (
            not prefix_record
            and country in NANPA_DATABASE_REGIONS
            and nanpa_area_code_known(national_digits[:3])
        ):
            # The NPA is covered by the NANPA records, but this NXX has never
            # been assigned to a carrier, so no subscriber can hold the number.
            npa_nxx = f"{national_digits[:3]}-{national_digits[3:6]}"
            db_date = database_metadata().get("file_updated", "the bundled file")
            valid = False
            number_status = "unassigned_exchange"
            assigned_exchange = False
            line_type, line_subtype = "Unknown", "Unassigned exchange"
            offline_carrier = "Not assigned to any carrier"
            confidence = "High"
            estimated = False
            source = "NANPA Central Office Code assignments"
            note = (
                f"Exchange {npa_nxx} is not assigned to any carrier in the NANPA "
                f"Central Office Code records ({db_date}), so this number is not in "
                "service. Run the offline database update if the file is old."
            )

    return {
        "raw": raw,
        "number": national_digits,
        "e164": e164,
        "national_format": national,
        "country_code": f"+{parsed.country_code}",
        "country": country,
        "valid": valid,
        "possible": possible,
        "lineType": line_type,
        "lineSubtype": line_subtype,
        "carrier": offline_carrier,
        "normalizedCarrier": "",
        "originalCarrier": offline_carrier if prefix_record else "",
        "location": location,
        "city": prefix_record["rate_center"] if prefix_record else "",
        "state": prefix_record["state"] if prefix_record else "",
        "ported": None,
        "portedStatus": "Unknown — live lookup required",
        "portedDate": "",
        "lrn": "",
        "ocn": prefix_record["ocn"] if prefix_record else "",
        "spid": "",
        "altSpid": "",
        "mobileCountryCode": "",
        "mobileNetworkCode": "",
        "prefix": prefix_record["prefix"] if prefix_record else "",
        "rateCenter": prefix_record["rate_center"] if prefix_record else "",
        "pooledPrefix": bool(prefix_record["pooled"]) if prefix_record else None,
        "lookupProvider": "offline",
        "lookupSource": source,
        "liveLookup": False,
        "cacheHit": False,
        "lookupTimestamp": "",
        "dataFreshness": "Offline metadata",
        "classificationConfidence": confidence,
        "estimated": estimated,
        "fallbackUsed": False,
        "classificationNote": note,
        "lookupError": "",
        "numberStatus": number_status,
        "assignedExchange": assigned_exchange,
        "error": None,
    }


_CACHE_LOCK = threading.Lock()


def _initialize_cache() -> None:
    try:
        with _CACHE_LOCK, sqlite3.connect(CACHE_DATABASE_PATH) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS live_lookup_cache (
                    e164 TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    looked_up_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (e164, provider)
                )
                """
            )
            connection.execute(
                "DELETE FROM live_lookup_cache WHERE expires_at <= ?", (time.time(),)
            )
    except sqlite3.Error:
        # Caching is an optimization; lookup must still work if the directory is
        # read-only or the cache database is unavailable.
        pass


def cache_get(e164: str, provider: str) -> tuple[dict[str, Any], float] | None:
    if LOOKUP_CACHE_HOURS <= 0:
        return None
    try:
        with _CACHE_LOCK, sqlite3.connect(CACHE_DATABASE_PATH) as connection:
            row = connection.execute(
                """
                SELECT payload, looked_up_at
                FROM live_lookup_cache
                WHERE e164 = ? AND provider = ? AND expires_at > ?
                """,
                (e164, provider, time.time()),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0]), float(row[1])
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return None


def cache_put(e164: str, provider: str, payload: dict[str, Any]) -> float:
    looked_up_at = time.time()
    expires_at = looked_up_at + (LOOKUP_CACHE_HOURS * 3600)
    try:
        with _CACHE_LOCK, sqlite3.connect(CACHE_DATABASE_PATH) as connection:
            connection.execute(
                """
                INSERT INTO live_lookup_cache
                    (e164, provider, payload, looked_up_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(e164, provider) DO UPDATE SET
                    payload = excluded.payload,
                    looked_up_at = excluded.looked_up_at,
                    expires_at = excluded.expires_at
                """,
                (e164, provider, json.dumps(payload), looked_up_at, expires_at),
            )
    except sqlite3.Error:
        pass
    return looked_up_at


def _provider_error(response: httpx.Response, provider: str) -> LiveLookupError:
    detail = ""
    try:
        body = response.json()
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            detail = str(first.get("detail") or first.get("title") or "")
        elif isinstance(body, dict):
            detail = str(body.get("message") or body.get("detail") or "")
    except (ValueError, TypeError):
        detail = ""
    safe_detail = f": {detail[:180]}" if detail else ""
    return LiveLookupError(
        f"{provider_display_name(provider)} returned HTTP {response.status_code}{safe_detail}"
    )


def parse_telnyx_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    carrier_data = data.get("carrier") or {}
    portability = data.get("portability") or {}
    raw_type = portability.get("line_type") or carrier_data.get("type")
    line_type, line_subtype = normalize_provider_type(raw_type)

    ported_raw = str(portability.get("ported_status") or "").strip().upper()
    if ported_raw in {"Y", "YES", "TRUE"}:
        ported: bool | None = True
        ported_status = "Ported"
    elif ported_raw in {"N", "NO", "FALSE"}:
        ported = False
        ported_status = "Not ported"
    else:
        ported = None
        ported_status = "Unknown"

    current_carrier = (
        portability.get("spid_carrier_name")
        or portability.get("altspid_carrier_name")
        or carrier_data.get("name")
        or carrier_data.get("normalized_carrier")
        or "Not returned"
    )
    city = str(portability.get("city") or "")
    state = str(portability.get("state") or "")
    live_location = ", ".join(value for value in (city, state) if value)
    provider_error_code = carrier_data.get("error_code")

    return {
        "providerValid": True,
        "lineType": line_type,
        "lineSubtype": line_subtype,
        "carrier": str(current_carrier),
        "normalizedCarrier": str(carrier_data.get("normalized_carrier") or ""),
        "originalCarrier": str(carrier_data.get("name") or ""),
        "location": live_location,
        "city": city,
        "state": state,
        "ported": ported,
        "portedStatus": ported_status,
        "portedDate": str(portability.get("ported_date") or ""),
        "lrn": str(portability.get("lrn") or ""),
        "ocn": str(portability.get("ocn") or ""),
        "spid": str(portability.get("spid") or ""),
        "altSpid": str(portability.get("altspid") or ""),
        "mobileCountryCode": str(carrier_data.get("mobile_country_code") or ""),
        "mobileNetworkCode": str(carrier_data.get("mobile_network_code") or ""),
        "providerErrorCode": str(provider_error_code or ""),
        "lookupSource": "Telnyx live carrier and portability data",
        "classificationNote": (
            "Live carrier lookup completed. Portability fields are shown exactly "
            "as returned by Telnyx."
        ),
    }


def parse_twilio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    intelligence = payload.get("line_type_intelligence") or {}
    raw_type = intelligence.get("type")
    line_type, line_subtype = normalize_provider_type(raw_type)
    provider_valid = payload.get("valid")
    if not isinstance(provider_valid, bool):
        provider_valid = True
    validation_errors = payload.get("validation_errors") or []
    error_code = intelligence.get("error_code")

    return {
        "providerValid": provider_valid,
        "lineType": line_type,
        "lineSubtype": line_subtype,
        "carrier": str(intelligence.get("carrier_name") or "Not returned"),
        "normalizedCarrier": "",
        "originalCarrier": "",
        "location": "",
        "city": "",
        "state": "",
        "ported": None,
        "portedStatus": "Not supplied by Twilio Line Type Intelligence",
        "portedDate": "",
        "lrn": "",
        "ocn": "",
        "spid": "",
        "altSpid": "",
        "mobileCountryCode": str(intelligence.get("mobile_country_code") or ""),
        "mobileNetworkCode": str(intelligence.get("mobile_network_code") or ""),
        "providerErrorCode": str(error_code or ""),
        "validationErrors": validation_errors,
        "lookupSource": "Twilio live Line Type Intelligence",
        "classificationNote": (
            "Live line-type and carrier lookup completed with Twilio. "
            "This package does not return portability details."
        ),
    }


async def lookup_telnyx(
    e164: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    url = f"https://api.telnyx.com/v2/number_lookup/{quote(e164, safe='')}"
    try:
        response = await client.get(
            url,
            params={"type": "carrier"},
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
        )
    except httpx.HTTPError as exc:
        raise LiveLookupError(f"Telnyx network error: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise _provider_error(response, "telnyx")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveLookupError("Telnyx returned an unreadable response") from exc
    return parse_telnyx_payload(payload)


async def lookup_twilio(
    e164: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    url = f"https://lookups.twilio.com/v2/PhoneNumbers/{quote(e164, safe='')}"
    try:
        response = await client.get(
            url,
            params={"Fields": "line_type_intelligence"},
            auth=httpx.BasicAuth(TWILIO_USERNAME, TWILIO_PASSWORD),
        )
    except httpx.HTTPError as exc:
        raise LiveLookupError(f"Twilio network error: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise _provider_error(response, "twilio")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveLookupError("Twilio returned an unreadable response") from exc
    return parse_twilio_payload(payload)


async def query_provider(
    provider: str,
    e164: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    if provider == "telnyx":
        return await lookup_telnyx(e164, client)
    if provider == "twilio":
        return await lookup_twilio(e164, client)
    raise LiveLookupError(f"Unsupported lookup provider: {provider}")


def merge_live_result(
    offline: dict[str, Any],
    live: dict[str, Any],
    provider: str,
    *,
    cache_hit: bool,
    looked_up_at: float,
) -> dict[str, Any]:
    result = offline.copy()
    live_line_type = live.get("lineType") or "Unknown"
    live_type_known = live_line_type != "Unknown"

    fields = (
        "carrier",
        "normalizedCarrier",
        "originalCarrier",
        "city",
        "state",
        "ported",
        "portedStatus",
        "portedDate",
        "lrn",
        "ocn",
        "spid",
        "altSpid",
        "mobileCountryCode",
        "mobileNetworkCode",
        "providerErrorCode",
        "validationErrors",
    )
    for field in fields:
        if field in live and live[field] not in (None, "", []):
            result[field] = live[field]

    if live.get("location"):
        result["location"] = live["location"]

    offline_unassigned = offline.get("numberStatus") == "unassigned_exchange"
    if isinstance(live.get("providerValid"), bool):
        if live_type_known or not offline_unassigned:
            result["valid"] = live["providerValid"]
    if live.get("providerValid") is False:
        result["numberStatus"] = "invalid_number"
    elif live_type_known:
        # A live provider record supersedes an older offline snapshot.
        result["numberStatus"] = "valid" if result["valid"] else "invalid_number"
        if offline_unassigned:
            result["assignedExchange"] = None

    if live_type_known:
        result["lineType"] = live_line_type
        result["lineSubtype"] = live.get("lineSubtype") or live_line_type
        result["estimated"] = False
        result["fallbackUsed"] = False
        result["classificationConfidence"] = "Live provider data"
        result["classificationNote"] = live.get("classificationNote") or ""
    else:
        result["estimated"] = True
        result["fallbackUsed"] = True
        result["classificationConfidence"] = offline.get(
            "classificationConfidence", "Low"
        )
        result["classificationNote"] = (
            f"{provider_display_name(provider)} returned an unknown line type. "
            f"Showing the offline result instead. {offline.get('classificationNote', '')}"
        ).strip()

    result["lookupProvider"] = provider
    result["lookupSource"] = (
        live.get("lookupSource")
        if live_type_known
        else f"Offline fallback after {provider_display_name(provider)}"
    )
    result["liveLookup"] = True
    result["cacheHit"] = cache_hit
    result["lookupTimestamp"] = utc_iso(looked_up_at)
    result["dataFreshness"] = (
        f"Cached live result ({result['lookupTimestamp']})"
        if cache_hit
        else f"Live lookup ({result['lookupTimestamp']})"
    )
    result["lookupError"] = (
        f"Provider type unavailable (code {live.get('providerErrorCode')})"
        if not live_type_known and live.get("providerErrorCode")
        else ""
    )
    return result


# snake_case line types used by the standalone phone-validator-api.
COMPAT_LINE_TYPES = {
    "Cell Phone": "cell_phone",
    "Landline": "landline",
    "Fixed or Mobile": "landline_or_cell_phone",
    "VoIP": "voip",
    "Toll-Free": "toll_free",
    "Premium Rate": "premium_rate",
    "Shared Cost": "shared_cost",
    "Personal Number": "personal_number",
    "Pager": "pager",
    "Universal Access": "uan",
    "Voicemail": "voicemail",
    "Satellite": "satellite",
    "Unknown": "unknown",
}


def add_compat_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Add the phone-validator-api response fields to a result.

    Existing fields are left untouched, so the portal and older scripts keep
    working, while scripts written for phone-validator-api (input, line_type,
    international_format, region, original_carrier, timezones) work unchanged.
    """
    parsed: phonenumbers.PhoneNumber | None = None
    if result.get("e164"):
        try:
            parsed = phonenumbers.parse(result["e164"], None)
        except phonenumbers.NumberParseException:
            parsed = None

    valid = bool(result.get("valid"))
    result["input"] = result.get("raw", "")
    result["line_type"] = (
        COMPAT_LINE_TYPES.get(result.get("lineType", "Unknown"), "unknown")
        if valid
        else None
    )
    result["region"] = result.get("country") or None
    result["calling_code"] = parsed.country_code if parsed else None
    result["international_format"] = (
        phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        if parsed
        else None
    )
    result["timezones"] = (
        [zone for zone in phone_timezone.time_zones_for_number(parsed) if zone != "Etc/Unknown"]
        if parsed
        else []
    )
    library_carrier = carrier.name_for_number(parsed, "en") if parsed and valid else ""
    result["original_carrier"] = (
        (result.get("originalCarrier") or library_carrier or None) if valid else None
    )
    if not valid and not result.get("error"):
        result["error"] = (
            result.get("classificationNote")
            if result.get("numberStatus") in {"unassigned_exchange", "fictional"}
            else "Number is not valid for its country's numbering plan"
        )
    return result


async def validate_number(
    phone: str,
    default_region: str = DEFAULT_REGION,
    lookup_mode: Literal["auto", "live", "offline"] = "auto",
    refresh_live: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    result = await _validate_number_core(
        phone,
        default_region,
        lookup_mode=lookup_mode,
        refresh_live=refresh_live,
        client=client,
    )
    return add_compat_fields(result)


async def _validate_number_core(
    phone: str,
    default_region: str = DEFAULT_REGION,
    lookup_mode: Literal["auto", "live", "offline"] = "auto",
    refresh_live: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    offline = validate_number_offline(phone, default_region)
    # Exchanges missing from the NANPA snapshot are re-checked live (when a
    # provider is configured) because the snapshot can be older than a new
    # assignment. Format errors and reserved 555 numbers never cost a lookup.
    recheck_live = offline.get("lookupSource") == "NANPA Central Office Code assignments"
    if lookup_mode == "offline" or (not offline["valid"] and not recheck_live):
        return offline

    providers = provider_chain(lookup_mode)
    if not providers:
        if lookup_mode == "live":
            offline["fallbackUsed"] = True
            offline["lookupError"] = (
                "Live lookup requested but no configured provider is available."
            )
            offline["classificationNote"] = (
                "Live lookup is not configured. " + offline["classificationNote"]
            )
        return offline

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS)

    errors: list[str] = []
    unknown_result: tuple[dict[str, Any], str, bool, float] | None = None
    try:
        for provider in providers:
            cached = None if refresh_live else cache_get(offline["e164"], provider)
            if cached:
                live, looked_up_at = cached
                cache_hit = True
            else:
                try:
                    live = await query_provider(provider, offline["e164"], client)
                except LiveLookupError as exc:
                    errors.append(str(exc))
                    continue
                looked_up_at = cache_put(offline["e164"], provider, live)
                cache_hit = False

            merged = merge_live_result(
                offline,
                live,
                provider,
                cache_hit=cache_hit,
                looked_up_at=looked_up_at,
            )
            if live.get("lineType") != "Unknown" or live.get("providerValid") is False:
                if errors:
                    merged["lookupError"] = "; ".join(errors)
                return merged
            unknown_result = (live, provider, cache_hit, looked_up_at)

        if unknown_result:
            live, provider, cache_hit, looked_up_at = unknown_result
            merged = merge_live_result(
                offline,
                live,
                provider,
                cache_hit=cache_hit,
                looked_up_at=looked_up_at,
            )
            combined = [message for message in errors if message]
            if merged.get("lookupError"):
                combined.append(merged["lookupError"])
            merged["lookupError"] = "; ".join(combined)
            return merged

        offline["fallbackUsed"] = True
        offline["lookupError"] = "; ".join(errors) or "Live lookup was unavailable."
        offline["lookupSource"] = "Offline fallback — live lookup failed"
        offline["classificationNote"] = (
            "All configured live lookups failed. " + offline["classificationNote"]
        )
        return offline
    finally:
        if owns_client and client is not None:
            await client.aclose()


def database_metadata() -> dict[str, str]:
    if not DATABASE_PATH.exists():
        return {}
    try:
        with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as connection:
            return dict(connection.execute("SELECT key, value FROM metadata").fetchall())
    except sqlite3.Error:
        return {}


@app.get("/", include_in_schema=False)
def portal() -> HTMLResponse:
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__PORTAL_TOKEN__", PORTAL_TOKEN),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "live-hybrid" if provider_chain() else "offline-fallback",
        "live_enabled": bool(provider_chain()),
        "database_ready": DATABASE_PATH.exists(),
    }


@app.get("/engine")
def engine_status() -> dict[str, Any]:
    metadata = database_metadata()
    providers = provider_chain()
    available = configured_providers()
    return {
        "live_enabled": bool(providers),
        "active_provider": provider_display_name(providers[0]) if providers else "",
        "fallback_providers": [provider_display_name(value) for value in providers[1:]],
        "configured_providers": [provider_display_name(value) for value in available],
        "provider_preference": LOOKUP_PROVIDER,
        "database_ready": DATABASE_PATH.exists(),
        "mode": "Live carrier intelligence with offline fallback"
        if providers
        else "Offline numbering metadata only",
        "file_updated": metadata.get("file_updated", "Unknown"),
        "method": metadata.get("method", "Carrier-name prefix estimate"),
        "rules_version": metadata.get("rules_version", "Legacy carrier rules"),
        "cache_hours": LOOKUP_CACHE_HOURS,
        "concurrency": LOOKUP_CONCURRENCY,
        "api_key_required": bool(API_KEYS),
        "accuracy_notice": (
            "No database can guarantee 100% accuracy or prove reachability. "
            "Live results are the provider's latest available carrier records."
        ),
    }


@app.get("/api", tags=["info"])
def api_info() -> dict[str, Any]:
    return {
        "service": "NumVerify Pro phone validation API",
        "docs": "/docs",
        "api_key_required": bool(API_KEYS),
        "endpoints": [
            "GET  /validate?number=...&region=..   (or ?phone=...&default_region=..)",
            "POST /validate          {\"phone_number\": \"...\"}",
            "POST /validate/batch    {\"numbers\": [...], \"region\": \"US\"}  -> list",
            "POST /validate/bulk     {\"numbers\": [...]}  -> {count, results}",
        ],
    }


@app.get("/validate", dependencies=[Depends(require_api_key)])
async def validate_phone(
    phone: str | None = Query(None, description="Phone number, preferably in E.164 format"),
    number: str | None = Query(None, description="Alias of phone (phone-validator-api style)"),
    default_region: str | None = Query(None, min_length=2, max_length=2),
    region: str | None = Query(
        None, min_length=2, max_length=2, description="Alias of default_region"
    ),
    lookup_mode: Literal["auto", "live", "offline"] = Query("auto"),
    refresh_live: bool = Query(False),
) -> dict[str, Any]:
    value = phone if phone is not None else number
    if value is None:
        raise HTTPException(status_code=422, detail="Provide ?phone=... or ?number=...")
    return await validate_number(
        value,
        default_region or region or DEFAULT_REGION,
        lookup_mode=lookup_mode,
        refresh_live=refresh_live,
    )


@app.post("/validate", dependencies=[Depends(require_api_key)])
async def validate_phone_json(payload: SingleValidationRequest) -> dict[str, Any]:
    """JSON body form: {"phone_number": "(415) 555-2671"} with an X-API-Key header."""
    return await validate_number(
        payload.phone_number,
        payload.default_region,
        lookup_mode=payload.lookup_mode,
        refresh_live=payload.refresh_live,
    )


async def run_batch(payload: BulkValidationRequest) -> list[dict[str, Any]]:
    if len(payload.numbers) > MAX_BATCH_NUMBERS:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_BATCH_NUMBERS:,} numbers per request",
        )
    unique_numbers = list(dict.fromkeys(str(number) for number in payload.numbers))
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)
    should_open_client = bool(provider_chain(payload.lookup_mode))

    async def run_one(number: str, client: httpx.AsyncClient | None) -> dict[str, Any]:
        async with semaphore:
            return await validate_number(
                number,
                payload.default_region,
                lookup_mode=payload.lookup_mode,
                refresh_live=payload.refresh_live,
                client=client,
            )

    if should_open_client:
        async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
            unique_values = await asyncio.gather(
                *(run_one(number, client) for number in unique_numbers)
            )
    else:
        unique_values = await asyncio.gather(
            *(run_one(number, None) for number in unique_numbers)
        )

    unique_results = dict(zip(unique_numbers, unique_values))
    return [unique_results[str(number)].copy() for number in payload.numbers]


@app.post("/validate/bulk", dependencies=[Depends(require_api_key)])
async def validate_bulk(payload: BulkValidationRequest) -> dict[str, Any]:
    results = await run_batch(payload)
    return {
        "count": len(results),
        "unique_count": len({str(number) for number in payload.numbers}),
        "live_enabled": bool(provider_chain(payload.lookup_mode)),
        "database_ready": DATABASE_PATH.exists(),
        "results": results,
    }


@app.post("/validate/batch", dependencies=[Depends(require_api_key)])
async def validate_batch(payload: BulkValidationRequest) -> list[dict[str, Any]]:
    """phone-validator-api style batch: returns a plain list of results."""
    return await run_batch(payload)


def open_portal() -> None:
    webbrowser.open("http://127.0.0.1:8000", new=2)


if __name__ == "__main__":
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.25, open_portal).start()
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
