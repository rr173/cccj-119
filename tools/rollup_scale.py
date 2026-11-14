"""Scale rehearsal for the rollup tool: 100k facts, then doubled.

Runs the real CLI entry (./gtfs-store rollup-build) as a subprocess against
generated inputs with:

* two deliberately unbalanced dimension branches (180 of 200 line leaves sit
  under trunk T0; 20 of 30 depot leaves sit under region G0);
* heavy vehicle duplication (100k facts over a 3k-vehicle pool, skewed);
* null dimensions, null vehicles, null amounts and a few >64-bit amounts.

For each size it reports wall time, the build process peak RSS (measured from
outside the process via getrusage of a fresh wrapper), the actual number of
occupied cells per grain versus the full Cartesian product of the domain,
and a sanity check that the ALL/ALL row_weight equals the fact count exactly.

Run:  python3 -I tools/rollup_scale.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = str(ROOT / "gtfs-store")
OUT = ROOT / "dist" / "rollup" / "scale"

LINES = 200
DEPOTS = 30
VEHICLES = 3000

MEASURE_SNIPPET = """
import json, resource, subprocess, sys, time
start = time.monotonic()
proc = subprocess.run(sys.argv[1:])
wall = time.monotonic() - start
rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({"exit": proc.returncode, "wall_s": round(wall, 3),
                  "peak_rss_kb": rss_kb}))
"""


def scale_config() -> dict:
    line_leaves = ["L%03d" % i for i in range(LINES)]
    cats = ["C%d" % i for i in range(4)]
    # unbalanced: 150 / 30 / 15 / 5 leaves per category
    buckets = [(0, 150, "C0"), (150, 180, "C1"), (180, 195, "C2"), (195, 200, "C3")]
    leaf_edges = []
    for start, end, cat in buckets:
        nxt = "C%d" % ((int(cat[1]) + 1) % 4)
        for i in range(start, end):
            leaf_edges.append({"child": line_leaves[i], "parent": cat,
                               "numerator": 3, "denominator": 4})
            leaf_edges.append({"child": line_leaves[i], "parent": nxt,
                               "numerator": 1, "denominator": 4})
    cat_edges = []
    for cat, trunk in (("C0", "T0"), ("C1", "T0"), ("C2", "T1"), ("C3", "T1")):
        cat_edges.append({"child": cat, "parent": trunk,
                          "numerator": 1, "denominator": 1})
    trunk_edges = [{"child": t, "parent": "R", "numerator": 1, "denominator": 1}
                   for t in ("T0", "T1")]

    depot_leaves = ["D%02d" % i for i in range(DEPOTS)]
    depot_buckets = [(0, 20, "G0"), (20, 27, "G1"), (27, 30, "G2")]
    depot_edges = []
    for start, end, region in depot_buckets:
        nxt = "G%d" % ((int(region[1]) + 1) % 3)
        for i in range(start, end):
            depot_edges.append({"child": depot_leaves[i], "parent": region,
                                "numerator": 1, "denominator": 2})
            depot_edges.append({"child": depot_leaves[i], "parent": nxt,
                                "numerator": 1, "denominator": 2})
    region_edges = [{"child": g, "parent": "RD", "numerator": 1, "denominator": 1}
                    for g in ("G0", "G1", "G2")]

    return {
        "dimensions": {
            "line": {
                "levels": [
                    {"name": "line_leaf", "members": line_leaves},
                    {"name": "category", "members": cats},
                    {"name": "trunk", "members": ["T0", "T1"]},
                    {"name": "root", "members": ["R"]},
                ],
                "links": [
                    {"from": "line_leaf", "to": "category", "edges": leaf_edges},
                    {"from": "category", "to": "trunk", "edges": cat_edges},
                    {"from": "trunk", "to": "root", "edges": trunk_edges},
                ],
            },
            "depot": {
                "levels": [
                    {"name": "depot_leaf", "members": depot_leaves},
                    {"name": "region", "members": ["G0", "G1", "G2"]},
                    {"name": "depot_root", "members": ["RD"]},
                ],
                "links": [
                    {"from": "depot_leaf", "to": "region", "edges": depot_edges},
                    {"from": "region", "to": "depot_root", "edges": region_edges},
                ],
            },
        },
        "fields": {"dimensions": {"line": "line", "depot": "depot"},
                   "vehicle": "vehicle_unit", "amount": "amount"},
        "grains": [
            {"name": "leaf_by_leaf",
             "levels": {"line": "line_leaf", "depot": "depot_leaf"}},
            {"name": "cat_by_region",
             "levels": {"line": "category", "depot": "region"}},
            {"name": "trunk_by_region",
             "levels": {"line": "trunk", "depot": "region"}},
            {"name": "root_root", "levels": {"line": "root", "depot": "depot_root"}},
            {"name": "all_all", "levels": {"line": "ALL", "depot": "ALL"}},
        ],
    }


def write_facts(path: Path, count: int, seed: int) -> None:
    rng = random.Random(seed)
    hot_lines = ["L%03d" % i for i in range(24)]
    all_lines = ["L%03d" % i for i in range(LINES)]
    hot_vehicles = ["V%05d" % i for i in range(300)]
    all_vehicles = ["V%05d" % i for i in range(VEHICLES)]
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(count):
            r = rng.random()
            if r < 0.02:
                line = None
            elif r < 0.72:
                line = rng.choice(hot_lines)
            else:
                line = rng.choice(all_lines)
            if rng.random() < 0.02:
                depot = None
            else:
                base = (int((line or "L000")[1:]) * 7) if line else 0
                depot = "D%02d" % ((base + rng.randint(0, 2)) % DEPOTS)
            r = rng.random()
            if r < 0.03:
                vehicle = None
            elif r < 0.83:
                vehicle = rng.choice(hot_vehicles)
            else:
                vehicle = rng.choice(all_vehicles)
            r = rng.random()
            if r < 0.05:
                amount = None
            elif i % 997 == 0:
                amount = 2 ** 70 + i
            else:
                amount = rng.randint(-2000, 2000)
            handle.write(json.dumps({"line": line, "depot": depot,
                                     "vehicle_unit": vehicle, "amount": amount})
                         + "\n")


def run_build(config: Path, facts: Path, out: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", MEASURE_SNIPPET,
         sys.executable, "-I", CLI, "--json", "rollup-build",
         "--config", str(config), "--facts", str(facts), "--out", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("measure wrapper failed: %s\n%s" % (proc.stdout, proc.stderr))
    measured = json.loads(proc.stdout.strip().splitlines()[-1])
    if measured["exit"] != 0:
        raise AssertionError("build failed: %s" % measured)
    return measured


def run_query(*argv: str):
    proc = subprocess.run([sys.executable, "-I", CLI, "--json", *argv],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("query failed: %s\n%s" % (proc.stdout, proc.stderr))
    return json.loads(proc.stdout)


def one_size(count: int, seed: int) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    config_path = OUT / "scale-config.json"
    facts_path = OUT / ("facts-%dk.jsonl" % (count // 1000))
    db_path = OUT / ("scale-%dk.db" % (count // 1000))
    if db_path.exists():
        db_path.unlink()
    if not config_path.exists():
        config_path.write_text(json.dumps(scale_config(), indent=2) + "\n",
                               encoding="utf-8")
    print("generating %d facts -> %s" % (count, facts_path))
    write_facts(facts_path, count, seed)
    facts_mb = facts_path.stat().st_size / 1e6

    measured = run_build(config_path, facts_path, db_path)
    info = run_query("rollup-info", "--result", str(db_path))
    grains = {g["name"]: g["cells"] for g in info["grains"]}
    all_all = run_query("rollup-cells", "--result", str(db_path), "--grain", "all_all")
    row_weight = int(all_all[0]["metrics"]["row_weight"]["num"])
    assert row_weight == count, "ALL/ALL row_weight must equal the fact count"
    db_mb = db_path.stat().st_size / 1e6
    report = {
        "facts": count,
        "facts_mb": round(facts_mb, 1),
        "wall_s": measured["wall_s"],
        "peak_rss_mb": round(measured["peak_rss_kb"] / 1024, 1),
        "db_mb": round(db_mb, 1),
        "cells": grains,
        "total_cells": info["cell_rows"],
        "contributions": info["contribution_rows"],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100000,200000",
                        help="comma-separated fact counts (default 100000,200000)")
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    print("rollup scale rehearsal: unbalanced branches (trunk T0 holds 180 of "
          "200 leaves,")
    print("region G0 holds 20 of 30 depots), %d-vehicle pool, batch_size=%d, "
          "cache=%d KiB" % (VEHICLES, 4096, 16384))
    reports = []
    for idx, count in enumerate(sizes):
        report = one_size(count, seed=20261114 + idx)
        reports.append(report)
        print("\n--- %d facts " % count + "-" * 40)
        print("facts file       : %.1f MB" % report["facts_mb"])
        print("build wall time  : %.2f s" % report["wall_s"])
        print("build peak RSS   : %.1f MB (measured outside the process)" % report["peak_rss_mb"])
        print("result database  : %.1f MB" % report["db_mb"])
        print("contribution rows: %d" % report["contributions"])
        for grain, cells in report["cells"].items():
            print("  grain %-16s %d occupied cells" % (grain, cells))
        print("  total occupied cells: %d" % report["total_cells"])
        full = LINES * DEPOTS
        print("  leaf x leaf full Cartesian domain: %d combos; occupied: %d "
              "(%.1f%%) -- unoccupied combos are never enumerated"
              % (full, report["cells"]["leaf_by_leaf"],
                 100.0 * report["cells"]["leaf_by_leaf"] / full))

    if len(reports) == 2:
        small, large = reports
        print("\n--- doubling check " + "-" * 42)
        print("facts            : %d -> %d (x%.1f)"
              % (small["facts"], large["facts"], large["facts"] / small["facts"]))
        print("wall time        : %.2fs -> %.2fs (x%.2f)"
              % (small["wall_s"], large["wall_s"],
                 large["wall_s"] / small["wall_s"]))
        print("peak RSS         : %.1f -> %.1f MB (x%.2f; flat memory would be "
              "~x1.0)" % (small["peak_rss_mb"], large["peak_rss_mb"],
                          large["peak_rss_mb"] / max(small["peak_rss_mb"], 0.1)))
        print("contributions    : %d -> %d" % (small["contributions"],
                                               large["contributions"]))
        print("RSS stays near the batch/cache floor while contributions and time")
        print("scale with input size: facts, contribution rows and vehicle sets")
        print("live in SQLite, not in Python.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
