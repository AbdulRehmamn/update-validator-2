import unittest

import app
import update_offline_database


class LineTypeMappingTests(unittest.TestCase):
    def test_provider_type_mapping_covers_all_primary_categories(self):
        cases = {
            "mobile": ("Cell Phone", "Mobile"),
            "landline": ("Landline", "Fixed Line"),
            "fixedVoip": ("VoIP", "Fixed VoIP"),
            "nonFixedVoip": ("VoIP", "Non-Fixed VoIP"),
            "tollFree": ("Toll-Free", "Toll-Free"),
            "premium": ("Premium Rate", "Premium Rate"),
            "sharedCost": ("Shared Cost", "Shared Cost"),
            "uan": ("Universal Access", "Universal Access Number"),
            "voicemail": ("Voicemail", "Voicemail"),
            "pager": ("Pager", "Pager"),
            "personal": ("Personal Number", "Personal Number"),
            "satellite": ("Satellite", "Satellite"),
        }
        for raw_type, expected in cases.items():
            with self.subTest(raw_type=raw_type):
                self.assertEqual(app.normalize_provider_type(raw_type), expected)

    def test_invalid_number_is_rejected(self):
        result = app.validate_number_offline("123")
        self.assertFalse(result["valid"])
        self.assertIn(result["lineType"], {"Unknown", "Fixed or Mobile"})

    def test_toll_free_is_separated_offline(self):
        result = app.validate_number_offline("+18885550100")
        self.assertTrue(result["valid"])
        self.assertEqual(result["lineType"], "Toll-Free")

    def test_verizon_regional_company_is_landline(self):
        self.assertEqual(
            update_offline_database.estimate_company_type("VERIZON NEW YORK, INC."),
            ("Landline", "Medium"),
        )

    def test_verizon_wireless_stays_mobile(self):
        self.assertEqual(
            update_offline_database.estimate_company_type(
                "CELLCO PARTNERSHIP DBA VERIZON WIRELESS - NY"
            ),
            ("Cell Phone", "Medium"),
        )

    def test_nationwide_carrier_family_rules(self):
        cases = {
            "ILLINOIS BELL TEL CO": ("Landline", "Medium"),
            "TELEPORT COMMUNICATIONS AMERICA, LLC - CA": ("Landline", "Medium"),
            "BRIGHTSPEED OF NORTHWEST OHIO, INC.": ("Landline", "Medium"),
            "FRACTEL, LLC": ("VoIP", "Medium"),
            "TWC IP ENABLED SERVICES, LLC": ("VoIP", "Medium"),
            "SPOK, INC.": ("Pager", "High"),
            "BOOST SUBSCRIBERCO L.L.C.": ("Cell Phone", "Medium"),
            "GLOBALSTAR USA, INC.": ("Satellite", "High"),
        }
        for company, expected in cases.items():
            with self.subTest(company=company):
                self.assertEqual(
                    update_offline_database.estimate_company_type(company),
                    expected,
                )

    def test_bundled_database_covers_all_us_time_zones(self):
        # Representative non-pooled fixed-network prefixes from Eastern,
        # Central, Mountain, Pacific, Alaska, and Hawaii assignments.
        examples = {
            "2122050000": "NY",
            "2172210000": "IL",
            "3032560000": "CO",
            "3103690000": "CA",
            "9072430000": "AK",
            "8082350000": "HI",
        }
        for number, state in examples.items():
            with self.subTest(state=state):
                record = app.lookup_nanpa_prefix(number)
                self.assertIsNotNone(record)
                self.assertEqual(record["state"], state)
                self.assertEqual(record["estimated_type"], "Landline")

    def test_screenshot_prefixes_are_classified_as_landline(self):
        for phone in (
            "+15163333739",
            "+16315801816",
            "+16318428889",
            "+16314215525",
            "+15169972895",
        ):
            with self.subTest(phone=phone):
                result = app.validate_number_offline(phone)
                self.assertEqual(result["carrier"], "VERIZON NEW YORK, INC.")
                self.assertEqual(result["lineType"], "Landline")


class ProviderPayloadTests(unittest.TestCase):
    def test_telnyx_prefers_portability_line_type_and_current_carrier(self):
        result = app.parse_telnyx_payload(
            {
                "data": {
                    "carrier": {
                        "name": "Original Carrier",
                        "normalized_carrier": "Normalized Carrier",
                        "type": "landline",
                        "mobile_country_code": "310",
                        "mobile_network_code": "260",
                        "error_code": None,
                    },
                    "portability": {
                        "line_type": "voip",
                        "ported_status": "Y",
                        "ported_date": "2025-04-15",
                        "lrn": "2125550100",
                        "ocn": "1234",
                        "spid": "5678",
                        "spid_carrier_name": "Current Carrier",
                        "city": "NEW YORK",
                        "state": "New York",
                    },
                }
            }
        )
        self.assertEqual(result["lineType"], "VoIP")
        self.assertEqual(result["carrier"], "Current Carrier")
        self.assertTrue(result["ported"])
        self.assertEqual(result["lrn"], "2125550100")
        self.assertEqual(result["location"], "NEW YORK, New York")

    def test_twilio_preserves_voip_subtype(self):
        result = app.parse_twilio_payload(
            {
                "valid": True,
                "line_type_intelligence": {
                    "type": "nonFixedVoip",
                    "carrier_name": "Example VoIP",
                    "mobile_country_code": "310",
                    "mobile_network_code": "999",
                    "error_code": None,
                },
            }
        )
        self.assertEqual(result["lineType"], "VoIP")
        self.assertEqual(result["lineSubtype"], "Non-Fixed VoIP")
        self.assertEqual(result["carrier"], "Example VoIP")

    def test_live_result_overrides_offline_estimate(self):
        offline = app.validate_number_offline("+16077341118")
        live = {
            "providerValid": True,
            "lineType": "Cell Phone",
            "lineSubtype": "Mobile",
            "carrier": "Current Wireless Carrier",
            "ported": True,
            "portedStatus": "Ported",
            "lookupSource": "Test live data",
            "classificationNote": "Test provider result.",
        }
        merged = app.merge_live_result(
            offline,
            live,
            "telnyx",
            cache_hit=False,
            looked_up_at=1_700_000_000,
        )
        self.assertEqual(merged["lineType"], "Cell Phone")
        self.assertEqual(merged["carrier"], "Current Wireless Carrier")
        self.assertTrue(merged["liveLookup"])
        self.assertFalse(merged["estimated"])


class AccuracyRuleTests(unittest.TestCase):
    def test_555_number_is_not_in_service(self):
        result = app.validate_number_offline("(415) 555-2671")
        self.assertFalse(result["valid"])
        self.assertTrue(result["possible"])
        self.assertEqual(result["numberStatus"], "unassigned_exchange")
        self.assertEqual(result["e164"], "+14155552671")
        self.assertEqual(result["location"], "San Francisco, CA")

    def test_fictional_555_01xx_range(self):
        for phone in ("2025550143", "+13125550199"):
            with self.subTest(phone=phone):
                result = app.validate_number_offline(phone)
                self.assertFalse(result["valid"])
                self.assertEqual(result["numberStatus"], "fictional")

    def test_directory_assistance_is_special_service(self):
        result = app.validate_number_offline("212-555-1212")
        self.assertEqual(result["numberStatus"], "special_service")

    def test_unassigned_exchange_in_known_area_code(self):
        # 415 is a covered San Francisco area code; find an NXX it lacks.
        missing = next(
            nxx
            for nxx in map(str, range(200, 1000))
            if nxx != "555"
            and app.lookup_nanpa_prefix(f"415{nxx}0123") is None
            and app.phonenumbers.is_valid_number(
                app.phonenumbers.parse(f"+1415{nxx}0123")
            )
        )
        result = app.validate_number_offline(f"+1415{missing}0123")
        self.assertFalse(result["valid"])
        self.assertEqual(result["numberStatus"], "unassigned_exchange")
        self.assertEqual(result["lookupSource"], "NANPA Central Office Code assignments")

    def test_assigned_exchange_stays_valid(self):
        result = app.validate_number_offline("+14152220123")
        self.assertTrue(result["valid"])
        self.assertEqual(result["numberStatus"], "valid")
        self.assertTrue(result["assignedExchange"])

    def test_toll_free_555_is_not_flagged(self):
        result = app.validate_number_offline("+18005550199")
        self.assertTrue(result["valid"])
        self.assertEqual(result["lineType"], "Toll-Free")

    def test_toll_free_and_canada_skip_unassigned_check(self):
        for phone in ("+18885550100", "+14165550000", "+16135550000"):
            with self.subTest(phone=phone):
                result = app.validate_number_offline(phone)
                self.assertNotEqual(
                    result["lookupSource"], "NANPA Central Office Code assignments"
                )

    def test_unknown_live_type_does_not_revive_unassigned_number(self):
        offline = app.validate_number_offline("+14155552671")
        offline["lookupSource"] = "NANPA Central Office Code assignments"
        merged = app.merge_live_result(
            offline,
            {"providerValid": True, "lineType": "Unknown"},
            "telnyx",
            cache_hit=False,
            looked_up_at=1_700_000_000,
        )
        self.assertFalse(merged["valid"])
        self.assertEqual(merged["numberStatus"], "unassigned_exchange")

    def test_known_live_type_overrides_stale_snapshot(self):
        offline = app.validate_number_offline("+14155552671")
        merged = app.merge_live_result(
            offline,
            {"providerValid": True, "lineType": "Cell Phone", "carrier": "X"},
            "telnyx",
            cache_hit=False,
            looked_up_at=1_700_000_000,
        )
        self.assertTrue(merged["valid"])
        self.assertEqual(merged["numberStatus"], "valid")


class ApiKeyEndpointTests(unittest.TestCase):
    KEY = "val_live_test_key_123"

    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved_keys = app.API_KEYS
        app.API_KEYS = (self.KEY,)
        self.client = TestClient(app.app)

    def tearDown(self):
        app.API_KEYS = self._saved_keys

    def test_post_validate_with_key(self):
        response = self.client.post(
            "/validate",
            json={"phone_number": "(415) 555-2671", "lookup_mode": "offline"},
            headers={"X-API-Key": self.KEY},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["e164"], "+14155552671")
        self.assertEqual(body["numberStatus"], "unassigned_exchange")

    def test_bearer_and_alias_fields(self):
        response = self.client.post(
            "/validate",
            json={"phone": "+14152220123", "lookup_mode": "offline"},
            headers={"Authorization": f"Bearer {self.KEY}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])

    def test_missing_or_wrong_key_rejected(self):
        payload = {"phone_number": "+14152220123"}
        self.assertEqual(self.client.post("/validate", json=payload).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/validate", json=payload, headers={"X-API-Key": "wrong"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/validate", params={"phone": "+14152220123"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/validate/bulk", json={"numbers": ["1"]}).status_code,
            401,
        )

    def test_portal_token_allows_browser_ui(self):
        page = self.client.get("/").text
        self.assertIn(app.PORTAL_TOKEN, page)
        response = self.client.get(
            "/validate",
            params={"phone": "+14152220123", "lookup_mode": "offline"},
            headers={"X-Portal-Token": app.PORTAL_TOKEN},
        )
        self.assertEqual(response.status_code, 200)

    def test_open_when_no_keys_configured(self):
        app.API_KEYS = ()
        response = self.client.post(
            "/validate", json={"phone_number": "+14152220123", "lookup_mode": "offline"}
        )
        self.assertEqual(response.status_code, 200)


class PhoneValidatorApiCompatTests(unittest.TestCase):
    """Requests written for the standalone phone-validator-api."""

    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved_keys = app.API_KEYS
        app.API_KEYS = ()
        self.client = TestClient(app.app)

    def tearDown(self):
        app.API_KEYS = self._saved_keys

    def test_get_with_number_and_region(self):
        body = self.client.get(
            "/validate", params={"number": "03001234567", "region": "PK"}
        ).json()
        self.assertTrue(body["valid"])
        self.assertEqual(body["line_type"], "cell_phone")
        self.assertEqual(body["region"], "PK")
        self.assertEqual(body["calling_code"], 92)
        self.assertEqual(body["international_format"], "+92 300 1234567")
        self.assertIn("Asia/Karachi", body["timezones"])
        self.assertEqual(body["input"], "03001234567")

    def test_toll_free_line_type(self):
        body = self.client.get("/validate", params={"number": "+18002752273"}).json()
        self.assertEqual(body["line_type"], "toll_free")
        self.assertEqual(body["international_format"], "+1 800-275-2273")

    def test_us_landline_uses_prefix_database(self):
        body = self.client.get(
            "/validate", params={"number": "+12127365000", "lookup_mode": "offline"}
        ).json()
        self.assertEqual(body["line_type"], "landline")
        self.assertEqual(body["original_carrier"], "VERIZON NEW YORK, INC.")

    def test_invalid_numbers_carry_error(self):
        body = self.client.get("/validate", params={"number": "+14155552671"}).json()
        self.assertFalse(body["valid"])
        self.assertIsNone(body["line_type"])
        self.assertIn("555", body["error"])
        body = self.client.get("/validate", params={"number": "12"}).json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["error"])

    def test_missing_number_is_422(self):
        self.assertEqual(self.client.get("/validate").status_code, 422)

    def test_batch_returns_list(self):
        response = self.client.post(
            "/validate/batch",
            json={
                "numbers": ["+923001234567", "+18002752273", "042 35761234"],
                "region": "PK",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsInstance(body, list)
        self.assertEqual(
            [item["line_type"] for item in body], ["cell_phone", "toll_free", "landline"]
        )

    def test_batch_limit(self):
        response = self.client.post(
            "/validate/batch", json={"numbers": ["+18002752273"] * 10_001}
        )
        self.assertEqual(response.status_code, 400)

    def test_original_query_names_still_work(self):
        body = self.client.get(
            "/validate", params={"phone": "2127365000", "default_region": "US"}
        ).json()
        self.assertTrue(body["valid"])
        bulk = self.client.post("/validate/bulk", json={"numbers": ["2127365000"]}).json()
        self.assertEqual(bulk["count"], 1)
        self.assertEqual(bulk["results"][0]["line_type"], "landline")

    def test_batch_requires_key_when_configured(self):
        app.API_KEYS = ("k",)
        payload = {"numbers": ["+18002752273"]}
        self.assertEqual(self.client.post("/validate/batch", json=payload).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/validate/batch", json=payload, headers={"X-API-Key": "k"}
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
