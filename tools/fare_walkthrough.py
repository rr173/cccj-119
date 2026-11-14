"""Offline fare clearing drill: hand-computed ledger vs. the real engine.

Usage:  python3 -I tools/fare_walkthrough.py [--db PATH]

Every expected amount, rule hit, ticket source leg, expiry and used-segment
count below is written by hand from the fare-v1 contract.  The drill then
cross-checks the engine two independent ways:

1. exact comparison of every per-leg field against the hand-written ledger
   (not just journey totals or success messages);
2. a second, deliberately tiny ticket state machine implemented in this
   file from the contract text, fed only with the hand ledger's base
   products, and compared leg-by-leg with the engine output.

The transcript is copied into docs/FARE_WALKTHROUGH.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.errors import GtfsError  # noqa: E402
from gtfs_store.fares import FareStore  # noqa: E402
from gtfs_store.store import Store  # noqa: E402

DIST = ROOT / "dist"
FEED_KEY, EDITION = "demo", "v1"

FAILURES: list[str] = []


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    print("  %-64s %s" % (label, "OK" if ok else
                          "MISMATCH\n    expected: %r\n    actual:   %r" % (expected, actual)))
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Hand-written expected ledger for fare-v1 + dist/fare-batch.csv
# ---------------------------------------------------------------------------

def leg(index, route, trip, board_stop, board_seq, alight_stop, alight_seq,
        rule, base, paid, source, charge, currency, expires, used, reasons):
    return {
        "leg_index": index, "route_id": route, "trip_id": trip,
        "board_stop_id": board_stop, "board_sequence": board_seq,
        "alight_stop_id": alight_stop, "alight_sequence": alight_seq,
        "matched_rule_id": rule, "base_product_id": base, "paid_product_id": paid,
        "source_leg_index": source, "charge_text": charge, "currency": currency,
        "ticket_expires_utc": expires, "used_legs_after": used,
        "purchase_reasons": reasons,
    }


# Product parameters as published by fare-v1, restated here for both the
# hand ledger and the independent mini state machine.
P_STD = {"product_id": "P_STD", "currency": "CNY", "amount_minor": 300,
         "validity_seconds": 3600, "max_legs": 2, "covers": {"P_STD"}}
P_LONG = {"product_id": "P_LONG", "currency": "CNY", "amount_minor": 500,
          "validity_seconds": 7200, "max_legs": 2, "covers": {"P_STD", "P_LONG"}}
P_T = {"product_id": "P_T", "currency": "CNY", "amount_minor": 200,
       "validity_seconds": 3600, "max_legs": 1, "covers": {"P_T"}}
P_FREE = {"product_id": "P_FREE", "currency": "CNY", "amount_minor": 0,
          "validity_seconds": 1800, "max_legs": 1, "covers": {"P_FREE"}}
P_JPY = {"product_id": "P_JPY", "currency": "JPY", "amount_minor": 250,
         "validity_seconds": 3600, "max_legs": 2, "covers": {"P_JPY"}}
HAND_PRODUCTS = {p["product_id"]: p for p in (P_STD, P_LONG, P_T, P_FREE, P_JPY)}

EXPECTED = {
    # J1: free middle leg does not extend expiry; leg 3 boards exactly at
    # the leg-1 expiry and has also spent the 2-segment quota.
    "J1": {"status": "settled", "currency": "CNY", "total_text": "6.00", "legs": [
        leg(1, "R1", "T_WKDY", "007", 10, "S2", 20, "r_any", "P_STD", "P_STD", 1,
            "3.00", "CNY", 1794646800, 1, ["first_purchase"]),
        leg(2, "R1", "T_LATE", "007", 1, "S2", 2, "r_any", "P_STD", "P_STD", 1,
            "0.00", "CNY", 1794646800, 2, []),
        leg(3, "R1", "T_WKND", "007", 1, "S2", 2, "r_any", "P_STD", "P_STD", 3,
            "3.00", "CNY", 1794650400, 1, ["expired", "quota_exhausted"]),
    ]},
    # J2: the 7200s window stays open; only the 2-segment quota runs out.
    "J2": {"status": "settled", "currency": "CNY", "total_text": "10.00", "legs": [
        leg(1, "R2", "T_FREQ_EXACT", "007", 1, "S2", 2, "r_r2", "P_LONG", "P_LONG", 1,
            "5.00", "CNY", 1794657200, 1, ["first_purchase"]),
        leg(2, "R2", "T_FREQ_EXACT", "007", 1, "S2", 2, "r_r2", "P_LONG", "P_LONG", 1,
            "0.00", "CNY", 1794657200, 2, []),
        leg(3, "R2", "T_FREQ_EXACT", "007", 1, "S2", 2, "r_r2", "P_LONG", "P_LONG", 3,
            "5.00", "CNY", 1794658400, 1, ["quota_exhausted"]),
    ]},
    # J3: P_LONG covers P_STD -> second leg free, expiry stays at leg 1's.
    "J3": {"status": "settled", "currency": "CNY", "total_text": "5.00", "legs": [
        leg(1, "R2", "T_FREQ_EXACT", "007", 1, "S2", 2, "r_r2", "P_LONG", "P_LONG", 1,
            "5.00", "CNY", 1794667200, 1, ["first_purchase"]),
        leg(2, "R1", "T_WKDY", "007", 10, "S2", 20, "r_any", "P_STD", "P_LONG", 1,
            "0.00", "CNY", 1794667200, 2, []),
    ]},
    # J3b: reverse direction is not covered -> rebuy (quota also spent).
    "J3b": {"status": "settled", "currency": "CNY", "total_text": "8.00", "legs": [
        leg(1, "R1", "T_WKDY", "007", 10, "S2", 20, "r_any", "P_STD", "P_STD", 1,
            "3.00", "CNY", 1794663600, 1, ["first_purchase"]),
        leg(2, "R2", "T_FREQ_EXACT", "007", 1, "S2", 2, "r_r2", "P_LONG", "P_LONG", 2,
            "5.00", "CNY", 1794667800, 1, ["product_not_covered"]),
    ]},
    # J4: one-segment P_T cannot cover the following P_STD.
    "J4": {"status": "settled", "currency": "CNY", "total_text": "5.00", "legs": [
        leg(1, "R1", "T_WKDY", "S2", 20, "S3", 30, "r_q", "P_T", "P_T", 1,
            "2.00", "CNY", 1794668600, 1, ["first_purchase"]),
        leg(2, "R1", "T_WKDY", "S3", 30, "S4", 40, "r_any", "P_STD", "P_STD", 2,
            "3.00", "CNY", 1794669200, 1, ["product_not_covered", "quota_exhausted"]),
    ]},
    # J5: valid 0.00 product, JPY-free CNY chain.
    "J5": {"status": "settled", "currency": "CNY", "total_text": "0.00", "legs": [
        leg(1, "R3", "T_FREQ_DEFAULT", "007", 1, "S2", 2, "r_free", "P_FREE", "P_FREE", 1,
            "0.00", "CNY", 1794671800, 1, ["first_purchase"]),
    ]},
    # J6: JPY zero-decimal money.
    "J6": {"status": "settled", "currency": "JPY", "total_text": "250", "legs": [
        leg(1, "R5", "T_ADJACENT", "007", 1, "S2", 2, "r_r5", "P_JPY", "P_JPY", 1,
            "250", "JPY", 1794674600, 1, ["first_purchase"]),
    ]},
    # J7: no rule on R3 ZB->ZC -> rejected, no partial total.
    "J7": {"status": "rejected", "reason_code": "no_rule", "leg_index": 1},
    # J8: CNY then JPY base products -> rejected without conversion/adding.
    "J8": {"status": "rejected", "reason_code": "mixed_currency"},
    # J9: higher-priority route rule overrides the wide fallback product.
    "J9": {"status": "settled", "currency": "CNY", "total_text": "5.00", "legs": [
        leg(1, "R4", "T_ID7", "7", 1, "S8", 2, "r_r4", "P_LONG", "P_LONG", 1,
            "5.00", "CNY", 1794682200, 1, ["first_purchase"]),
    ]},
}

# Raw batch legs for the independent mini state machine.
BATCH = {
    "J1": [(1, "P_STD", 1794643200), (2, "P_STD", 1794645000), (3, "P_STD", 1794646800)],
    "J2": [(1, "P_LONG", 1794650000), (2, "P_LONG", 1794650600), (3, "P_LONG", 1794651200)],
    "J3": [(1, "P_LONG", 1794660000), (2, "P_STD", 1794660600)],
    "J3b": [(1, "P_STD", 1794660000), (2, "P_LONG", 1794660600)],
    "J4": [(1, "P_T", 1794665000), (2, "P_STD", 1794665600)],
    "J5": [(1, "P_FREE", 1794670000)],
    "J6": [(1, "P_JPY", 1794671000)],
    "J9": [(1, "P_LONG", 1794675000)],
}


def independent_minor_ledger():
    """Tiny re-implementation of the contract ticket machine.

    It imports nothing from gtfs_store.fares and only knows the hand
    products above; the rule layer is taken from EXPECTED (base product per
    leg), so this isolates and re-checks the money/ticket logic.
    """
    ledger = {}
    for journey_id, legs in BATCH.items():
        active_pid = None
        bought_at = 0
        used = 0
        source = 0
        out = []
        for index, base_pid, board_utc in legs:
            if active_pid is None:
                reasons = ["first_purchase"]
                active_pid, bought_at, used, source = base_pid, board_utc, 1, index
                charge = HAND_PRODUCTS[base_pid]["amount_minor"]
            else:
                ticket = HAND_PRODUCTS[active_pid]
                not_covered = base_pid not in ticket["covers"]
                expired = not (board_utc < bought_at + ticket["validity_seconds"])
                no_quota = not (used < ticket["max_legs"])
                if not_covered or expired or no_quota:
                    reasons = []
                    if not_covered:
                        reasons.append("product_not_covered")
                    if expired:
                        reasons.append("expired")
                    if no_quota:
                        reasons.append("quota_exhausted")
                    active_pid, bought_at, used, source = base_pid, board_utc, 1, index
                    charge = HAND_PRODUCTS[base_pid]["amount_minor"]
                else:
                    used += 1
                    reasons, charge = [], 0
            ticket = HAND_PRODUCTS[active_pid]
            out.append({"leg_index": index, "paid_product_id": active_pid,
                        "charge_minor": charge,
                        "ticket_expires_utc": bought_at + ticket["validity_seconds"],
                        "used_legs_after": used, "purchase_reasons": reasons,
                        "source_leg_index": source})
        ledger[journey_id] = out
    return ledger


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fare-drill-"))
    db = tmp / "fare.db"
    store = Store(db)

    emit("0. Static edition under test", [
        "loading dist/demo-v1.zip as demo/v1 (the fare packages bind to it)"])
    static = store.load(FEED_KEY, EDITION, DIST / "demo-v1.zip")
    check("static edition ready", static["status"], "ready")
    fares = FareStore(store)

    emit("1. Install fare-v1 and the three whole-package rejections")
    install = fares.fare_install(str(DIST / "fare-v1.zip"))
    check("fare-v1 installs", install["status"], "ready")
    check("fare-v1 currencies", install["currencies"], ["CNY", "JPY"])

    for name, category in (("fare-reject-overlap.zip", "rule_overlap"),
                           ("fare-reject-amount.zip", "invalid_amount"),
                           ("fare-reject-dangling.zip", "foreign_key")):
        result = fares.fare_install(str(DIST / name))
        cats = {p["category"] for p in result["problems"]}
        check("%s rejected (%s)" % (name, category), result["status"], "rejected")
        check("%s reports %s" % (name, category), category in cats, True)
        if category == "rule_overlap":
            ctx = next(p.get("context") for p in result["problems"]
                       if p["category"] == "rule_overlap")
            print("  overlap witness ->", json.dumps(ctx, ensure_ascii=False))
            check("witness names both rules",
                  {ctx["rule_a"], ctx["rule_b"]}, {"r_any", "r_any_dup"})
            check("witness is a concrete route/zone/zone triple",
                  set(ctx["witness"]), {"route_id", "from_zone", "to_zone"})

    emit("2. Idempotent reinstall and version-content conflict")
    again = fares.fare_install(str(DIST / "fare-v1.zip"))
    check("same version+content is idempotent", again.get("resend"), True)
    try:
        fares.fare_install(str(DIST / "fare-v2.zip"),
                           # tamper: take v2 bytes but label the version as fare-v1
                           raw_bytes=_relabel_as_v1(DIST / "fare-v2.zip"))
        check("changed content under same version conflicts", "no error raised", "GtfsError")
    except GtfsError as exc:
        check("changed content under same version conflicts", "GtfsError: %s" % exc,
              "GtfsError: %s" % exc)

    emit("3. Install fare-v2 alongside v1; both remain independently queryable")
    v2 = fares.fare_install(str(DIST / "fare-v2.zip"))
    check("fare-v2 installs beside v1", v2["status"], "ready")
    packages = {r["fare_version"]: r["status"] for r in
                fares.fare_packages(FEED_KEY, EDITION) if r["status"] == "ready"}
    check("both ready versions present", packages,
          {"fare-v1": "ready", "fare-v2": "ready"})
    v1_std = next(p for p in fares.fare_products(FEED_KEY, EDITION, "fare-v1")
                  if p["product_id"] == "P_STD")
    v2_std = next(p for p in fares.fare_products(FEED_KEY, EDITION, "fare-v2")
                  if p["product_id"] == "P_STD")
    check("v1 P_STD still 3.00", v1_std["amount_text"], "3.00")
    check("v2 P_STD is 3.50", v2_std["amount_text"], "3.50")

    emit("4. Clear the shuffled batch under fare-v1 (per-leg hand ledger check)")
    result = fares.settle(FEED_KEY, EDITION, "fare-v1",
                          (DIST / "fare-batch.csv").read_bytes())
    check("batch row count (shuffled input accepted)", result["batch_rows"], 18)
    check("no batch-level parse problems", result["batch_problems"], [])
    by_id = {j["journey_id"]: j for j in result["journeys"]}

    compare_fields = ("leg_index", "route_id", "trip_id", "board_stop_id",
                      "board_sequence", "alight_stop_id", "alight_sequence",
                      "matched_rule_id", "base_product_id", "paid_product_id",
                      "source_leg_index", "charge_text", "ticket_expires_utc",
                      "used_legs_after", "purchase_reasons")
    for journey_id, expected in EXPECTED.items():
        actual = by_id[journey_id]
        check("%s status" % journey_id, actual["status"], expected["status"])
        if expected["status"] == "rejected":
            check("%s failure code" % journey_id,
                  actual["reasons"][0]["code"], expected["reason_code"])
            check("%s returns no partial total" % journey_id,
                  actual["total_text"], None)
            check("%s returns no partial legs" % journey_id, actual["legs"], [])
            continue
        check("%s currency" % journey_id, actual["currency"], expected["currency"])
        check("%s total" % journey_id, actual["total_text"], expected["total_text"])
        check("%s leg count" % journey_id, len(actual["legs"]), len(expected["legs"]))
        for actual_leg, expected_leg in zip(actual["legs"], expected["legs"]):
            for field_name in compare_fields:
                check("%s leg%d %s" % (journey_id, expected_leg["leg_index"], field_name),
                      actual_leg[field_name], expected_leg[field_name])
            check("%s leg%d charge currency" % (journey_id, expected_leg["leg_index"]),
                  actual_leg["base_product"]["currency"], expected_leg["currency"])

    emit("5. Independent mini ticket machine vs. engine money/state fields")
    mini = independent_minor_ledger()
    for journey_id, mini_legs in mini.items():
        engine_legs = by_id[journey_id]["legs"]
        for mini_leg, engine_leg in zip(mini_legs, engine_legs):
            for field_name in ("paid_product_id", "charge_minor", "ticket_expires_utc",
                               "used_legs_after", "purchase_reasons",
                               "source_leg_index"):
                check("mini %s leg%d %s" % (journey_id, mini_leg["leg_index"], field_name),
                      mini_leg[field_name], engine_leg[field_name])

    emit("6. Hand totals recomputed from the per-leg charges")
    for journey_id, expected in EXPECTED.items():
        if expected["status"] != "settled":
            continue
        hand_sum = sum(int(l["charge_text"].replace(".", ""))
                       if expected["currency"] == "JPY"
                       else int(round(float(l["charge_text"]) * 100))
                       for l in expected["legs"])
        check("%s hand-summed total matches" % journey_id,
              by_id[journey_id]["total_minor"], hand_sum)

    emit("7. fare-v2 recomputes the same batch differently; v1 is untouched")
    result_v2 = fares.settle(FEED_KEY, EDITION, "fare-v2",
                             (DIST / "fare-batch.csv").read_bytes())
    v2_by_id = {j["journey_id"]: j for j in result_v2["journeys"]}
    # Under v2, r_any on R1 sells P_LONG: J1 leg 1 now costs 5.00 and leg 2
    # rides the long ticket for free; leg 3 still rebuys at the 7200s window?
    # No: leg 3 at 09:00 is inside 2h, but the 2-leg quota is spent -> 5.00.
    check("v2 J1 total changes (10.00 expected)", v2_by_id["J1"]["total_text"], "10.00")
    check("v1 J1 total stays 6.00", by_id["J1"]["total_text"], "6.00")
    check("v2 content hash differs from v1",
          result_v2["content_sha256"] != result["content_sha256"], True)

    emit("8. Reopen the database: historical fare-v1 reproduces the same detail")
    store.close()
    reopened = Store(db)
    fares2 = FareStore(reopened)
    re_result = fares2.settle(FEED_KEY, EDITION, "fare-v1",
                              (DIST / "fare-batch.csv").read_bytes())
    check("reopened settle is byte-identical in JSON",
          json.dumps(re_result, sort_keys=True),
          json.dumps(result, sort_keys=True))
    check("rejection ledger survives reopen",
          len(fares2.fare_rejections(FEED_KEY, EDITION)) >= 4, True)
    reopened.close()

    emit("9. Static and realtime entry points still work after fare install")
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
         "--json", "list"], capture_output=True, text=True, check=True)
    listing = json.loads(proc.stdout)
    check("static list still serves demo/v1",
          [(r["feed_key"], r["edition"], r["status"]) for r in listing],
          [("demo", "v1", "ready")])
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
         "fare-packages"], capture_output=True, text=True, check=True)
    check("fare-packages CLI lists versions", "fare-v1" in proc.stdout
          and "fare-v2" in proc.stdout, True)

    print()
    if FAILURES:
        print("FARE DRILL FAILED: %d mismatch(es)" % len(FAILURES))
        for label in FAILURES:
            print(" -", label)
        return 1
    print("all fare drill checks passed; database left at", db)
    return 0


def _relabel_as_v1(path: Path) -> bytes:
    """Repack fare-v2 contents with fare_version rewritten to fare-v1."""
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["fare_version"] = "fare-v1"
                data = (json.dumps(manifest, indent=2) + "\n").encode()
            dst.writestr(info.filename, data)
    return out.getvalue()


if __name__ == "__main__":
    sys.exit(main())
