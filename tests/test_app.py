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


if __name__ == "__main__":
    unittest.main()
