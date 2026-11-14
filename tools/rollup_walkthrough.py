"""End-to-end rehearsal of the weighted hierarchical rollup tool.

Everything below runs the real terminal entries (./gtfs-store rollup-*) in
fresh subprocesses against the hand-written fixtures in dist/rollup/:

* the worked manual example, checked cell-by-cell against the hand-computed
  expectations in dist/rollup/manual-expected.json (including the drill-down
  coefficients, the root-vs-ALL difference and the non-additive vehicle
  counts);
* the stress example, checked cell-by-cell against an independent brute-force
  enumerator written in this file (it imports nothing from gtfs_store.rollup)
  plus paginated drill-down re-summation of every occupied cell;
* the four required failure demos (proportions not summing to 1, bad member
  reference, duplicate grain, bad fact at the file tail): each must exit
  non-zero and leave no queryable result behind;
* an overwrite attempt against an existing result: refused, bytes unchanged;
* a fresh-process read that only sees the result database.

Run:  python3 -I tools/rollup_walkthrough.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "rollup"
CLI = str(ROOT / "gtfs-store")


def run_cli(*argv: str, expect: int = 0) -> tuple[int, object]:
    proc = subprocess.run([sys.executable, "-I", CLI, "--json", *argv],
                          capture_output=True, text=True)
    if proc.returncode != expect:
        raise AssertionError("gtfs-store %s -> exit %d (wanted %d)\nstdout: %s\nstderr: %s"
                             % (" ".join(argv), proc.returncode, expect,
                                proc.stdout, proc.stderr))
    payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, payload


def show(title: str) -> None:
    print("\n=== %s %s" % (title, "=" * max(2, 62 - len(title))))


def frac(num, den=1) -> Fraction:
    return Fraction(num, den)


def frac_of(obj: dict) -> Fraction:
    return Fraction(int(obj["num"]), int(obj["den"]))


def fmt(obj: dict | None) -> str:
    if obj is None:
        return "null"
    if obj["den"] == "1":
        return obj["num"]
    return "%s/%s" % (obj["num"], obj["den"])


def coord_text(coord: dict) -> str:
    if coord["kind"] == "all":
        return "*"
    if coord["kind"] == "null":
        return "NULL"
    return '"%s"' % coord["value"]


def print_cell(cell: dict) -> None:
    dims = list(cell["coords"].values())
    m = cell["metrics"]
    print("  [%-9s %-9s] row_w=%-6s val_w=%-6s sum=%-8s mean=%-6s vehicles=%d"
          % (coord_text(dims[0]), coord_text(dims[1]), fmt(m["row_weight"]),
             fmt(m["value_weight"]), fmt(m["amount_sum"]), fmt(m["amount_mean"]),
             m["vehicle_count"]))


# ---------------------------------------------------------------------------
# Independent brute-force enumerator (shares no code with gtfs_store.rollup)
# ---------------------------------------------------------------------------

def enum_paths(dim_spec: dict, leaf: str, target_level: str) -> dict:
    """Sum of path products from ``leaf`` to each member of ``target_level``."""
    levels = [lvl["name"] for lvl in dim_spec["levels"]]
    adj = {}
    for link in dim_spec["links"]:
        per_child = defaultdict(list)
        for edge in link["edges"]:
            per_child[edge["child"]].append(
                (edge["parent"], Fraction(edge["numerator"], edge["denominator"])))
        adj[link["from"]] = dict(per_child)
    out = defaultdict(Fraction)

    def visit(member, idx, acc):
        if levels[idx] == target_level:
            out[member] += acc
            return
        for parent, weight in adj.get(levels[idx], {}).get(member, []):
            visit(parent, idx + 1, acc * weight)

    visit(leaf, 0, Fraction(1))
    return dict(out)


def enum_targets(dim_spec, selector, value):
    if selector == "ALL":
        return {("all", None): Fraction(1)}
    if value is None:
        return {("null", None): Fraction(1)}
    return {("member", m): c for m, c in enum_paths(dim_spec, value, selector).items()}


def enum_cells(config: dict, facts: list[dict]) -> dict:
    """{grain: {(k1,v1,k2,v2): {row_w,val_w,sum,vehicles,lines}}}"""
    dims = list(config["dimensions"])
    fields = config["fields"]
    grains = {}
    for grain in config["grains"]:
        cells = {}
        for line_no, fact in enumerate(facts, start=1):
            t1 = enum_targets(config["dimensions"][dims[0]],
                              grain["levels"][dims[0]],
                              fact[fields["dimensions"][dims[0]]])
            t2 = enum_targets(config["dimensions"][dims[1]],
                              grain["levels"][dims[1]],
                              fact[fields["dimensions"][dims[1]]])
            for (k1, v1), c1 in t1.items():
                for (k2, v2), c2 in t2.items():
                    key = (k1, v1, k2, v2)
                    cell = cells.setdefault(key, {
                        "row_w": Fraction(0), "val_w": Fraction(0),
                        "sum": Fraction(0), "has_amount": False,
                        "vehicles": set(), "lines": defaultdict(Fraction)})
                    coeff = c1 * c2
                    cell["row_w"] += coeff
                    amount = fact[fields["amount"]]
                    if amount is not None:
                        cell["has_amount"] = True
                        cell["val_w"] += coeff
                        cell["sum"] += amount * coeff
                    vehicle = fact[fields["vehicle"]]
                    if vehicle is not None:
                        cell["vehicles"].add(vehicle)
                    cell["lines"][line_no] += coeff
        grains[grain["name"]] = cells
    return grains


def cell_key(cell: dict) -> tuple:
    coords = list(cell["coords"].values())
    return (coords[0]["kind"], coords[0].get("value"),
            coords[1]["kind"], coords[1].get("value"))


def check_cell_against_enum(grain: str, cell: dict, expected: dict) -> None:
    m = cell["metrics"]
    label = "%s %s" % (grain, cell_key(cell))
    assert frac_of(m["row_weight"]) == expected["row_w"], label
    assert frac_of(m["value_weight"]) == expected["val_w"], label
    if expected["has_amount"]:
        assert m["amount_sum"] is not None, label
        assert frac_of(m["amount_sum"]) == expected["sum"], label
        assert frac_of(m["amount_mean"]) == expected["sum"] / expected["val_w"], label
    else:
        assert expected["val_w"] == 0, label
        assert m["amount_sum"] is None and m["amount_mean"] is None, label
    assert m["vehicle_count"] == len(expected["vehicles"]), label


def drill_all(db: Path, grain: str, key: tuple, page: int = 3) -> list:
    """Page through one cell's ledger with the real CLI; return all rows."""
    rows = []
    offset = 0
    total = None
    while True:
        _, payload = run_cli("rollup-drill", "--result", str(db), "--grain", grain,
                             "--d1-kind", key[0],
                             *(["--d1-value", key[1]] if key[1] is not None else []),
                             "--d2-kind", key[2],
                             *(["--d2-value", key[3]] if key[3] is not None else []),
                             "--offset", str(offset), "--limit", str(page))
        total = payload["total_contributions"]
        rows.extend(payload["rows"])
        if len(payload["rows"]) < page:
            break
        offset += page
    assert total == len(rows), "ledger count must match its rows"
    return rows


def main() -> int:
    if not (DIST / "manual-config.json").is_file():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "build_rollup_fixtures.py")], check=True)
    work = Path(tempfile.mkdtemp(prefix="rollup-walkthrough-"))
    print("work dir: %s" % work)

    # ------------------------------------------------------------------ 1
    show("1. build the manual example (real CLI)")
    manual_db = work / "manual.db"
    _, payload = run_cli("rollup-build", "--config", str(DIST / "manual-config.json"),
                         "--facts", str(DIST / "manual-facts.jsonl"),
                         "--out", str(manual_db))
    print("build: facts=%(facts)d grains=%(grains)d cells=%(cells)d "
          "contributions=%(contributions)d" % payload)

    show("2. manual cells vs hand-written expectations")
    expected = json.loads((DIST / "manual-expected.json").read_text())
    dims = ("line_cat", "depot")
    for grain, exp_cells in expected["grains"].items():
        _, cells = run_cli("rollup-cells", "--result", str(manual_db), "--grain", grain)
        got = {tuple((c["coords"][d]["kind"], c["coords"][d].get("value")) for d in dims):
               c["metrics"] for c in cells}
        want = {tuple((c["coords"][d]["kind"], c["coords"][d].get("value")) for d in dims):
                c["metrics"] for c in exp_cells}
        assert got == want, "grain %s mismatch:\n%s\nvs\n%s" % (grain, got, want)
        print("grain %s: %d cell(s), all metrics match hand arithmetic" % (grain, len(cells)))
        for cell in cells:
            print_cell(cell)

    show("3. drill the X cell; root != ALL; vehicle counts do not add up")
    _, drill = run_cli("rollup-drill", "--result", str(manual_db),
                       "--grain", "trunk_by_depot",
                       "--d1-kind", "member", "--d1-value", "X",
                       "--d2-kind", "member", "--d2-value", "D1")
    print("cell (X, D1): %d contribution(s)" % drill["total_contributions"])
    for row in drill["rows"]:
        print("  line=%d amount=%d vehicle=%r coeff=%s"
              % (row["line_no"], row["amount"], row["vehicle_unit"],
                 fmt(row["coefficient"])))
    exp_drill = expected["drill"]
    assert drill["total_contributions"] == exp_drill["total_contributions"]
    for got_row, exp_row in zip(drill["rows"], exp_drill["rows"]):
        assert got_row["line_no"] == exp_row["line_no"]
        assert got_row["coefficient"] == exp_row["coefficient"]
    print("drill coefficients match the hand-written 3/4 splits")

    _, root_cells = run_cli("rollup-cells", "--result", str(manual_db),
                            "--grain", "root_totals",
                            "--d1-kind", "member", "--d1-value", "R")
    _, all_cells = run_cli("rollup-cells", "--result", str(manual_db),
                           "--grain", "grand_total")
    root_row_w = frac_of(root_cells[0]["metrics"]["row_weight"])
    all_row_w = frac_of(all_cells[0]["metrics"]["row_weight"])
    print("root cell row_weight=%s  ALL cell row_weight=%s  "
          "(the null-dimension fact never reaches the root)" % (root_row_w, all_row_w))
    assert root_row_w == 2 and all_row_w == 3

    _, cat_cells = run_cli("rollup-cells", "--result", str(manual_db),
                           "--grain", "category_by_depot")
    cat_vehicles = [c["metrics"]["vehicle_count"] for c in cat_cells
                    if c["coords"]["line_cat"]["kind"] == "member"]
    root_vehicles = root_cells[0]["metrics"]["vehicle_count"]
    print("A/B vehicle counts=%s, root vehicle count=%d -> finer counts must "
          "not be summed" % (cat_vehicles, root_vehicles))
    assert cat_vehicles == [1, 1] and root_vehicles == 1

    # ------------------------------------------------------------------ 4
    show("4. stress example vs independent enumerator (every cell, every grain)")
    stress_db = work / "stress.db"
    _, payload = run_cli("rollup-build", "--config", str(DIST / "stress-config.json"),
                         "--facts", str(DIST / "stress-facts.jsonl"),
                         "--out", str(stress_db))
    print("build: facts=%(facts)d cells=%(cells)d contributions=%(contributions)d"
          % payload)
    config = json.loads((DIST / "stress-config.json").read_text())
    facts = [json.loads(line)
             for line in (DIST / "stress-facts.jsonl").read_text().splitlines()]
    reference = enum_cells(config, facts)
    checked_cells = 0
    checked_contribs = 0
    for grain in config["grains"]:
        _, cells = run_cli("rollup-cells", "--result", str(stress_db),
                           "--grain", grain["name"])
        ref_cells = reference[grain["name"]]
        assert len(cells) == len(ref_cells), \
            "grain %s: engine %d cells, enumerator %d" % (
                grain["name"], len(cells), len(ref_cells))
        for cell in cells:
            key = cell_key(cell)
            assert key in ref_cells, "unexpected cell %s %s" % (grain["name"], key)
            expected_cell = ref_cells[key]
            check_cell_against_enum(grain["name"], cell, expected_cell)
            checked_cells += 1
            # paginated drill-down re-sums to the same four rational metrics
            rows = drill_all(stress_db, grain["name"], key)
            assert len(rows) == len(expected_cell["lines"])
            sum_row = sum((frac_of(r["coefficient"]) for r in rows), Fraction(0))
            sum_val = sum((frac_of(r["coefficient"]) for r in rows
                           if r["amount"] is not None), Fraction(0))
            sum_amt = sum((r["amount"] * frac_of(r["coefficient"]) for r in rows
                           if r["amount"] is not None), Fraction(0))
            vehicles = {r["vehicle_unit"] for r in rows
                        if r["vehicle_unit"] is not None}
            assert sum_row == expected_cell["row_w"]
            assert sum_val == expected_cell["val_w"]
            if expected_cell["has_amount"]:
                assert sum_amt == expected_cell["sum"]
            assert vehicles == expected_cell["vehicles"]
            for row in rows:
                assert frac_of(row["coefficient"]) == \
                    expected_cell["lines"][row["line_no"]]
            checked_contribs += len(rows)
        print("grain %-16s %2d cells: metrics + paginated drill ledger all match"
              % (grain["name"], len(cells)))
    print("checked %d cells and %d contribution rows against independent "
          "arithmetic" % (checked_cells, checked_contribs))

    show("5. hand-pinned stress highlights")
    highlights = json.loads((DIST / "stress-expected.json").read_text())
    for grain, cells in highlights.items():
        for label, exp in cells.items():
            args = []
            for prefix, dim in (("d1", "line"), ("d2", "depot")):
                coord = exp["coords"][dim]
                args += ["--%s-kind" % prefix, coord["kind"]]
                if coord.get("value") is not None:
                    args += ["--%s-value" % prefix, coord["value"]]
            _, got = run_cli("rollup-cells", "--result", str(stress_db),
                             "--grain", grain, *args)
            assert len(got) == 1, (grain, label)
            assert got[0]["metrics"] == exp["metrics"], \
                "%s %s:\n%s\nvs\n%s" % (grain, label, got[0]["metrics"], exp["metrics"])
            m = exp["metrics"]
            print("  %-16s %-16s sum=%s mean=%s vehicles=%d"
                  % (grain, label, fmt(m["amount_sum"]), fmt(m["amount_mean"]),
                     m["vehicle_count"]))
    print("exact-zero cancellation, null-amount cells, >64-bit sums, empty-string")
    print("vehicle, duplicate lines and member ids ALL/007 all verified literally")

    # ------------------------------------------------------------------ 6
    show("6. failure demos: non-zero exit, no queryable result")
    demos = [
        ("proportions do not sum to 1", "bad-sum-config.json", "manual-facts.jsonl"),
        ("edge references unknown member", "bad-ref-config.json", "manual-facts.jsonl"),
        ("duplicate grain combination", "bad-dup-grain-config.json", "manual-facts.jsonl"),
        ("bad fact at the file tail", "manual-config.json", "bad-tail-facts.jsonl"),
    ]
    for label, config_name, facts_name in demos:
        db = work / ("bad-%d.db" % demos.index((label, config_name, facts_name)))
        proc = subprocess.run([sys.executable, "-I", CLI, "rollup-build",
                               "--config", str(DIST / config_name),
                               "--facts", str(DIST / facts_name),
                               "--out", str(db)],
                              capture_output=True, text=True)
        assert proc.returncode != 0, "%s: build must fail" % label
        assert not db.exists(), "%s: no result file may remain" % label
        assert not list(work.glob(db.name + ".tmp-*")), "temp files cleaned"
        message = (proc.stderr or proc.stdout).strip().splitlines()[-1]
        print("  %-34s exit=%d  %s" % (label, proc.returncode, message))
        info = subprocess.run([sys.executable, "-I", CLI, "rollup-info",
                               "--result", str(db)], capture_output=True, text=True)
        assert info.returncode != 0, "%s: failed build must not be queryable" % label
    print("every failed build exits non-zero and leaves nothing queryable")

    show("7. overwrite attempt leaves the existing result untouched")
    import hashlib
    before = hashlib.sha256(manual_db.read_bytes()).hexdigest()
    proc = subprocess.run([sys.executable, "-I", CLI, "rollup-build",
                           "--config", str(DIST / "manual-config.json"),
                           "--facts", str(DIST / "manual-facts.jsonl"),
                           "--out", str(manual_db)],
                          capture_output=True, text=True)
    after = hashlib.sha256(manual_db.read_bytes()).hexdigest()
    assert proc.returncode != 0
    assert before == after, "existing result bytes must not change"
    print("  refused with exit=%d; sha256 %s unchanged" % (proc.returncode, before[:16]))

    show("8. fresh process, result database only")
    proc = subprocess.run([sys.executable, "-I", CLI, "--json", "rollup-cells",
                           "--result", str(manual_db), "--grain", "grand_total"],
                          capture_output=True, text=True, cwd="/")
    assert proc.returncode == 0
    cells = json.loads(proc.stdout)
    print("  grand_total from a new process (cwd=/): row_weight=%s vehicles=%d"
          % (fmt(cells[0]["metrics"]["row_weight"]),
             cells[0]["metrics"]["vehicle_count"]))
    assert frac_of(cells[0]["metrics"]["row_weight"]) == 3

    print("\nwalkthrough complete: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
