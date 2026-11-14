"""Generate the rollup demo fixtures under dist/rollup/.

Everything here is hand-written input material: the configs, the JSONL fact
files and -- most importantly -- the expected metrics.  The expected numbers
are literal hand-computed values in this source file; they are NOT produced
by running the rollup engine, so the walkthrough and the unit tests check
the engine against independent arithmetic.

Cases produced:

* manual-*    the worked example from the task: L splits 1/2+1/2 into A/B,
              A->X, B->X(1/2)+Y(1/2), so L reaches X with 3/4 and Y with 1/4;
              two L facts (vehicle v, amounts 10 and 20) plus one fact with a
              null first dimension to prove root cells differ from ALL cells.
* stress-*    both dimensions fork, paths converge, one vehicle spans leaves,
              two byte-identical fact lines, null vs empty-string vehicles,
              all-null amounts, negative amounts cancelling to an exact zero,
              >64-bit amounts, and the legal member ids "ALL" and "007".
* bad-*       configs/facts that must be refused with a non-zero exit.

Run:  python3 -I tools/build_rollup_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "rollup"


def _frac(num: int, den: int = 1) -> dict:
    return {"num": str(num), "den": str(den)}


def _metrics(row_w, val_w, amount_sum, amount_mean, vehicles) -> dict:
    return {
        "row_weight": _frac(*row_w),
        "value_weight": _frac(*val_w),
        "amount_sum": None if amount_sum is None else _frac(*amount_sum),
        "amount_mean": None if amount_mean is None else _frac(*amount_mean),
        "vehicle_count": vehicles,
    }


def _coord(kind: str, value: str | None = None) -> dict:
    return {"kind": kind, "value": value}


# ---------------------------------------------------------------------------
# Manual example
# ---------------------------------------------------------------------------

MANUAL_CONFIG = {
    "dimensions": {
        "line_cat": {
            "levels": [
                {"name": "line", "members": ["L"]},
                {"name": "category", "members": ["A", "B"]},
                {"name": "trunk", "members": ["X", "Y"]},
                {"name": "root", "members": ["R"]},
            ],
            "links": [
                {"from": "line", "to": "category", "edges": [
                    {"child": "L", "parent": "A", "numerator": 1, "denominator": 2},
                    {"child": "L", "parent": "B", "numerator": 1, "denominator": 2},
                ]},
                {"from": "category", "to": "trunk", "edges": [
                    {"child": "A", "parent": "X", "numerator": 1, "denominator": 1},
                    {"child": "B", "parent": "X", "numerator": 1, "denominator": 2},
                    {"child": "B", "parent": "Y", "numerator": 1, "denominator": 2},
                ]},
                {"from": "trunk", "to": "root", "edges": [
                    {"child": "X", "parent": "R", "numerator": 1, "denominator": 1},
                    {"child": "Y", "parent": "R", "numerator": 1, "denominator": 1},
                ]},
            ],
        },
        "depot": {
            "levels": [
                {"name": "depot_leaf", "members": ["D1"]},
                {"name": "depot_root", "members": ["DALL"]},
            ],
            "links": [
                {"from": "depot_leaf", "to": "depot_root", "edges": [
                    {"child": "D1", "parent": "DALL", "numerator": 1, "denominator": 1},
                ]},
            ],
        },
    },
    "fields": {
        "dimensions": {"line_cat": "line", "depot": "depot"},
        "vehicle": "vehicle_unit",
        "amount": "amount",
    },
    "grains": [
        {"name": "trunk_by_depot", "levels": {"line_cat": "trunk", "depot": "depot_leaf"}},
        {"name": "category_by_depot", "levels": {"line_cat": "category", "depot": "depot_leaf"}},
        {"name": "root_totals", "levels": {"line_cat": "root", "depot": "depot_root"}},
        {"name": "grand_total", "levels": {"line_cat": "ALL", "depot": "ALL"}},
    ],
}

MANUAL_FACTS = [
    {"line": "L", "depot": "D1", "vehicle_unit": "v", "amount": 10},
    {"line": "L", "depot": "D1", "vehicle_unit": "v", "amount": 20},
    {"line": None, "depot": "D1", "vehicle_unit": "w", "amount": 7},
]

# Hand-computed.  L reaches X with 1/2*1 + 1/2*1/2 = 3/4 and Y with 1/2*1/2 =
# 1/4; the null-dimension fact stays null at every level and never joins R.
MANUAL_EXPECTED = {
    "grains": {
        "trunk_by_depot": [
            {"coords": {"line_cat": _coord("member", "X"), "depot": _coord("member", "D1")},
             "metrics": _metrics((3, 2), (3, 2), (45, 2), (15, 1), 1)},
            {"coords": {"line_cat": _coord("member", "Y"), "depot": _coord("member", "D1")},
             "metrics": _metrics((1, 2), (1, 2), (15, 2), (15, 1), 1)},
            {"coords": {"line_cat": _coord("null"), "depot": _coord("member", "D1")},
             "metrics": _metrics((1, 1), (1, 1), (7, 1), (7, 1), 1)},
        ],
        "category_by_depot": [
            {"coords": {"line_cat": _coord("member", "A"), "depot": _coord("member", "D1")},
             "metrics": _metrics((1, 1), (1, 1), (15, 1), (15, 1), 1)},
            {"coords": {"line_cat": _coord("member", "B"), "depot": _coord("member", "D1")},
             "metrics": _metrics((1, 1), (1, 1), (15, 1), (15, 1), 1)},
            {"coords": {"line_cat": _coord("null"), "depot": _coord("member", "D1")},
             "metrics": _metrics((1, 1), (1, 1), (7, 1), (7, 1), 1)},
        ],
        "root_totals": [
            {"coords": {"line_cat": _coord("member", "R"), "depot": _coord("member", "DALL")},
             "metrics": _metrics((2, 1), (2, 1), (30, 1), (15, 1), 1)},
            {"coords": {"line_cat": _coord("null"), "depot": _coord("member", "DALL")},
             "metrics": _metrics((1, 1), (1, 1), (7, 1), (7, 1), 1)},
        ],
        "grand_total": [
            {"coords": {"line_cat": _coord("all"), "depot": _coord("all")},
             "metrics": _metrics((3, 1), (3, 1), (37, 1), (37, 3), 2)},
        ],
    },
    "drill": {
        "grain": "trunk_by_depot",
        "d1": _coord("member", "X"),
        "d2": _coord("member", "D1"),
        "total_contributions": 2,
        "rows": [
            {"line_no": 1, "vehicle_unit": "v", "amount": 10,
             "coefficient": _frac(3, 4)},
            {"line_no": 2, "vehicle_unit": "v", "amount": 20,
             "coefficient": _frac(3, 4)},
        ],
    },
}


# ---------------------------------------------------------------------------
# Stress example
# ---------------------------------------------------------------------------

BIG = 9223372036854775809  # 2**63 + 1, does not fit a signed 64-bit integer

STRESS_CONFIG = {
    "dimensions": {
        "line": {
            "levels": [
                {"name": "line_leaf", "members": ["L1", "L2", "007"]},
                {"name": "category", "members": ["A", "B"]},
                {"name": "trunk", "members": ["X", "Y"]},
                {"name": "root", "members": ["R"]},
            ],
            "links": [
                {"from": "line_leaf", "to": "category", "edges": [
                    {"child": "L1", "parent": "A", "numerator": 1, "denominator": 2},
                    {"child": "L1", "parent": "B", "numerator": 1, "denominator": 2},
                    {"child": "L2", "parent": "A", "numerator": 1, "denominator": 3},
                    {"child": "L2", "parent": "B", "numerator": 2, "denominator": 3},
                    {"child": "007", "parent": "B", "numerator": 1, "denominator": 1},
                ]},
                {"from": "category", "to": "trunk", "edges": [
                    {"child": "A", "parent": "X", "numerator": 1, "denominator": 1},
                    {"child": "B", "parent": "X", "numerator": 1, "denominator": 2},
                    {"child": "B", "parent": "Y", "numerator": 1, "denominator": 2},
                ]},
                {"from": "trunk", "to": "root", "edges": [
                    {"child": "X", "parent": "R", "numerator": 1, "denominator": 1},
                    {"child": "Y", "parent": "R", "numerator": 1, "denominator": 1},
                ]},
            ],
        },
        "depot": {
            "levels": [
                {"name": "depot_leaf", "members": ["D1", "D2"]},
                {"name": "region", "members": ["ALL", "E"]},
                {"name": "depot_root", "members": ["RD"]},
            ],
            "links": [
                {"from": "depot_leaf", "to": "region", "edges": [
                    {"child": "D1", "parent": "ALL", "numerator": 1, "denominator": 2},
                    {"child": "D1", "parent": "E", "numerator": 1, "denominator": 2},
                    {"child": "D2", "parent": "ALL", "numerator": 1, "denominator": 1},
                ]},
                {"from": "region", "to": "depot_root", "edges": [
                    {"child": "ALL", "parent": "RD", "numerator": 1, "denominator": 1},
                    {"child": "E", "parent": "RD", "numerator": 1, "denominator": 1},
                ]},
            ],
        },
    },
    "fields": {
        "dimensions": {"line": "line_code", "depot": "depot_code"},
        "vehicle": "vehicle_unit",
        "amount": "amount",
    },
    "grains": [
        {"name": "leaf_by_leaf", "levels": {"line": "line_leaf", "depot": "depot_leaf"}},
        {"name": "trunk_by_region", "levels": {"line": "trunk", "depot": "region"}},
        {"name": "root_root", "levels": {"line": "root", "depot": "depot_root"}},
        {"name": "trunk_all", "levels": {"line": "trunk", "depot": "ALL"}},
        {"name": "all_all", "levels": {"line": "ALL", "depot": "ALL"}},
    ],
}

STRESS_FACTS = [
    # 1-2: same vehicle, same leaf pair
    {"line_code": "L1", "depot_code": "D1", "vehicle_unit": "v", "amount": 10},
    {"line_code": "L1", "depot_code": "D1", "vehicle_unit": "v", "amount": 20},
    # 3-4: same vehicle on a second leaf; amounts cancel exactly
    {"line_code": "L2", "depot_code": "D2", "vehicle_unit": "v", "amount": -30},
    {"line_code": "L2", "depot_code": "D2", "vehicle_unit": "v", "amount": 30},
    # 5-6: two byte-identical lines are still two facts; empty-string vehicle;
    # amount exceeds int64; leaf id "007"
    {"line_code": "007", "depot_code": "D1", "vehicle_unit": "", "amount": BIG},
    {"line_code": "007", "depot_code": "D1", "vehicle_unit": "", "amount": BIG},
    # 7: unclassified line, no vehicle, no amount
    {"line_code": None, "depot_code": "D1", "vehicle_unit": None, "amount": None},
    # 8: unclassified depot, no amount, but a countable vehicle
    {"line_code": "L1", "depot_code": None, "vehicle_unit": "u2", "amount": None},
    # 9: no vehicle, ordinary amount
    {"line_code": "L1", "depot_code": "D1", "vehicle_unit": None, "amount": 5},
]

# Hand-computed highlight cells.  Full per-cell verification of every grain
# happens in tests/test_rollup.py and tools/rollup_walkthrough.py through an
# independent brute-force enumerator; these literals pin the tricky cases.
STRESS_EXPECTED = {
    "trunk_by_region": {
        # negative amounts cancel to an exact zero inside this cell
        "X|member:ALL": {
            "coords": {"line": _coord("member", "X"), "depot": _coord("member", "ALL")},
            "metrics": _metrics((71, 24), (71, 24),
                                (105 + 4 * BIG, 8),
                                (3 * (105 + 4 * BIG), 71), 2),
        },
        # only null amounts reach this cell: sum/mean stay null, weight 0
        # (fact 7's depot D1 reaches region ALL with coefficient 1/2)
        "null|member:ALL": {
            "coords": {"line": _coord("null"), "depot": _coord("member", "ALL")},
            "metrics": _metrics((1, 2), (0, 1), None, None, 0),
        },
        # null depot coordinate, vehicle u2 counted without any coefficient
        "X|null": {
            "coords": {"line": _coord("member", "X"), "depot": _coord("null")},
            "metrics": _metrics((3, 4), (0, 1), None, None, 1),
        },
    },
    "leaf_by_leaf": {
        # exact cancellation: sum is 0/1, not null
        "L2|D2": {
            "coords": {"line": _coord("member", "L2"), "depot": _coord("member", "D2")},
            "metrics": _metrics((2, 1), (2, 1), (0, 1), (0, 1), 1),
        },
        # two identical lines are two facts; "" is a real vehicle id
        "007|D1": {
            "coords": {"line": _coord("member", "007"), "depot": _coord("member", "D1")},
            "metrics": _metrics((2, 1), (2, 1), (2 * BIG, 1), (BIG, 1), 1),
        },
    },
    "root_root": {
        "R|RD": {
            "coords": {"line": _coord("member", "R"), "depot": _coord("member", "RD")},
            "metrics": _metrics((7, 1), (7, 1), (35 + 2 * BIG, 1),
                                (35 + 2 * BIG, 7), 2),
        },
    },
    "all_all": {
        "all|all": {
            "coords": {"line": _coord("all"), "depot": _coord("all")},
            "metrics": _metrics((9, 1), (7, 1), (35 + 2 * BIG, 1),
                                (35 + 2 * BIG, 7), 3),
        },
    },
}


# ---------------------------------------------------------------------------
# Bad inputs (each must fail the build with a non-zero exit)
# ---------------------------------------------------------------------------

def _manual_config_mutated(mutate) -> dict:
    config = json.loads(json.dumps(MANUAL_CONFIG))
    mutate(config)
    return config


BAD_SUM_CONFIG = _manual_config_mutated(
    lambda c: c["dimensions"]["line_cat"]["links"][0]["edges"].pop())
# L now only reaches A with 1/2; outgoing proportions sum to 1/2, not 1.

BAD_REF_CONFIG = _manual_config_mutated(
    lambda c: c["dimensions"]["line_cat"]["links"][0]["edges"].append(
        {"child": "L", "parent": "Q", "numerator": 1, "denominator": 2}))
# Q is not a member of the category level.

BAD_DUP_GRAIN_CONFIG = _manual_config_mutated(
    lambda c: c["grains"].append(
        {"name": "trunk_by_depot_again",
         "levels": {"depot": "depot_leaf", "line_cat": "trunk"}}))
# Same level combination as trunk_by_depot, dimensions listed in the other
# order; key order must not make a new grain.

BAD_TAIL_FACTS = [
    {"line": "L", "depot": "D1", "vehicle_unit": "v", "amount": 10},
    {"line": "L", "depot": "D1", "vehicle_unit": "v", "amount": 20},
    {"line": "L", "depot": "D1", "vehicle_unit": "v", "amount": 10.5},
]
# The bad line sits at the very tail of the file (amount is a decimal).


# ---------------------------------------------------------------------------

def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _write_json(OUT / "manual-config.json", MANUAL_CONFIG)
    _write_jsonl(OUT / "manual-facts.jsonl", MANUAL_FACTS)
    _write_json(OUT / "manual-expected.json", MANUAL_EXPECTED)
    _write_json(OUT / "stress-config.json", STRESS_CONFIG)
    _write_jsonl(OUT / "stress-facts.jsonl", STRESS_FACTS)
    _write_json(OUT / "stress-expected.json", STRESS_EXPECTED)
    _write_json(OUT / "bad-sum-config.json", BAD_SUM_CONFIG)
    _write_json(OUT / "bad-ref-config.json", BAD_REF_CONFIG)
    _write_json(OUT / "bad-dup-grain-config.json", BAD_DUP_GRAIN_CONFIG)
    _write_jsonl(OUT / "bad-tail-facts.jsonl", BAD_TAIL_FACTS)
    print("wrote rollup fixtures to %s" % OUT)
    for path in sorted(OUT.iterdir()):
        print("  %s (%d bytes)" % (path.name, path.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
