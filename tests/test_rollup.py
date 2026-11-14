"""Tests for gtfs_store.rollup (weighted two-dimension hierarchical rollup).

The reference implementation in this file (reference_rollup) is a deliberate
brute-force enumerator: it walks every raw root path of every leaf, multiplies
edge fractions along each path and only then sums.  It imports nothing from
gtfs_store.rollup, so the engine's folded coefficient maps are checked against
genuinely independent arithmetic.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.cli import main as cli_main  # noqa: E402
from gtfs_store.rollup import (  # noqa: E402
    Coord,
    RollupError,
    RollupResult,
    build,
)

DIST = ROOT / "dist" / "rollup"


def setUpModule() -> None:
    if not (DIST / "manual-config.json").is_file():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "build_rollup_fixtures.py")],
                       check=True, capture_output=True, cwd=str(ROOT))


# ---------------------------------------------------------------------------
# Independent reference implementation (no imports from gtfs_store.rollup)
# ---------------------------------------------------------------------------

def _ref_paths(dim_spec: dict, leaf: str, target_level: str) -> dict[str, Fraction]:
    """Every raw path leaf->root, multiplied, summed per target member."""
    levels = [lvl["name"] for lvl in dim_spec["levels"]]
    adj: dict[str, dict[str, list[tuple[str, Fraction]]]] = {}
    for link in dim_spec["links"]:
        per_child: dict[str, list[tuple[str, Fraction]]] = defaultdict(list)
        for edge in link["edges"]:
            per_child[edge["child"]].append(
                (edge["parent"], Fraction(edge["numerator"], edge["denominator"])))
        adj[link["from"]] = dict(per_child)
    out: dict[str, Fraction] = defaultdict(Fraction)

    def visit(member: str, level_idx: int, acc: Fraction) -> None:
        if levels[level_idx] == target_level:
            out[member] += acc
            return
        for parent, frac in adj.get(levels[level_idx], {}).get(member, []):
            visit(parent, level_idx + 1, acc * frac)

    visit(leaf, 0, Fraction(1))
    return dict(out)


def _ref_targets(dim_spec: dict, selector: str, value) -> dict[tuple, Fraction]:
    if selector == "ALL":
        return {("all", None): Fraction(1)}
    if value is None:
        return {("null", None): Fraction(1)}
    return {("member", member): coeff
            for member, coeff in _ref_paths(dim_spec, value, selector).items()}


def reference_rollup(config: dict, facts: list[dict]) -> dict:
    """Brute-force per-grain cells: {grain: {(k1,v1,k2,v2): cell}}."""
    dim_names = list(config["dimensions"].keys())
    dim_specs = config["dimensions"]
    fields = config["fields"]
    grains = {}
    for grain in config["grains"]:
        cells: dict[tuple, dict] = {}
        for line_no, fact in enumerate(facts, start=1):
            t1 = _ref_targets(dim_specs[dim_names[0]], grain["levels"][dim_names[0]],
                              fact[fields["dimensions"][dim_names[0]]])
            t2 = _ref_targets(dim_specs[dim_names[1]], grain["levels"][dim_names[1]],
                              fact[fields["dimensions"][dim_names[1]]])
            amount = fact[fields["amount"]]
            vehicle = fact[fields["vehicle"]]
            for (k1, v1), c1 in t1.items():
                for (k2, v2), c2 in t2.items():
                    key = (k1, v1, k2, v2)
                    cell = cells.setdefault(key, {
                        "row_w": Fraction(0), "val_w": Fraction(0),
                        "amount_sum": Fraction(0), "has_amount": False,
                        "vehicles": set(), "lines": defaultdict(Fraction)})
                    coeff = c1 * c2
                    cell["row_w"] += coeff
                    if amount is not None:
                        cell["has_amount"] = True
                        cell["val_w"] += coeff
                        cell["amount_sum"] += amount * coeff
                    if vehicle is not None:
                        cell["vehicles"].add(vehicle)
                    cell["lines"][line_no] += coeff
        grains[grain["name"]] = cells
    return grains


def _frac(obj: dict) -> Fraction:
    return Fraction(int(obj["num"]), int(obj["den"]))


def _engine_cells(result: RollupResult, grain: str) -> dict:
    dims = (result.dim1_name, result.dim2_name)
    cells = {}
    for cell in result.cells(grain):
        key = tuple((cell["coords"][d]["kind"], cell["coords"][d].get("value"))
                    for d in dims)
        key = (key[0][0], key[0][1], key[1][0], key[1][1])
        cells[key] = cell
    return cells


def assert_cells_match(test: unittest.TestCase, result: RollupResult,
                       reference: dict) -> None:
    """Every grain, every cell: metrics, drill ledger and drill re-sums."""
    for grain_name, ref_cells in reference.items():
        engine = _engine_cells(result, grain_name)
        test.assertEqual(set(engine), set(ref_cells),
                         "grain %s cell sets differ" % grain_name)
        for key, ref in ref_cells.items():
            cell = engine[key]
            metrics = cell["metrics"]
            test.assertEqual(_frac(metrics["row_weight"]), ref["row_w"],
                             "%s %s row_weight" % (grain_name, key))
            test.assertEqual(_frac(metrics["value_weight"]), ref["val_w"],
                             "%s %s value_weight" % (grain_name, key))
            if ref["has_amount"]:
                test.assertIsNotNone(metrics["amount_sum"])
                test.assertEqual(_frac(metrics["amount_sum"]), ref["amount_sum"],
                                 "%s %s amount_sum" % (grain_name, key))
                test.assertEqual(_frac(metrics["amount_mean"]),
                                 ref["amount_sum"] / ref["val_w"],
                                 "%s %s amount_mean" % (grain_name, key))
            else:
                test.assertEqual(ref["val_w"], 0)
                test.assertIsNone(metrics["amount_sum"],
                                  "%s %s amount_sum must be null" % (grain_name, key))
                test.assertIsNone(metrics["amount_mean"])
            test.assertEqual(metrics["vehicle_count"], len(ref["vehicles"]),
                             "%s %s vehicle_count" % (grain_name, key))

            # Drill the cell in small pages; the ledger must re-sum to the
            # four rational metrics and dedup to the vehicle count.
            d1 = Coord(key[0], key[1])
            d2 = Coord(key[2], key[3])
            rows = []
            offset = 0
            total = None
            while True:
                page = result.drill(grain_name, d1, d2, offset=offset, limit=2)
                total = page["total_contributions"]
                rows.extend(page["rows"])
                if len(page["rows"]) < 2:
                    break
                offset += 2
            test.assertEqual(total, len(ref["lines"]))
            test.assertEqual(len(rows), len(ref["lines"]),
                             "%s %s: one ledger row per (cell, source line)"
                             % (grain_name, key))
            test.assertEqual([r["line_no"] for r in rows],
                             sorted(r["line_no"] for r in rows),
                             "drill rows must be ordered by source line")
            sum_row_w = Fraction(0)
            sum_val_w = Fraction(0)
            sum_amount = Fraction(0)
            seen_vehicles = set()
            for row in rows:
                coeff = _frac(row["coefficient"])
                test.assertEqual(coeff, ref["lines"][row["line_no"]],
                                 "%s %s line %d coefficient"
                                 % (grain_name, key, row["line_no"]))
                sum_row_w += coeff
                if row["amount"] is not None:
                    sum_val_w += coeff
                    sum_amount += row["amount"] * coeff
                if row["vehicle_unit"] is not None:
                    seen_vehicles.add(row["vehicle_unit"])
            test.assertEqual(sum_row_w, ref["row_w"])
            test.assertEqual(sum_val_w, ref["val_w"])
            if ref["has_amount"]:
                test.assertEqual(sum_amount, ref["amount_sum"])
            test.assertEqual(seen_vehicles, ref["vehicles"])


# ---------------------------------------------------------------------------
# Fixture-backed cases
# ---------------------------------------------------------------------------

class RollupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rollup-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, payload) -> Path:
        path = self.tmp / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        return path

    def _write_facts(self, name: str, rows) -> Path:
        path = self.tmp / name
        lines = []
        for row in rows:
            lines.append(row if isinstance(row, str)
                         else json.dumps(row, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _build(self, config_path: Path, facts_path: Path, name: str = "out.db",
               **kw) -> Path:
        out = self.tmp / name
        build(config_path, facts_path, out, **kw)
        return out


class ManualExampleTest(RollupCase):
    """The worked example from the task statement, against hand-written numbers."""

    def test_manual_cells_and_drill(self) -> None:
        out = self._build(DIST / "manual-config.json", DIST / "manual-facts.jsonl")
        expected = json.loads((DIST / "manual-expected.json").read_text())
        dims = ("line_cat", "depot")
        with RollupResult(out) as result:
            for grain, exp_cells in expected["grains"].items():
                got = {}
                for cell in result.cells(grain):
                    key = tuple((cell["coords"][d]["kind"],
                                 cell["coords"][d].get("value")) for d in dims)
                    got[key] = cell["metrics"]
                want = {}
                for cell in exp_cells:
                    key = tuple((cell["coords"][d]["kind"],
                                 cell["coords"][d].get("value")) for d in dims)
                    want[key] = cell["metrics"]
                self.assertEqual(got, want, "grain %s" % grain)

            # The task's headline numbers, spelled out literally.
            trunk_x = result.drill("trunk_by_depot", Coord("member", "X"),
                                   Coord("member", "D1"))
            self.assertEqual(_frac(trunk_x["metrics"]["row_weight"]), Fraction(3, 2))
            self.assertEqual(_frac(trunk_x["metrics"]["amount_sum"]), Fraction(45, 2))
            self.assertEqual(_frac(trunk_x["metrics"]["amount_mean"]), Fraction(15))
            self.assertEqual(trunk_x["metrics"]["vehicle_count"], 1)
            self.assertEqual([r["coefficient"] for r in trunk_x["rows"]],
                             [{"num": "3", "den": "4"}, {"num": "3", "den": "4"}])

            # Finer A/B vehicle counts (1+1) must not add up to the root's 1.
            cells = {tuple((c["coords"][d]["kind"], c["coords"][d].get("value"))
                           for d in dims): c["metrics"]
                     for c in result.cells("category_by_depot")}
            self.assertEqual(cells[(("member", "A"), ("member", "D1"))]["vehicle_count"], 1)
            self.assertEqual(cells[(("member", "B"), ("member", "D1"))]["vehicle_count"], 1)
            root = result.cells("root_totals", d1=Coord("member", "R"))
            self.assertEqual(root[0]["metrics"]["vehicle_count"], 1)

            # The null-dimension fact proves root cells differ from ALL cells.
            self.assertEqual(_frac(root[0]["metrics"]["row_weight"]), Fraction(2))
            grand = result.cells("grand_total")
            self.assertEqual(_frac(grand[0]["metrics"]["row_weight"]), Fraction(3))
            self.assertEqual(grand[0]["metrics"]["vehicle_count"], 2)

    def test_manual_against_reference(self) -> None:
        config = json.loads((DIST / "manual-config.json").read_text())
        facts = [json.loads(line) for line in
                 (DIST / "manual-facts.jsonl").read_text().splitlines()]
        out = self._build(DIST / "manual-config.json", DIST / "manual-facts.jsonl")
        with RollupResult(out) as result:
            assert_cells_match(self, result, reference_rollup(config, facts))


class StressExampleTest(RollupCase):
    def test_stress_highlights(self) -> None:
        out = self._build(DIST / "stress-config.json", DIST / "stress-facts.jsonl")
        expected = json.loads((DIST / "stress-expected.json").read_text())
        dims = ("line", "depot")
        with RollupResult(out) as result:
            for grain, cells in expected.items():
                got = {}
                for cell in result.cells(grain):
                    key = tuple((cell["coords"][d]["kind"],
                                 cell["coords"][d].get("value")) for d in dims)
                    got[key] = cell["metrics"]
                for label, exp in cells.items():
                    key = tuple((exp["coords"][d]["kind"], exp["coords"][d].get("value"))
                                for d in dims)
                    self.assertIn(key, got, "%s %s missing" % (grain, label))
                    self.assertEqual(got[key], exp["metrics"], "%s %s" % (grain, label))

    def test_stress_against_reference(self) -> None:
        config = json.loads((DIST / "stress-config.json").read_text())
        facts = [json.loads(line) for line in
                 (DIST / "stress-facts.jsonl").read_text().splitlines()]
        out = self._build(DIST / "stress-config.json", DIST / "stress-facts.jsonl")
        with RollupResult(out) as result:
            assert_cells_match(self, result, reference_rollup(config, facts))

    def test_coordinate_kinds_are_distinct(self) -> None:
        """Member "ALL", real null and the ALL selector stay three things."""
        out = self._build(DIST / "stress-config.json", DIST / "stress-facts.jsonl")
        with RollupResult(out) as result:
            member_all = result.cells("trunk_by_region", d2=Coord("member", "ALL"))
            self.assertTrue(member_all)
            for cell in member_all:
                self.assertEqual(cell["coords"]["depot"],
                                 {"kind": "member", "level": "region", "value": "ALL"})
            real_null = result.cells("trunk_by_region", d2=Coord("null"))
            self.assertTrue(real_null)
            for cell in real_null:
                self.assertEqual(cell["coords"]["depot"]["kind"], "null")
            dropped = result.cells("trunk_all", d2=Coord("all"))
            self.assertTrue(dropped)
            for cell in dropped:
                self.assertEqual(cell["coords"]["depot"], {"kind": "all"})
            # leaf id "007" is a plain member string
            leaf007 = result.cells("leaf_by_leaf", d1=Coord("member", "007"))
            self.assertEqual(len(leaf007), 1)
            self.assertEqual(leaf007[0]["coords"]["line"]["value"], "007")

    def test_result_is_self_contained(self) -> None:
        """A fresh reader needs only the result DB, not the input files."""
        import shutil
        config = self.tmp / "config.json"
        facts = self.tmp / "facts.jsonl"
        shutil.copyfile(DIST / "manual-config.json", config)
        shutil.copyfile(DIST / "manual-facts.jsonl", facts)
        out = self._build(config, facts)
        # Inputs are read-only for the tool; simulate them being archived away.
        config.rename(self.tmp / "config.gone")
        facts.rename(self.tmp / "facts.gone")
        with RollupResult(out) as result:
            self.assertEqual(result.info()["fact_rows"], 3)
            self.assertEqual(result.fact(2)["amount"], 20)
            cells = result.cells("grand_total")
            self.assertEqual(_frac(cells[0]["metrics"]["row_weight"]), Fraction(3))


# ---------------------------------------------------------------------------
# Randomized differential test against the independent enumerator
# ---------------------------------------------------------------------------

_IDS = ["007", "ALL", "0", "A", "B", "L1", "L2", "D1", "D2", "X", "Y", "R",
        "E", "Q", "M", "N", "0070", "all"]


def _random_dim(rng: random.Random, dim_idx: int) -> dict:
    n_levels = rng.randint(2, 4)
    levels = []
    for lvl_idx in range(n_levels):
        if lvl_idx == n_levels - 1:
            members = ["root%d" % dim_idx]
        else:
            count = rng.randint(1, 4)
            members = []
            for m in range(count):
                base = rng.choice(_IDS)
                member = "%s_%d_%d" % (base, dim_idx, m)
                while member in members:
                    member += "x"
                members.append(member)
        levels.append({"name": "lvl%d_%d" % (dim_idx, lvl_idx), "members": members})
    links = []
    for lvl_idx in range(n_levels - 1):
        children = levels[lvl_idx]["members"]
        parents = levels[lvl_idx + 1]["members"]
        edges = []
        for child in children:
            k = rng.randint(1, min(3, len(parents)))
            chosen = rng.sample(parents, k)
            weights = [rng.randint(1, 5) for _ in chosen]
            total = sum(weights)
            for parent, weight in zip(chosen, weights):
                edges.append({"child": child, "parent": parent,
                              "numerator": weight, "denominator": total})
        links.append({"from": levels[lvl_idx]["name"],
                      "to": levels[lvl_idx + 1]["name"], "edges": edges})
    return {"levels": levels, "links": links}


def _random_case(rng: random.Random):
    dims = {"d1": _random_dim(rng, 1), "d2": _random_dim(rng, 2)}
    level_names = {d: [lvl["name"] for lvl in dims[d]["levels"]] for d in dims}
    combos = set()
    grains = []
    for i in range(rng.randint(1, 5)):
        sel = tuple(rng.choice(level_names[d] + ["ALL"]) for d in ("d1", "d2"))
        if sel in combos:
            continue
        combos.add(sel)
        grains.append({"name": "g%d" % i,
                       "levels": {"d1": sel[0], "d2": sel[1]}})
    config = {
        "dimensions": dims,
        "fields": {"dimensions": {"d1": "f1", "d2": "f2"},
                   "vehicle": "veh", "amount": "amt"},
        "grains": grains,
    }
    leaves1 = dims["d1"]["levels"][0]["members"]
    leaves2 = dims["d2"]["levels"][0]["members"]
    vehicles = ["v1", "v2", "", "007"]
    facts = []
    for _ in range(rng.randint(20, 120)):
        amount = rng.choice([None, 0, rng.randint(-50, 50),
                             rng.randint(-10**25, 10**25)])
        facts.append({
            "f1": rng.choice(leaves1 + [None]),
            "f2": rng.choice(leaves2 + [None]),
            "veh": rng.choice(vehicles + [None]),
            "amt": amount,
            "ignored_extra": rng.randint(0, 9),
        })
    return config, facts


class RandomizedDifferentialTest(RollupCase):
    def test_random_cases_match_reference(self) -> None:
        for seed in range(25):
            rng = random.Random(20261114 + seed)
            config, facts = _random_case(rng)
            config_path = self._write("cfg-%d.json" % seed, config)
            facts_path = self._write_facts("facts-%d.jsonl" % seed, facts)
            out = self._build(config_path, facts_path, name="rand-%d.db" % seed,
                              batch_size=7)  # tiny batches exercise flushing
            with self.subTest(seed=seed), RollupResult(out) as result:
                assert_cells_match(self, result, reference_rollup(config, facts))


# ---------------------------------------------------------------------------
# Config validation failures
# ---------------------------------------------------------------------------

def _base_config() -> dict:
    return {
        "dimensions": {
            "d1": {
                "levels": [
                    {"name": "leaf", "members": ["a", "b"]},
                    {"name": "mid", "members": ["m"]},
                    {"name": "root", "members": ["r"]},
                ],
                "links": [
                    {"from": "leaf", "to": "mid", "edges": [
                        {"child": "a", "parent": "m", "numerator": 1, "denominator": 2},
                        {"child": "a", "parent": "m", "numerator": 0, "denominator": 1},
                    ]},
                    {"from": "mid", "to": "root", "edges": [
                        {"child": "m", "parent": "r", "numerator": 1, "denominator": 1},
                    ]},
                ],
            },
            "d2": {
                "levels": [
                    {"name": "leaf", "members": ["x"]},
                    {"name": "root", "members": ["z"]},
                ],
                "links": [
                    {"from": "leaf", "to": "root", "edges": [
                        {"child": "x", "parent": "z", "numerator": 1, "denominator": 1},
                    ]},
                ],
            },
        },
        "fields": {"dimensions": {"d1": "f1", "d2": "f2"},
                   "vehicle": "veh", "amount": "amt"},
        "grains": [{"name": "g", "levels": {"d1": "mid", "d2": "leaf"}}],
    }


def _good_config() -> dict:
    config = _base_config()
    config["dimensions"]["d1"]["links"][0]["edges"] = [
        {"child": "a", "parent": "m", "numerator": 1, "denominator": 2},
        {"child": "b", "parent": "m", "numerator": 1, "denominator": 1},
    ]
    # 'a' splits 1/2 to m and ... need sum 1: give a a second parent? mid has
    # only m, so make a -> m 1/1 and b -> m 1/1.
    config["dimensions"]["d1"]["links"][0]["edges"] = [
        {"child": "a", "parent": "m", "numerator": 1, "denominator": 1},
        {"child": "b", "parent": "m", "numerator": 1, "denominator": 1},
    ]
    return config


class ConfigErrorTest(RollupCase):
    def _facts(self) -> Path:
        return self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "x", "veh": "v", "amt": 1}])

    def _expect_error(self, config, needle: str) -> None:
        config_path = self._write("cfg.json", config)
        with self.assertRaises(RollupError) as ctx:
            build(config_path, self._facts(), self.tmp / "out.db")
        self.assertIn(needle, str(ctx.exception),
                      "error should mention %r" % needle)
        self.assertFalse((self.tmp / "out.db").exists(),
                         "failed build must not leave a result database")

    def test_good_config_builds(self) -> None:
        out = self._build(self._write("cfg.json", _good_config()), self._facts())
        with RollupResult(out) as result:
            self.assertEqual(result.info()["fact_rows"], 1)

    def test_proportions_must_sum_to_one(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"] = [
            {"child": "a", "parent": "m", "numerator": 1, "denominator": 2},
            {"child": "b", "parent": "m", "numerator": 1, "denominator": 1},
        ]
        self._expect_error(config, "sum")

    def test_unknown_child_member(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"].append(
            {"child": "ghost", "parent": "m", "numerator": 1, "denominator": 2})
        self._expect_error(config, "unknown child member")

    def test_unknown_parent_member(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][1]["edges"] = [
            {"child": "m", "parent": "Q", "numerator": 1, "denominator": 1}]
        self._expect_error(config, "unknown parent member")

    def test_cross_level_link_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"].append(
            {"from": "leaf", "to": "root", "edges": []})
        self._expect_error(config, "adjacent")

    def test_duplicate_edge_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"] = [
            {"child": "a", "parent": "m", "numerator": 1, "denominator": 2},
            {"child": "a", "parent": "m", "numerator": 1, "denominator": 2},
            {"child": "b", "parent": "m", "numerator": 1, "denominator": 1},
        ]
        self._expect_error(config, "duplicate edge")

    def test_zero_coefficient_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"][0]["numerator"] = 0
        self._expect_error(config, "positive integer")

    def test_bool_coefficient_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"][0]["denominator"] = True
        self._expect_error(config, "positive integer")

    def test_float_coefficient_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"][0]["numerator"] = 0.5
        self._expect_error(config, "positive integer")

    def test_member_without_parent_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"][0]["edges"] = [
            {"child": "a", "parent": "m", "numerator": 1, "denominator": 1}]
        self._expect_error(config, "no parent")

    def test_missing_link_block_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["links"] = config["dimensions"]["d1"]["links"][:1]
        self._expect_error(config, "missing link block")

    def test_empty_level_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["levels"][1]["members"] = []
        self._expect_error(config, "non-empty array")

    def test_empty_dimension_name_rejected(self) -> None:
        config = _good_config()
        config["dimensions"][""] = config["dimensions"].pop("d1")
        self._expect_error(config, "non-empty string")

    def test_single_dimension_rejected(self) -> None:
        config = _good_config()
        del config["dimensions"]["d2"]
        self._expect_error(config, "exactly two")

    def test_root_must_be_single(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["levels"][2]["members"] = ["r", "r2"]
        self._expect_error(config, "exactly one root")

    def test_duplicate_member_in_level_rejected(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["levels"][0]["members"] = ["a", "a"]
        self._expect_error(config, "duplicate member")

    def test_level_named_all_reserved(self) -> None:
        config = _good_config()
        config["dimensions"]["d1"]["levels"][1]["name"] = "ALL"
        self._expect_error(config, "reserved")

    def test_field_names_must_not_be_reused(self) -> None:
        config = _good_config()
        config["fields"]["amount"] = "veh"
        self._expect_error(config, "must all be different")

    def test_duplicate_grain_name_rejected(self) -> None:
        config = _good_config()
        config["grains"].append({"name": "g", "levels": {"d1": "leaf", "d2": "leaf"}})
        self._expect_error(config, "duplicate grain name")

    def test_duplicate_grain_combo_rejected_even_reordered(self) -> None:
        config = _good_config()
        config["grains"].append(
            {"name": "g2", "levels": {"d2": "leaf", "d1": "mid"}})
        self._expect_error(config, "duplicate grain level combination")

    def test_grain_unknown_level_rejected(self) -> None:
        config = _good_config()
        config["grains"][0]["levels"]["d1"] = "nowhere"
        self._expect_error(config, "no level")

    def test_duplicate_json_member_rejected(self) -> None:
        text = json.dumps(_good_config())
        text = text.replace('"grains"', '"grains": [], "grains"', 1)
        config_path = self._write("cfg.json", text)
        with self.assertRaises(RollupError) as ctx:
            build(config_path, self._facts(), self.tmp / "out.db")
        self.assertIn("duplicate JSON member", str(ctx.exception))


# ---------------------------------------------------------------------------
# Fact validation failures
# ---------------------------------------------------------------------------

class FactErrorTest(RollupCase):
    def _config(self) -> Path:
        return self._write("cfg.json", _good_config())

    def _expect_error(self, lines, needle: str, line_no: int | None = None) -> None:
        facts_path = self._write_facts("facts.jsonl", lines)
        with self.assertRaises(RollupError) as ctx:
            build(self._config(), facts_path, self.tmp / "out.db")
        message = str(ctx.exception)
        self.assertIn(needle, message)
        if line_no is not None:
            self.assertIn("line %d" % line_no, message)
        self.assertFalse((self.tmp / "out.db").exists())

    def test_decimal_amount_rejected_at_tail(self) -> None:
        self._expect_error([
            {"f1": "a", "f2": "x", "veh": "v", "amt": 1},
            {"f1": "a", "f2": "x", "veh": "v", "amt": 2},
            {"f1": "a", "f2": "x", "veh": "v", "amt": 10.5},
        ], "amount", line_no=3)

    def test_bool_amount_rejected(self) -> None:
        self._expect_error([{"f1": "a", "f2": "x", "veh": "v", "amt": True}],
                           "amount", line_no=1)

    def test_string_amount_rejected(self) -> None:
        self._expect_error([{"f1": "a", "f2": "x", "veh": "v", "amt": "7"}],
                           "amount")

    def test_unknown_leaf_rejected_with_field(self) -> None:
        facts_path = self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "nope", "veh": "v", "amt": 1}])
        with self.assertRaises(RollupError) as ctx:
            build(self._config(), facts_path, self.tmp / "out.db")
        self.assertIn("line 1", str(ctx.exception))
        self.assertIn("'f2'", str(ctx.exception))
        self.assertIn("unknown leaf member", str(ctx.exception))

    def test_empty_string_leaf_is_not_a_wildcard(self) -> None:
        self._expect_error([{"f1": "", "f2": "x", "veh": "v", "amt": 1}],
                           "unknown leaf member")

    def test_missing_field_rejected(self) -> None:
        self._expect_error([{"f1": "a", "f2": "x", "veh": "v"}], "missing")

    def test_duplicate_json_member_rejected(self) -> None:
        self._expect_error(
            ['{"f1": "a", "f1": "a", "f2": "x", "veh": "v", "amt": 1}'],
            "duplicate JSON member", line_no=1)

    def test_empty_line_rejected(self) -> None:
        self._expect_error([{"f1": "a", "f2": "x", "veh": "v", "amt": 1}, "   "],
                           "empty line", line_no=2)

    def test_non_object_line_rejected(self) -> None:
        self._expect_error(["[1, 2]"], "JSON object")

    def test_vehicle_type_rejected(self) -> None:
        self._expect_error([{"f1": "a", "f2": "x", "veh": 7, "amt": 1}],
                           "vehicle_unit")

    def test_dimension_value_type_rejected(self) -> None:
        self._expect_error([{"f1": 7, "f2": "x", "veh": "v", "amt": 1}],
                           "leaf member string or null")

    def test_identical_lines_are_two_facts(self) -> None:
        facts_path = self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "x", "veh": "v", "amt": 5},
            {"f1": "a", "f2": "x", "veh": "v", "amt": 5},
        ])
        out = self._build(self._config(), facts_path)
        with RollupResult(out) as result:
            cells = result.cells("g", d1=Coord("member", "m"))
            self.assertEqual(_frac(cells[0]["metrics"]["row_weight"]), Fraction(2))
            drill = result.drill("g", Coord("member", "m"), Coord("member", "x"))
            self.assertEqual(drill["total_contributions"], 2)
            self.assertEqual([r["line_no"] for r in drill["rows"]], [1, 2])


# ---------------------------------------------------------------------------
# Build/run lifecycle
# ---------------------------------------------------------------------------

class LifecycleTest(RollupCase):
    def test_refuses_to_overwrite_and_keeps_existing_bytes(self) -> None:
        config = self._write("cfg.json", _good_config())
        facts = self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "x", "veh": "v", "amt": 1}])
        out = self._build(config, facts)
        before = out.read_bytes()
        with self.assertRaises(RollupError) as ctx:
            build(config, facts, out)
        self.assertIn("refusing to overwrite", str(ctx.exception))
        self.assertEqual(out.read_bytes(), before,
                         "a refused overwrite must not touch the existing result")

    def test_failed_build_leaves_no_queryable_result(self) -> None:
        config = self._write("cfg.json", _good_config())
        facts = self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "x", "veh": "v", "amt": 1},
            {"f1": "a", "f2": "x", "veh": "v", "amt": "bad"},
        ])
        out = self.tmp / "out.db"
        with self.assertRaises(RollupError):
            build(config, facts, out)
        self.assertFalse(out.exists())
        self.assertFalse(list(self.tmp.glob("out.db.tmp-*")),
                         "temp build files must be cleaned up")
        with self.assertRaises(RollupError):
            RollupResult(out)

    def test_incomplete_database_is_not_queryable(self) -> None:
        import sqlite3
        out = self.tmp / "half.db"
        conn = sqlite3.connect(str(out))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO meta VALUES ('status', 'building')")
        conn.commit()
        conn.close()
        with self.assertRaises(RollupError) as ctx:
            RollupResult(out)
        self.assertIn("incomplete", str(ctx.exception))

    def test_inputs_are_not_modified(self) -> None:
        config = self._write("cfg.json", _good_config())
        facts = self._write_facts("facts.jsonl", [
            {"f1": "a", "f2": "x", "veh": "v", "amt": 1}])
        config_bytes = config.read_bytes()
        facts_bytes = facts.read_bytes()
        self._build(config, facts)
        self.assertEqual(config.read_bytes(), config_bytes)
        self.assertEqual(facts.read_bytes(), facts_bytes)

    def test_empty_facts_file_builds_an_empty_result(self) -> None:
        config = self._write("cfg.json", _good_config())
        facts = self.tmp / "empty.jsonl"
        facts.write_bytes(b"")
        out = self._build(config, facts)
        with RollupResult(out) as result:
            self.assertEqual(result.info()["fact_rows"], 0)
            self.assertEqual(result.cells("g"), [])
            self.assertEqual(result.grains()[0]["cells"], 0)


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

class CliTest(RollupCase):
    def _run(self, argv: list[str]) -> int:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli_main(argv)
        self.last_output = buf.getvalue()
        return code

    def test_full_cli_flow(self) -> None:
        out = self.tmp / "manual.db"
        code = self._run(["--json", "rollup-build", "--config",
                          str(DIST / "manual-config.json"), "--facts",
                          str(DIST / "manual-facts.jsonl"), "--out", str(out)])
        self.assertEqual(code, 0, self.last_output)
        payload = json.loads(self.last_output)
        self.assertEqual(payload["cells"], 9)

        code = self._run(["--json", "rollup-cells", "--result", str(out),
                          "--grain", "trunk_by_depot",
                          "--d1-kind", "member", "--d1-value", "X"])
        self.assertEqual(code, 0, self.last_output)
        cells = json.loads(self.last_output)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["metrics"]["amount_mean"], {"num": "15", "den": "1"})

        code = self._run(["--json", "rollup-cells", "--result", str(out),
                          "--grain", "trunk_by_depot", "--d1-kind", "null"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(self.last_output)), 1)

        code = self._run(["--json", "rollup-drill", "--result", str(out),
                          "--grain", "trunk_by_depot",
                          "--d1-kind", "member", "--d1-value", "X",
                          "--d2-kind", "member", "--d2-value", "D1",
                          "--limit", "1"])
        self.assertEqual(code, 0, self.last_output)
        drill = json.loads(self.last_output)
        self.assertEqual(drill["total_contributions"], 2)
        self.assertEqual(len(drill["rows"]), 1)
        self.assertEqual(drill["rows"][0]["coefficient"], {"num": "3", "den": "4"})

        code = self._run(["--json", "rollup-info", "--result", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.last_output)["fact_rows"], 3)

        code = self._run(["--json", "rollup-fact", "--result", str(out), "--line", "3"])
        self.assertEqual(code, 0)
        fact = json.loads(self.last_output)
        self.assertIsNone(fact["dimensions"]["line_cat"])
        self.assertEqual(fact["amount"], 7)

    def test_cli_bad_build_exit_codes(self) -> None:
        out = self.tmp / "bad.db"
        code = self._run(["rollup-build", "--config", str(DIST / "bad-sum-config.json"),
                          "--facts", str(DIST / "manual-facts.jsonl"),
                          "--out", str(out)])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())

        code = self._run(["rollup-build", "--config", str(DIST / "manual-config.json"),
                          "--facts", str(DIST / "bad-tail-facts.jsonl"),
                          "--out", str(out)])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("line 3", self.last_output)

        code = self._run(["rollup-info", "--result", str(out)])
        self.assertEqual(code, 1)

    def test_cli_refuses_overwrite(self) -> None:
        out = self.tmp / "keep.db"
        argv = ["rollup-build", "--config", str(DIST / "manual-config.json"),
                "--facts", str(DIST / "manual-facts.jsonl"), "--out", str(out)]
        self.assertEqual(self._run(argv), 0)
        before = out.read_bytes()
        self.assertEqual(self._run(argv), 1)
        self.assertIn("refusing to overwrite", self.last_output)
        self.assertEqual(out.read_bytes(), before)

    def test_existing_commands_still_work(self) -> None:
        # The pre-existing entry points keep their behaviour (usage error = 2).
        self.assertEqual(self._run([]), 2)
        code = self._run(["--db", str(self.tmp / "gtfs.db"), "list"])
        self.assertEqual(code, 0, self.last_output)


if __name__ == "__main__":
    unittest.main()
