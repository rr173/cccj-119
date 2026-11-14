"""Scale rehearsal for the three-way dictionary merge.

Generates a shared base draft of at least 100,000 JSONL rows (composite key)
plus two sparse revisions that touch only a small fraction of the keys, then
runs the real CLI entries (./gtfs-store dict-compare / dict-finalize) in
fresh subprocesses and records:

* input row counts and the resulting conflict count;
* compare and finalize wall time;
* peak resident set size of each entry, measured from outside the process
  via getrusage of a wrapper (never from inside the measured code);
* a growth check doubling the base to 200,000 rows: peak RSS must stay near
  its floor while time and on-disk work-library size scale with the input.

The point is that the full row/key/conflict universes live in SQLite: Python
holds one streaming line, one key's three field maps and one bounded batch.

Run:  python3 -I tools/dictmerge_scale.py [--sizes 100000,200000]
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = str(ROOT / "gtfs-store")

KEY = ["org", "code"]
ORGS = ["O%02d" % i for i in range(20)]
SPARSE_STEP = 977      # one sparse touch per ~1k keys
BIG = 2 ** 127 + 7

MEASURE_SNIPPET = """
import json, resource, subprocess, sys, time
start = time.monotonic()
proc = subprocess.run(sys.argv[1:])
wall = time.monotonic() - start
rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({"exit": proc.returncode, "wall_s": round(wall, 3),
                  "peak_rss_kb": rss_kb}))
"""


def write_base(path: Path, count: int) -> None:
    per_org = count // len(ORGS)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        n = 0
        for org in ORGS:
            for i in range(per_org):
                row = {
                    "org": org,
                    "code": "%07d" % i,                # leading zeros preserved
                    "name": "%s stop %d" % (org, i),
                    "category": None if i % 3 == 0 else "C%d" % (i % 4),
                    "seq": i,
                    "flag": i % 2 == 0,
                    "big": BIG + i if i % 1009 == 0 else i,
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
                if n >= count:
                    return


def write_revisions(base_path: Path, left_path: Path, right_path: Path
                    ) -> tuple[int, int]:
    """Two full revision dictionaries with sparse edits.

    The merge contract treats omission as deletion, so each revision carries
    every retained base row: the base streams through unchanged except for a
    small number of sparse edits (left-side renames / a few real deletions,
    right-side classifications).  The full key universe is never held in
    Python even by this generator.
    """
    left_n = right_n = 0
    with open(base_path, encoding="utf-8") as bh, \
            open(left_path, "w", encoding="utf-8", newline="\n") as lh, \
            open(right_path, "w", encoding="utf-8", newline="\n") as rh:
        idx = 0
        for line in bh:
            obj = json.loads(line)
            left_row = dict(obj)
            right_row = dict(obj)
            if idx % SPARSE_STEP == 0:
                # left renames the display name; every seventh touch is a
                # real row deletion instead (the row is omitted on the left).
                if idx % (SPARSE_STEP * 7) == 0:
                    left_row = None
                else:
                    left_row["name"] = "L rename %d" % idx
                    left_row["seq"] = idx
            if idx % (SPARSE_STEP + 13) == 0:
                # right adds a classification field; some rows collide with
                # left on purpose so a handful of real field conflicts appear.
                right_row["category"] = "R%d" % (idx % 9)
                right_row["reviewed"] = True
            if idx % (SPARSE_STEP * 3) == 0:
                # Every third left-rename row: right writes a *different*
                # name too, a genuine field-level conflict.
                right_row["name"] = "R rename %d" % idx
            if left_row is not None:
                lh.write(json.dumps(left_row, ensure_ascii=False) + "\n")
                left_n += 1
            rh.write(json.dumps(right_row, ensure_ascii=False) + "\n")
            right_n += 1
            idx += 1
        # A few brand-new keys on each side, including one add collision.
        for side, n in (("L", 3), ("R", 3)):
            for j in range(n):
                (lh if side == "L" else rh).write(json.dumps({
                    "org": "NEW", "code": "%s-%d" % (side, j),
                    "name": "new %s %d" % (side, j), "v": j}) + "\n")
                if side == "L":
                    left_n += 1
                else:
                    right_n += 1
        # same new key on both sides with different content -> collision
        lh.write(json.dumps({"org": "NEW", "code": "BOTH", "v": 1}) + "\n")
        rh.write(json.dumps({"org": "NEW", "code": "BOTH", "v": 2}) + "\n")
        left_n += 1
        right_n += 1
    return left_n, right_n


def measured(argv: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", MEASURE_SNIPPET, sys.executable, "-I",
         *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("measure wrapper failed: %s\n%s"
                             % (proc.stdout, proc.stderr))
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    if payload["exit"] != 0:
        raise AssertionError("measured command failed: %s" % payload)
    return payload


def run_one(count: int, out_dir: Path) -> dict:
    base = out_dir / ("base-%dk.jsonl" % (count // 1000))
    left = out_dir / ("left-%dk.jsonl" % (count // 1000))
    right = out_dir / ("right-%dk.jsonl" % (count // 1000))
    work = out_dir / ("work-%dk.db" % (count // 1000))
    decisions = out_dir / ("decisions-%dk.jsonl" % (count // 1000))
    final = out_dir / ("final-%dk.jsonl" % (count // 1000))

    t0 = time.monotonic()
    write_base(base, count)
    left_rows, right_rows = write_revisions(base, left, right)
    gen_s = time.monotonic() - t0

    compare_m = measured([
        CLI, "--json", "dict-compare", "--base", str(base),
        "--left", str(left), "--right", str(right),
        "--key", "org", "--key", "code", "--out", str(work)])
    info = json.loads(subprocess.run(
        [sys.executable, "-I", CLI, "--json", "dict-info", "--work",
         str(work)], capture_output=True, text=True).stdout)

    # Resolve every conflict mechanically (side choice or custom) so the
    # finalize entry runs over a fully decided work library.
    conflicts = json.loads(subprocess.run(
        [sys.executable, "-I", CLI, "--json", "dict-conflicts", "--work",
         str(work), "--limit", "1000000"],
        capture_output=True, text=True).stdout)["conflicts"]
    dec_lines = []
    for c in conflicts:
        cid = c["conflict_id"]
        if c["category"] == "field_conflict":
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "custom", "custom": {"value": "scaled"}}
        elif c["category"] == "add_collision":
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "custom",
                   "custom": {"row": {**{k: v for k, v in zip(KEY, c["key"])},
                                      "name": "scaled new"}}}
        else:
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "right"}
        dec_lines.append(json.dumps(dec, ensure_ascii=False))
    decisions.write_text("\n".join(dec_lines) + "\n", encoding="utf-8")

    finalize_m = measured([
        CLI, "--json", "dict-finalize", "--work", str(work),
        "--decisions", str(decisions), "--out", str(final)])

    with open(final, encoding="utf-8") as handle:
        output_rows = sum(1 for line in handle if line.strip())
    report = {
        "base_rows": count,
        "left_rows": left_rows,
        "right_rows": right_rows,
        "conflicts": info["counts"]["conflicts_total"],
        "field_conflicts": info["counts"]["field_conflicts"],
        "row_conflicts": info["counts"]["row_conflicts"],
        "output_rows": output_rows,
        "generate_s": round(gen_s, 2),
        "compare_wall_s": compare_m["wall_s"],
        "compare_peak_rss_mb": round(compare_m["peak_rss_kb"] / 1024, 1),
        "finalize_wall_s": finalize_m["wall_s"],
        "finalize_peak_rss_mb": round(finalize_m["peak_rss_kb"] / 1024, 1),
        "work_db_mb": round(work.stat().st_size / 1e6, 1),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100000,200000")
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    with tempfile.TemporaryDirectory(prefix="dictmerge-scale-") as tmp:
        out_dir = Path(tmp)
        reports = []
        for count in sizes:
            print("running three-way merge over %d-row base ..." % count)
            report = run_one(count, out_dir)
            reports.append(report)
            print(json.dumps(report, indent=2))
        if len(reports) == 2:
            small, large = reports
            print("\n--- doubling check " + "-" * 44)
            print("base rows        : %d -> %d (x%.1f)"
                  % (small["base_rows"], large["base_rows"],
                     large["base_rows"] / small["base_rows"]))
            print("compare wall     : %.2fs -> %.2fs (x%.2f)"
                  % (small["compare_wall_s"], large["compare_wall_s"],
                     large["compare_wall_s"] / small["compare_wall_s"]))
            print("compare peak RSS : %.1f -> %.1f MB (x%.2f; flat memory "
                  "stays near x1.0)"
                  % (small["compare_peak_rss_mb"],
                     large["compare_peak_rss_mb"],
                     large["compare_peak_rss_mb"]
                     / max(small["compare_peak_rss_mb"], 0.1)))
            print("finalize peak RSS: %.1f -> %.1f MB"
                  % (small["finalize_peak_rss_mb"],
                     large["finalize_peak_rss_mb"]))
            print("work library     : %.1f -> %.1f MB"
                  % (small["work_db_mb"], large["work_db_mb"]))
            print("conflicts        : %d -> %d (sparse revisions keep the "
                  "conflict count small and stable)"
                  % (small["conflicts"], large["conflicts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
