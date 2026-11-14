"""Build the hand-computed fare rule packages and clearing batch fixtures.

Run from the repository root:  python3 -I tools/build_fare_fixtures.py

The packages bind to dist/demo-v1.zip's static feed (feed_key="demo",
edition="v1").  Every amount, validity window and expected settlement below
is chosen so a human can recompute the clearing with pencil and paper; see
tools/fare_walkthrough.py for the independently written expected ledger.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FARE_FIX = ROOT / "fixtures" / "fares"
DIST = ROOT / "dist"

FEED_KEY = "demo"
EDITION = "v1"

# ---------------------------------------------------------------------------
# stop -> single fare zone (every stop appears at most once)
# ---------------------------------------------------------------------------

STOP_ZONES = [
    {"stop_id": "007", "zone_id": "ZA"},
    {"stop_id": "7", "zone_id": "ZD"},
    {"stop_id": "S2", "zone_id": "ZB"},
    {"stop_id": "S3", "zone_id": "ZC"},
    {"stop_id": "S4", "zone_id": "ZC"},
    {"stop_id": "S5", "zone_id": "ZA"},
    {"stop_id": "S6", "zone_id": "ZB"},
    {"stop_id": "S8", "zone_id": "ZE"},
]

# ---------------------------------------------------------------------------
# fare-v1: CNY products plus one JPY product; all amounts are fixed-point
# ---------------------------------------------------------------------------

PRODUCTS_V1 = [
    # standard transfer ticket: 3 yuan, one hour, two segments, covers itself
    {"product_id": "P_STD", "amount": "3.00", "currency": "CNY",
     "validity_seconds": 3600, "max_legs": 2, "covers": ["P_STD"]},
    # long ticket: 5 yuan, two hours, two segments, covers itself + standard
    {"product_id": "P_LONG", "amount": "5.00", "currency": "CNY",
     "validity_seconds": 7200, "max_legs": 2, "covers": ["P_STD", "P_LONG"]},
    # one-segment ticket: 2 yuan, one hour, one segment, standard only
    {"product_id": "P_T", "amount": "2.00", "currency": "CNY",
     "validity_seconds": 3600, "max_legs": 1, "covers": ["P_T"]},
    # free product is a valid product: 0.00, short window, one segment
    {"product_id": "P_FREE", "amount": "0.00", "currency": "CNY",
     "validity_seconds": 1800, "max_legs": 1, "covers": ["P_FREE"]},
    # JPY has zero decimals; 250 yen, one hour, two segments
    {"product_id": "P_JPY", "amount": "250", "currency": "JPY",
     "validity_seconds": 3600, "max_legs": 2, "covers": ["P_JPY"]},
]

RULES_V1 = [
    # wide rule at the lowest priority.  It is route-scoped but leaves both
    # zone conditions null, so it is strictly wider than the zone-scoped
    # rules below; every R1/R2 leg starts by matching it.
    {"rule_id": "r_any", "priority": 1, "route_id": "R1",
     "from_zone": None, "to_zone": None, "product_id": "P_STD"},
    # route-specific overrides beat r_any on R2/R4/R5
    {"rule_id": "r_r2", "priority": 10, "route_id": "R2",
     "from_zone": None, "to_zone": None, "product_id": "P_LONG"},
    {"rule_id": "r_r4", "priority": 10, "route_id": "R4",
     "from_zone": None, "to_zone": None, "product_id": "P_LONG"},
    # zone+route scoped one-segment product on R1 ZB->ZC
    {"rule_id": "r_q", "priority": 15, "route_id": "R1",
     "from_zone": "ZB", "to_zone": "ZC", "product_id": "P_T"},
    {"rule_id": "r_r5", "priority": 20, "route_id": "R5",
     "from_zone": None, "to_zone": None, "product_id": "P_JPY"},
    # free product, narrowly scoped
    {"rule_id": "r_free", "priority": 20, "route_id": "R3",
     "from_zone": "ZA", "to_zone": "ZB", "product_id": "P_FREE"},
    # NOTE: R3 ZB -> ZC has no rule at all (r_any only covers R1), so the
    # no-rule journey fails instead of silently matching the catch-all.
]

# fare-v2 keeps the same identity keys but changes contents: P_STD costs
# 3.50 and r_any sells the long ticket.  Recomputing the same batch under v2
# must give a different, still deterministic ledger; v1 stays intact.
PRODUCTS_V2 = [
    dict(PRODUCTS_V1[0], amount="3.50"),
    *PRODUCTS_V1[1:],
]
RULES_V2 = [
    dict(RULES_V1[0], product_id="P_LONG"),
    *RULES_V1[1:],
]

# ---------------------------------------------------------------------------
# Reject fixtures, each broken in exactly one documented way
# ---------------------------------------------------------------------------

# Two different spellings of overlapping conditions at the same priority:
# r_any matches R1 with null zones; this rule pins one zone and leaves the
# other null, so both can fire together on R1 ZB->ZC.
OVERLAP_RULE = {"rule_id": "r_any_dup", "priority": 1, "route_id": "R1",
                "from_zone": "ZB", "to_zone": None, "product_id": "P_STD"}

# JPY forbids decimals (zero-decimal currency).
BAD_AMOUNT_PRODUCTS = [
    dict(PRODUCTS_V1[4], amount="250.0"),
    *PRODUCTS_V1[:4],
]

# A coverage reference to a product that does not exist.
DANGLING_PRODUCTS = [
    *PRODUCTS_V1,
    {"product_id": "P_GHOST", "amount": "1.00", "currency": "CNY",
     "validity_seconds": 600, "max_legs": 1,
     "covers": ["P_GHOST", "P_DOES_NOT_EXIST"]},
]


def write_fare_zip(path: Path, *, feed_key: str, edition: str, fare_version: str,
                   stop_zones: list[dict], products: list[dict],
                   rules: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest.json": {
            "feed_key": feed_key, "edition": edition, "fare_version": fare_version,
        },
        "stop_zones.json": stop_zones,
        "products.json": products,
        "rules.json": rules,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("manifest.json", "stop_zones.json", "products.json", "rules.json"):
            zf.writestr(name, json.dumps(payload[name], ensure_ascii=False, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# The clearing batch.  Rows are deliberately shuffled and use the
# non-consecutive stop_sequences of T_WKDY (10/20/30/40) and stop id "007".
# Epoch base: 2026-11-14T08:00:00Z = 1794643200
# ---------------------------------------------------------------------------

BATCH_ROWS = [
    # J1: three legs whose base product is always P_STD (r_any / r_r1-free
    # routes).  P_STD: 3.00, 3600s, max_legs 2, covers only itself.
    # Leg 2 is a free transfer (coverage + window + quota all hold) and does
    # NOT extend the leg-1 expiry.  Leg 3 boards exactly at that expiry with
    # the quota spent too, so it rebuys and both reasons are listed.
    ("J1", 2, "T_LATE", 1, 2, 1794645000, 1794645400),    # leg2 08:30 < 09:00, free, used_legs=2
    ("J1", 1, "T_WKDY", 10, 20, 1794643200, 1794643600),  # leg1 007(10)->S2(20), buy@08:00
    ("J1", 3, "T_WKND", 1, 2, 1794646800, 1794647400),    # leg3 boards 09:00 == expiry -> rebuy
    # J2: time still valid but the two-segment quota is exhausted on leg 3.
    # P_LONG: 5.00, validity 7200 (still open all journey), max_legs 2.
    ("J2", 1, "T_FREQ_EXACT", 1, 2, 1794650000, 1794650400),  # buy P_LONG@09:53:20
    ("J2", 2, "T_FREQ_EXACT", 1, 2, 1794650600, 1794650900),  # free, used_legs -> 2
    ("J2", 3, "T_FREQ_EXACT", 1, 2, 1794651200, 1794651600),  # quota exhausted, time open -> buy
    # J3: cross-product coverage one direction: P_LONG covers P_STD.
    ("J3", 2, "T_WKDY", 10, 20, 1794660600, 1794661000),      # r_any P_STD covered -> free
    ("J3", 1, "T_FREQ_EXACT", 1, 2, 1794660000, 1794660400),  # r_r2 buy P_LONG
    # J3b: reverse direction: P_STD does not cover P_LONG -> rebuy.
    ("J3b", 2, "T_FREQ_EXACT", 1, 2, 1794660600, 1794661000),  # r_r2 P_LONG not covered -> rebuy
    ("J3b", 1, "T_WKDY", 10, 20, 1794660000, 1794660400),      # r_any buy P_STD
    # J4: r_q on R1 ZB->ZC sells the one-segment P_T; the next leg cannot
    # ride it (max_legs 1), exercising the quota path on a second product.
    ("J4", 1, "T_WKDY", 20, 30, 1794665000, 1794665400),      # r_q buy P_T
    ("J4", 2, "T_WKDY", 30, 40, 1794665600, 1794666000),      # r_any P_STD, P_T cannot cover -> buy
    # J5: 0-yuan product, single leg (narrow r_free rule on R3).
    ("J5", 1, "T_FREQ_DEFAULT", 1, 2, 1794670000, 1794670400),  # ZA->ZB r_free P_FREE 0.00
    # J6: JPY amount with zero decimals.
    ("J6", 1, "T_ADJACENT", 1, 2, 1794671000, 1794671400),   # R5 r_r5 -> P_JPY 250
    # J7: no matching rule on R3 ZB->ZC -> whole journey rejected, no total.
    ("J7", 1, "T_FREQ_WINDOW", 2, 3, 1794672000, 1794672400),
    # J8: mixed currency: CNY leg then JPY leg -> whole journey rejected.
    ("J8", 1, "T_WKDY", 10, 20, 1794673000, 1794673400),     # r_any P_STD CNY
    ("J8", 2, "T_ADJACENT", 1, 2, 1794673600, 1794674000),   # r_r5 P_JPY JPY
    # J9: a higher-priority route rule r_r4 wins over what would otherwise be
    # the r_any-style fallback; the leg pays P_LONG, not P_STD.
    ("J9", 1, "T_ID7", 1, 2, 1794675000, 1794675400),        # r_r4 P_LONG
]


def write_batch(path: Path, rows: list[tuple]) -> Path:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["journey_id", "leg_index", "trip_id", "board_sequence",
                     "alight_sequence", "board_utc", "alight_utc"])
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue().encode("utf-8"))
    return path


def main() -> None:
    write_fare_zip(DIST / "fare-v1.zip", feed_key=FEED_KEY, edition=EDITION,
                   fare_version="fare-v1", stop_zones=STOP_ZONES,
                   products=PRODUCTS_V1, rules=RULES_V1)
    write_fare_zip(DIST / "fare-v2.zip", feed_key=FEED_KEY, edition=EDITION,
                   fare_version="fare-v2", stop_zones=STOP_ZONES,
                   products=PRODUCTS_V2, rules=RULES_V2)
    write_fare_zip(DIST / "fare-reject-overlap.zip", feed_key=FEED_KEY, edition=EDITION,
                   fare_version="fare-bad-overlap", stop_zones=STOP_ZONES,
                   products=PRODUCTS_V1, rules=RULES_V1 + [OVERLAP_RULE])
    write_fare_zip(DIST / "fare-reject-amount.zip", feed_key=FEED_KEY, edition=EDITION,
                   fare_version="fare-bad-amount", stop_zones=STOP_ZONES,
                   products=BAD_AMOUNT_PRODUCTS, rules=RULES_V1)
    write_fare_zip(DIST / "fare-reject-dangling.zip", feed_key=FEED_KEY, edition=EDITION,
                   fare_version="fare-bad-dangling", stop_zones=STOP_ZONES,
                   products=DANGLING_PRODUCTS, rules=RULES_V1)
    # Also keep the unzipped source fixtures for inspection.
    for name, products, rules, version in (
            ("fare-v1", PRODUCTS_V1, RULES_V1, "fare-v1"),
            ("fare-v2", PRODUCTS_V2, RULES_V2, "fare-v2")):
        d = FARE_FIX / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps({"feed_key": FEED_KEY, "edition": EDITION,
                        "fare_version": version}, indent=2) + "\n")
        (d / "stop_zones.json").write_text(json.dumps(STOP_ZONES, indent=2) + "\n")
        (d / "products.json").write_text(json.dumps(products, indent=2) + "\n")
        (d / "rules.json").write_text(json.dumps(rules, indent=2) + "\n")
    write_batch(DIST / "fare-batch.csv", BATCH_ROWS)
    print("fare fixtures written to", DIST)


if __name__ == "__main__":
    main()
