"""Fare package validation, overlap pre-check and clearing semantics.

Run with:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from helpers import DIST, StoreTestCase, build_all_fixtures

from gtfs_store.errors import GtfsError
from gtfs_store.fares import FareStore, _match_rule
from gtfs_store.farepack import (
    find_priority_overlaps,
    format_minor,
    parse_amount,
    parse_fare_package,
    read_fare_package,
)
from gtfs_store.store import Store

ROOT = Path(__file__).resolve().parent.parent

STOP_ZONES = [
    {"stop_id": "007", "zone_id": "ZA"},
    {"stop_id": "7", "zone_id": "ZD"},
    {"stop_id": "S2", "zone_id": "ZB"},
    {"stop_id": "S3", "zone_id": "ZC"},
]
PRODUCTS = [
    {"product_id": "P_STD", "amount": "3.00", "currency": "CNY",
     "validity_seconds": 3600, "max_legs": 2, "covers": ["P_STD"]},
    {"product_id": "P_LONG", "amount": "5.00", "currency": "CNY",
     "validity_seconds": 7200, "max_legs": 2, "covers": ["P_STD", "P_LONG"]},
    {"product_id": "P_JPY", "amount": "250", "currency": "JPY",
     "validity_seconds": 3600, "max_legs": 2, "covers": ["P_JPY"]},
]
RULES = [
    {"rule_id": "r_any", "priority": 1, "route_id": "R1",
     "from_zone": None, "to_zone": None, "product_id": "P_STD"},
    {"rule_id": "r_r2", "priority": 10, "route_id": "R2",
     "from_zone": None, "to_zone": None, "product_id": "P_LONG"},
]


def fare_zip(path: Path, *, feed_key="demo", edition="v1", fare_version="fv",
             stop_zones=None, products=None, rules=None) -> Path:
    payload = {
        "manifest.json": {"feed_key": feed_key, "edition": edition,
                          "fare_version": fare_version},
        "stop_zones.json": STOP_ZONES if stop_zones is None else stop_zones,
        "products.json": PRODUCTS if products is None else products,
        "rules.json": RULES if rules is None else rules,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in payload.items():
            zf.writestr(name, json.dumps(data))
    return path


class FarePackParsingTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fare_fixtures.py")],
                       check=True, capture_output=True, cwd=str(ROOT))

    def setUp(self) -> None:
        super().setUp()
        self.load("demo", "v1", DIST / "demo-v1.zip")
        self.fares = FareStore(self.store)

    def _parse(self, members: dict[str, bytes]):
        return parse_fare_package(members)

    def _members(self, **overrides):
        path = fare_zip(self.tmp / "p.zip", **overrides)
        return read_fare_package(path)

    # -- money grammar -------------------------------------------------
    def test_amount_grammar_rejects_everything_but_fixed_point(self):
        good = {"CNY": [("0.00", 0), ("3.00", 300), ("12.34", 1234)],
                "USD": [("0.00", 0), ("99.99", 9999)],
                "JPY": [("0", 0), ("250", 250), ("1000", 1000)]}
        for currency, cases in good.items():
            for text, minor in cases:
                self.assertEqual(parse_amount(text, currency),
                                 (minor, format_minor(minor, currency)))
        bad = ["1", "1.0", "1.000", "1e2", "1.0e1", "NaN", "Infinity", "-1.00",
               "+1.00", ".50", "1.", " 1.00", "01.2"]
        for text in bad:
            self.assertIsNone(parse_amount(text, "CNY"), text)
        for text in ("250.0", "250.00", "1e2", "NaN", "Infinity", "-1"):
            self.assertIsNone(parse_amount(text, "JPY"), text)
        self.assertIsNone(parse_amount(3.00, "CNY"))       # non-string
        self.assertIsNone(parse_amount("1.00", "EUR"))     # unsupported currency
        self.assertIsNone(parse_amount("1.00", None))

    def test_zero_amount_product_is_valid(self):
        products = [dict(PRODUCTS[0], product_id="P_Z", amount="0.00",
                         covers=["P_Z"])]
        package = self._parse(self._members(products=products,
                                            rules=[dict(RULES[0], product_id="P_Z")]))
        self.assertTrue(package.ok, [p.message for p in package.problems])
        self.assertEqual(package.products[0]["amount_minor"], 0)
        self.assertEqual(package.products[0]["amount_text"], "0.00")

    # -- structural rejection ------------------------------------------
    def test_duplicate_stop_and_product_and_rule_ids_rejected(self):
        package = self._parse(self._members(
            stop_zones=STOP_ZONES + [{"stop_id": "007", "zone_id": "ZB"}],
            products=PRODUCTS + [dict(PRODUCTS[0])],
            rules=RULES + [dict(RULES[0])]))
        cats = {p.category for p in package.problems}
        self.assertIn("duplicate_id", cats)
        self.assertGreaterEqual(
            sum("mapped twice" in p.message for p in package.problems), 1)

    def test_dangling_coverage_and_missing_self_coverage_rejected(self):
        dangling = [dict(PRODUCTS[0], covers=["P_STD", "P_GHOST"])]
        package = self._parse(self._members(products=dangling))
        self.assertTrue(any(p.category == "foreign_key" for p in package.problems))
        no_self = [dict(PRODUCTS[0], covers=["P_LONG"])]
        package2 = self._parse(self._members(products=no_self))
        self.assertTrue(any("must include the product itself" in p.message
                            for p in package2.problems))

    def test_invalid_scalars_are_rejected_not_coerced(self):
        products = [
            dict(PRODUCTS[0], validity_seconds=0),
            dict(PRODUCTS[1], max_legs=-2),
            dict(PRODUCTS[2], amount="250.0"),
            dict(PRODUCTS[0], product_id="P_BOOL", validity_seconds=True,
                 covers=["P_BOOL"]),
        ]
        package = self._parse(self._members(products=products, rules=[]))
        self.assertFalse(package.ok)
        fields = {p.field for p in package.problems}
        self.assertIn("validity_seconds", fields)
        self.assertIn("max_legs", fields)
        self.assertIn("amount", fields)

    def test_duplicate_json_keys_and_unknown_fields_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"feed_key": "demo", "edition": "v1", "fare_version": "fv"}))
            zf.writestr("stop_zones.json", json.dumps(STOP_ZONES))
            zf.writestr("products.json", json.dumps(PRODUCTS))
            zf.writestr("rules.json",
                        '[{"rule_id": "r", "priority": 1, "route_id": null, '
                        '"from_zone": null, "to_zone": null, "product_id": "P_STD", '
                        '"extra": 1}]')
        package = parse_fare_package(read_fare_package("x.zip", buf.getvalue()))
        self.assertTrue(any(p.category == "unsupported_semantics"
                            for p in package.problems))

        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as zf:
            zf.writestr("manifest.json",
                        '{"feed_key": "demo", "feed_key": "demo", '
                        '"edition": "v1", "fare_version": "fv"}')
            zf.writestr("stop_zones.json", json.dumps(STOP_ZONES))
            zf.writestr("products.json", json.dumps(PRODUCTS))
            zf.writestr("rules.json", json.dumps(RULES))
        package2 = parse_fare_package(read_fare_package("x.zip", buf2.getvalue()))
        self.assertTrue(any(p.category == "duplicate_key" for p in package2.problems))

    def test_empty_string_condition_is_literal_not_wildcard(self):
        rules = [{"rule_id": "r_empty", "priority": 5, "route_id": "",
                  "from_zone": "", "to_zone": "", "product_id": "P_STD"}]
        package = self._parse(self._members(rules=rules))
        self.assertTrue(package.ok, [p.message for p in package.problems])
        # Empty strings never match non-empty real ids.
        self.assertIsNone(_match_rule(rules, "R1", "ZA", "ZB"))
        # Null conditions do match.
        wide = [{"rule_id": "r_wide", "priority": 1, "route_id": None,
                 "from_zone": None, "to_zone": None, "product_id": "P_STD"}]
        self.assertEqual(_match_rule(wide, "R1", "ZA", "ZB")["rule_id"], "r_wide")

    def test_unknown_zone_and_missing_file_rejected(self):
        rules = [dict(RULES[0], from_zone="ZZ")]
        package = self._parse(self._members(rules=rules))
        self.assertTrue(any(p.category == "foreign_key" for p in package.problems))
        with zipfile.ZipFile(self.tmp / "m.zip", "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"feed_key": "demo", "edition": "v1", "fare_version": "fv"}))
            zf.writestr("products.json", json.dumps(PRODUCTS))
            zf.writestr("rules.json", json.dumps(RULES))
        with self.assertRaises(GtfsError):
            read_fare_package(self.tmp / "m.zip")


class OverlapTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load("demo", "v1", DIST / "demo-v1.zip")
        self.fares = FareStore(self.store)

    def _rule(self, rid, prio, route, from_zone, to_zone, product="P_STD"):
        return {"rule_id": rid, "priority": prio, "route_id": route,
                "from_zone": from_zone, "to_zone": to_zone, "product_id": product}

    def test_overlap_uses_condition_intersection_with_witness(self):
        routes = {"R1", "R2"}
        zones = {"ZA", "ZB", "ZC"}
        # Different spellings: null route vs explicit, null to_zone vs explicit.
        a = self._rule("a", 1, None, "ZA", None)
        b = self._rule("b", 1, "R1", None, "ZB")
        conflicts = find_priority_overlaps([a, b], routes, zones)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["witness"],
                         {"route_id": "R1", "from_zone": "ZA", "to_zone": "ZB"})

    def test_identical_conditions_also_conflict_even_same_product(self):
        a = self._rule("a", 5, "R1", "ZA", "ZB")
        b = self._rule("b", 5, "R1", "ZA", "ZB", product="P_STD")
        conflicts = find_priority_overlaps([a, b], {"R1"}, {"ZA", "ZB"})
        self.assertEqual(len(conflicts), 1)

    def test_disjoint_conditions_and_different_priorities_are_fine(self):
        # The narrower zone-scoped rules sit at a higher priority, so their
        # overlap with the wide route rule is resolved by priority, not a tie.
        a = self._rule("a", 1, "R1", None, None)
        b = self._rule("b", 1, "R2", None, None)        # disjoint routes
        c = self._rule("c", 5, "R1", "ZA", "ZB")
        d = self._rule("d", 5, "R1", "ZB", "ZC")        # disjoint zone pairs
        e = self._rule("e", 2, "R1", None, None)        # higher priority, allowed
        conflicts = find_priority_overlaps(
            [a, b, c, d, e], {"R1", "R2"}, {"ZA", "ZB", "ZC"})
        self.assertEqual(conflicts, [])

    def test_literal_empty_string_does_not_intersect_real_zones(self):
        a = self._rule("a", 1, "R1", None, None)
        b = self._rule("b", 1, "R1", "", "")
        conflicts = find_priority_overlaps([a, b], {"R1"}, {"ZA", "ZB"})
        self.assertEqual(conflicts, [])

    def test_installed_overlap_package_is_rejected_with_witness(self):
        result = self.fares.fare_install(str(DIST / "fare-reject-overlap.zip"))
        self.assertEqual(result["status"], "rejected")
        problem = next(p for p in result["problems"] if p["category"] == "rule_overlap")
        self.assertIn("witness", problem.get("context", {}))


class FareInstallTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fare_fixtures.py")],
                       check=True, capture_output=True, cwd=str(ROOT))

    def setUp(self) -> None:
        super().setUp()
        self.load("demo", "v1", DIST / "demo-v1.zip")
        self.fares = FareStore(self.store)

    def test_install_and_idempotent_resend_and_version_conflict(self):
        first = self.fares.fare_install(str(DIST / "fare-v1.zip"))
        self.assertEqual(first["status"], "ready")
        again = self.fares.fare_install(str(DIST / "fare-v1.zip"))
        self.assertTrue(again["resend"])
        self.assertEqual(again["status"], "ready")
        # Same version key, different bytes -> hard conflict.
        with self.assertRaises(GtfsError):
            self.fares.fare_install(str(DIST / "fare-v2.zip"),
                                    raw_bytes=_relabel(DIST / "fare-v2.zip", "fare-v1"))
        # v2 itself installs and does not alter v1.
        v2 = self.fares.fare_install(str(DIST / "fare-v2.zip"))
        self.assertEqual(v2["status"], "ready")
        v1_products = {p["product_id"]: p["amount_text"]
                       for p in self.fares.fare_products("demo", "v1", "fare-v1")}
        self.assertEqual(v1_products["P_STD"], "3.00")
        v2_products = {p["product_id"]: p["amount_text"]
                       for p in self.fares.fare_products("demo", "v1", "fare-v2")}
        self.assertEqual(v2_products["P_STD"], "3.50")

    def test_rejected_packages_do_not_create_ready_versions(self):
        for name in ("fare-reject-amount.zip", "fare-reject-dangling.zip"):
            result = self.fares.fare_install(str(DIST / name))
            self.assertEqual(result["status"], "rejected")
        ready = {r["fare_version"] for r in self.fares.fare_packages()
                 if r["status"] == "ready"}
        self.assertEqual(ready, set())
        self.assertTrue(self.fares.fare_rejections("demo", "v1"))

    def test_binding_to_missing_static_edition_is_rejected(self):
        path = fare_zip(self.tmp / "wrong.zip", feed_key="demo", edition="nope")
        result = self.fares.fare_install(str(path))
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any(p["category"] == "foreign_key" for p in result["problems"]))

    def test_mapped_stop_and_rule_route_must_exist_in_static_edition(self):
        bad_zones = STOP_ZONES + [{"stop_id": "GHOST", "zone_id": "ZA"}]
        path = fare_zip(self.tmp / "bad-stop.zip", stop_zones=bad_zones,
                        fare_version="fv-bad-stop")
        result = self.fares.fare_install(str(path))
        self.assertTrue(any("GHOST" in p["message"] for p in result["problems"]))
        bad_rules = RULES + [{"rule_id": "r_x", "priority": 9, "route_id": "R99",
                              "from_zone": None, "to_zone": None,
                              "product_id": "P_STD"}]
        path2 = fare_zip(self.tmp / "bad-route.zip", rules=bad_rules,
                         fare_version="fv-bad-route")
        result2 = self.fares.fare_install(str(path2))
        self.assertTrue(any("R99" in p["message"] for p in result2["problems"]))

    def test_unsafe_archive_member_is_refused(self):
        with zipfile.ZipFile(self.tmp / "evil.zip", "w") as zf:
            zf.writestr("../escape.json", "{}")
            for name in ("manifest.json", "stop_zones.json", "products.json",
                         "rules.json"):
                zf.writestr(name, "[]")
        with self.assertRaises(GtfsError):
            self.fares.fare_install(str(self.tmp / "evil.zip"))


BATCH_HEADER = ("journey_id,leg_index,trip_id,board_sequence,alight_sequence,"
                "board_utc,alight_utc\r\n")


def batch_csv(rows: list[tuple]) -> bytes:
    out = BATCH_HEADER
    for row in rows:
        out += ",".join(str(v) for v in row) + "\r\n"
    return out.encode()


class ClearingTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fare_fixtures.py")],
                       check=True, capture_output=True, cwd=str(ROOT))

    def setUp(self) -> None:
        super().setUp()
        self.load("demo", "v1", DIST / "demo-v1.zip")
        self.fares = FareStore(self.store)
        self.fares.fare_install(str(DIST / "fare-v1.zip"))

    def settle(self, rows):
        return self.fares.settle("demo", "v1", "fare-v1", batch_csv(rows))

    def journey(self, result, journey_id):
        return next(j for j in result["journeys"] if j["journey_id"] == journey_id)

    def test_demo_batch_matches_hand_ledger_totals(self):
        result = self.fares.settle(
            "demo", "v1", "fare-v1", (DIST / "fare-batch.csv").read_bytes())
        totals = {j["journey_id"]: (j["status"], j.get("total_text"))
                  for j in result["journeys"]}
        self.assertEqual(totals, {
            "J1": ("settled", "6.00"), "J2": ("settled", "10.00"),
            "J3": ("settled", "5.00"), "J3b": ("settled", "8.00"),
            "J4": ("settled", "5.00"), "J5": ("settled", "0.00"),
            "J6": ("settled", "250"), "J7": ("rejected", None),
            "J8": ("rejected", None), "J9": ("settled", "5.00"),
        })
        self.assertEqual(result["summary"]["by_currency"]["CNY"]["text"], "39.00")
        self.assertEqual(result["summary"]["by_currency"]["JPY"]["text"], "250")

    def test_free_transfer_keeps_original_expiry_and_quota_count(self):
        rows = [
            ("K", 1, "T_WKDY", 10, 20, 1000, 1100),
            ("K", 2, "T_LATE", 1, 2, 2000, 2100),
        ]
        journey = self.journey(self.settle(rows), "K")
        leg1, leg2 = journey["legs"]
        self.assertEqual(leg2["charge_minor"], 0)
        self.assertEqual(leg2["source_leg_index"], 1)
        self.assertEqual(leg2["ticket_expires_utc"], leg1["ticket_expires_utc"])
        self.assertEqual(leg2["used_legs_after"], 2)
        self.assertEqual(leg2["purchase_reasons"], [])

    def test_boarding_exactly_at_expiry_rebuys_but_overrun_alight_is_free(self):
        rows = [
            ("K", 1, "T_WKDY", 10, 20, 1000, 1100),     # P_STD expires 4600, max_legs 2
            ("K", 2, "T_LATE", 1, 2, 4600, 99999),      # boards == expiry, alights later
        ]
        journey = self.journey(self.settle(rows), "K")
        # Window is exactly exhausted (strict < comparison); quota still has
        # room, so only the expiry reason applies.
        self.assertEqual(journey["legs"][1]["purchase_reasons"], ["expired"])
        self.assertEqual(journey["legs"][1]["charge_text"], "3.00")
        # No surcharge for alighting past the new ticket's own expiry.
        self.assertEqual(journey["total_text"], "6.00")

    def test_quota_exhausted_while_window_still_open(self):
        rows = [
            ("K", 1, "T_FREQ_EXACT", 1, 2, 1000, 1100),
            ("K", 2, "T_FREQ_EXACT", 1, 2, 1200, 1300),
            ("K", 3, "T_FREQ_EXACT", 1, 2, 1400, 1500),
        ]
        journey = self.journey(self.settle(rows), "K")
        self.assertEqual(journey["legs"][2]["purchase_reasons"], ["quota_exhausted"])
        self.assertNotIn("expired", journey["legs"][2]["purchase_reasons"])

    def test_non_consecutive_stop_sequences_and_leading_zero_stop_id(self):
        rows = [("K", 1, "T_WKDY", 10, 40, 1000, 1100)]   # 10 -> 40, gap is legal
        journey = self.journey(self.settle(rows), "K")
        self.assertEqual(journey["status"], "settled")
        self.assertEqual(journey["legs"][0]["board_stop_id"], "007")
        self.assertEqual(journey["legs"][0]["alight_stop_id"], "S4")

    def test_shuffled_rows_are_ordered_by_leg_index_and_chain_times_checked(self):
        rows = [
            ("K", 3, "T_WKDY", 30, 40, 3000, 3100),
            ("K", 1, "T_WKDY", 10, 20, 1000, 1100),
            ("K", 2, "T_LATE", 1, 2, 2000, 2100),
        ]
        result = self.settle(rows)
        journey = self.journey(result, "K")
        self.assertEqual([l["leg_index"] for l in journey["legs"]], [1, 2, 3])
        bad = [
            ("K", 2, "T_LATE", 1, 2, 500, 600),   # boards before leg1 alights
            ("K", 1, "T_WKDY", 10, 20, 1000, 1100),
        ]
        rejected = self.journey(self.settle(bad), "K")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reasons"][0]["code"], "bad_chain_order")

    def test_leg_index_must_be_unique_and_contiguous_from_one(self):
        gap = [("K", 1, "T_WKDY", 10, 20, 1000, 1100),
               ("K", 3, "T_LATE", 1, 2, 2000, 2100)]
        self.assertEqual(self.journey(self.settle(gap), "K")["reasons"][0]["code"],
                         "non_contiguous_leg_index")
        dup = [("K", 1, "T_WKDY", 10, 20, 1000, 1100),
               ("K", 1, "T_LATE", 1, 2, 2000, 2100)]
        self.assertEqual(self.journey(self.settle(dup), "K")["reasons"][0]["code"],
                         "duplicate_leg_index")

    def test_static_identity_and_order_failures_are_local_to_the_journey(self):
        rows = [
            ("good", 1, "T_WKDY", 10, 20, 1000, 1100),
            ("bad_trip", 1, "T_NOPE", 1, 2, 1000, 1100),
            ("bad_seq", 1, "T_WKDY", 40, 10, 1000, 1100),
            ("bad_time", 1, "T_WKDY", 10, 20, 2000, 1000),
        ]
        result = self.settle(rows)
        statuses = {j["journey_id"]: j["status"] for j in result["journeys"]}
        self.assertEqual(statuses["good"], "settled")
        for journey_id, code in (("bad_trip", "unknown_trip"),
                                 ("bad_seq", "bad_sequence_order"),
                                 ("bad_time", "bad_time_order")):
            journey = self.journey(result, journey_id)
            self.assertEqual(journey["status"], "rejected")
            self.assertIn(code, {r["code"] for r in journey["reasons"]})
            self.assertIsNone(journey["total_text"])

    def test_unmapped_stop_and_no_rule_fail_with_leg_locators(self):
        # T_FREQ_WINDOW S2->S3 is R3 ZB->ZC, deliberately rule-free.
        no_rule = [("K", 1, "T_FREQ_WINDOW", 2, 3, 1000, 1100)]
        journey = self.journey(self.settle(no_rule), "K")
        self.assertEqual(journey["reasons"][0]["code"], "no_rule")
        self.assertEqual(journey["reasons"][0]["leg_index"], 1)

        # A package that drops a zone a rule condition names cannot install.
        base_dir = ROOT / "fixtures" / "fares" / "fare-v1"
        zones = json.loads((base_dir / "stop_zones.json").read_text())
        rules = json.loads((base_dir / "rules.json").read_text())
        products = json.loads((base_dir / "products.json").read_text())
        without_za = [z for z in zones if z["zone_id"] != "ZA"]
        broken = fare_zip(self.tmp / "broken-zone.zip", stop_zones=without_za,
                          rules=rules, products=products,
                          fare_version="fv-broken-zone")
        self.assertEqual(self.fares.fare_install(str(broken))["status"], "rejected")

        # Dropping a zone no rule condition names still installs; clearing a
        # leg that touches it then reports stop_without_zone on that leg.
        # ZE appears only as the zone of S8; no rule names ZE.
        without_ze = [z for z in zones if z["zone_id"] != "ZE"]
        path = fare_zip(self.tmp / "nozone.zip", stop_zones=without_ze,
                        rules=rules, products=products, fare_version="fv-nozone")
        self.assertEqual(self.fares.fare_install(str(path))["status"], "ready")
        result = self.fares.settle(
            "demo", "v1", "fv-nozone",
            batch_csv([("K", 1, "T_ID7", 1, 2, 1000, 1100)]))   # 7(ZD) -> S8(ZE)
        failed = self.journey(result, "K")
        self.assertEqual(failed["status"], "rejected")
        self.assertEqual(failed["reasons"][0]["code"], "stop_without_zone")
        self.assertEqual(failed["reasons"][0]["leg_index"], 1)

    def test_mixed_currency_rejects_whole_chain_without_total(self):
        rows = [
            ("K", 1, "T_WKDY", 10, 20, 1000, 1100),      # CNY
            ("K", 2, "T_ADJACENT", 1, 2, 2000, 2100),    # JPY
        ]
        journey = self.journey(self.settle(rows), "K")
        self.assertEqual(journey["status"], "rejected")
        self.assertEqual(journey["reasons"][0]["code"], "mixed_currency")
        self.assertIsNone(journey["total_minor"])

    def test_batch_level_parse_problem_aborts_with_exit_reason_data(self):
        bad = BATCH_HEADER.encode() + b"K,notint,T_WKDY,10,20,1000,1100\r\n"
        result = self.fares.settle("demo", "v1", "fare-v1", bad)
        self.assertTrue(result["batch_problems"])
        self.assertEqual(result["batch_problems"][0]["input_line"], 2)

    def test_one_journey_failure_does_not_affect_another_in_same_batch(self):
        rows = [
            ("ok", 1, "T_WKDY", 10, 20, 1000, 1100),
            ("dead", 1, "T_FREQ_WINDOW", 2, 3, 1000, 1100),   # no rule
        ]
        result = self.settle(rows)
        statuses = {j["journey_id"]: j["status"] for j in result["journeys"]}
        self.assertEqual(statuses, {"ok": "settled", "dead": "rejected"})

    def test_reopen_database_reproduces_identical_settlement(self):
        rows = batch_csv([("K", 1, "T_WKDY", 10, 20, 1000, 1100)])
        first = self.fares.settle("demo", "v1", "fare-v1", rows)
        self.store.close()
        reopened = Store(self.db_path)
        fares2 = FareStore(reopened)
        second = fares2.settle("demo", "v1", "fare-v1", rows)
        self.assertEqual(json.dumps(second, sort_keys=True),
                         json.dumps(first, sort_keys=True))
        reopened.close()
        self.store = Store(self.db_path)
        self.fares = FareStore(self.store)


def _relabel(path: Path, version: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["fare_version"] = version
                data = json.dumps(manifest).encode()
            dst.writestr(info.filename, data)
    return out.getvalue()
