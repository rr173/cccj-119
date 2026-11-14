"""Build hand-crafted fixtures for the anonymized GPS egress pipeline.

The artefacts are synthetic GPS jobs written through the *existing* dwell
intake (no new import path), plus a member manifest and a grid-level config.
Every timeline below is designed so the resulting coarse cell sequences can
be computed by hand; ``dist/anon/anon-expected.json`` records the intended
per-level sequences, distinct-vehicle support counts and the final public
rows, and ``tools/anon_walkthrough.py`` recomputes them with an independent
pure-Python implementation and compares every field with the real CLI output.

Scenarios covered (each is a named set of runs):

* S1 same vehicle, several trips -> one vehicle can never reach k=2;
* S2 every cell individually common but the ordered whole path is unique;
* S3 reversed order, non-adjacent repetition and a mere prefix never merge;
* S4 fine cells keep paths apart, coarse cells merge them successfully;
* S5 one rare group forces *all* members to be promoted together;
* S6 a group still rare at the last level is dropped as a whole group;
* S7 negative epochs (floor toward -inf) and exact grid-boundary points;
* S8 members registered across two different GPS jobs.

Run from the repository root:  python3 -I tools/build_anon_fixtures.py
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))

from gtfs_store.store import Store  # noqa: E402
from gtfs_store.dwell import DwellStore  # noqa: E402

DIST = ROOT / "dist" / "anon"

# Grid ladder shared by every scenario.  Fine cells are half a degree
# (500_000 microdeg) with 60 s buckets; coarse cells double both dimensions.
LEVELS = [
    {"cell_microdeg": 500_000, "bucket_s": 60},
    {"cell_microdeg": 1_000_000, "bucket_s": 120},
]
K = 2


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def insert_dwell_job(db_path: Path, job_number: int, source: str,
                     runs: dict[str, list[tuple[int, float, float, str]]]) -> None:
    """Create a dwell GPS job directly through the existing dwell schema.

    The anonymization pipeline is only allowed to read dwell_jobs /
    dwell_job_runs / dwell_samples, so the builder populates exactly those
    tables (the dwell state machine itself is intentionally bypassed: these
    are synthetic raw samples, not dwell facts).
    """
    store = Store(db_path)
    DwellStore(store)  # ensure the dwell DDL exists
    conn = store.conn
    total = len(runs)
    conn.execute(
        "INSERT INTO dwell_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_number, source, "sha:%s" % source, "bindings-%d.csv" % job_number,
         "sha:b%d" % job_number, 120, 250, 60, 120, total, total, 0, 0,
         "2026-11-14T00:00:00Z"))
    for run_id, points in runs.items():
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_number, run_id, None, None, None, "processed",
             len(points), 0, None, "[]"))
        for seq, (observed_utc, lat, lon, sample_id) in enumerate(points):
            conn.execute(
                "INSERT INTO dwell_samples VALUES(?,?,?,?,?,?,?,?)",
                (job_number, run_id, sample_id, observed_utc, lat, lon,
                 seq + 2, seq))
    store.close()


# ---------------------------------------------------------------------------
# Hand-designed timelines.  Each point is (observed_utc, lat, lon, sample_id).
# ---------------------------------------------------------------------------

def build_runs() -> dict[str, list[tuple[int, float, float, str]]]:
    runs: dict[str, list[tuple[int, float, float, str]]] = {}

    # S1: one vehicle (VA) drives two trips with identical shapes.  The group
    # has two runs but only one distinct vehicle, so it stays rare at k=2 and
    # is dropped at the last level.
    s1 = [(3600, 52.0, 13.0, "a"), (3660, 52.4, 13.4, "b"),
          (3720, 52.6, 13.6, "c")]
    runs["S1-A-1"] = s1
    runs["S1-A-2"] = [(t + 10_000, la, lo, sid) for t, la, lo, sid in s1]

    # S2: the "common cells, unique whole path" trap.  X and Y visit the same
    # two fine cells in opposite orders.  Any per-cell head count sees two
    # vehicles per cell; the ordered whole sequences differ, so each path has
    # support 1 at the fine level.  At the coarse level the two merge into one
    # two-vehicle group (the scenario still proves no per-cell shortcut can
    # publish them separately at the fine level).
    runs["S2-X"] = [(3600, 52.0, 13.0, "a"), (3660, 52.0, 13.0, "a2"),
                    (3720, 52.6, 13.6, "b")]
    runs["S2-Y"] = [(3600, 52.6, 13.6, "b"), (3660, 52.6, 13.6, "b2"),
                    (3720, 52.0, 13.0, "a")]

    # S3: ordering and repetition matter.  Z1 and the Z2/Z3 shapes share cells
    # but never produce identical ordered wholes: Z2 revisits a cell
    # non-adjacently (A,B,A,B), while Z3 is only a prefix of Z1 (A,B).  Z1's
    # exact sequence gets no second vehicle, so it stays rare; Z2 and Z3 are
    # unique as well and all three land in separate groups at every level.
    runs["S3-Z1"] = [(3600, 52.0, 13.0, "a"), (3660, 52.4, 13.4, "b"),
                     (3720, 52.0, 13.0, "a2")]
    runs["S3-Z2"] = [(3600, 52.0, 13.0, "a"), (3660, 52.4, 13.4, "b"),
                     (3720, 52.0, 13.0, "a2"), (3780, 52.4, 13.4, "b2")]
    runs["S3-Z3"] = [(3600, 52.0, 13.0, "a"), (3660, 52.4, 13.4, "b")]

    # S4: fine vs coarse.  Q1 and Q2 differ at fine level because one point
    # sits just across the 52.5 half-degree line; the 1-degree coarse level
    # merges them.  Two distinct vehicles make the merged group publishable.
    runs["S4-Q1"] = [(4000, 52.0, 13.0, "a"), (4060, 52.4, 13.0, "b"),
                     (4120, 52.6, 13.6, "c")]
    runs["S4-Q2"] = [(4000, 52.0, 13.0, "a"), (4060, 52.6, 13.0, "bjit"),
                     (4120, 52.6, 13.6, "c")]

    # S5: a publishable group plus one rare group.  At the fine level the
    # common group already has two vehicles, but the rare singleton forces
    # *every* member up to the coarse level (groups may not pick levels
    # individually).  The singleton's cells are deliberately unique at both
    # levels so it is still rare after promotion, which also feeds S6.
    runs["S5-P1"] = [(5000, 53.0, 14.0, "a"), (5060, 53.4, 14.4, "b"),
                     (5120, 53.6, 14.6, "c")]
    runs["S5-P2"] = [(5000, 53.0, 14.0, "a"), (5060, 53.4, 14.4, "b"),
                     (5120, 53.6, 14.6, "c")]
    runs["S5-RARE"] = [(5000, 60.0, 20.0, "x"), (5060, 60.6, 20.6, "y"),
                       (5120, 61.2, 21.2, "z")]

    # S6: a genuinely unique itinerary stays rare even at the coarsest level,
    # while the common group next to it survives.  The whole rare group is
    # dropped together (both of its member runs, never a per-run filtering).
    runs["S6-U1"] = [(6000, 54.0, 15.0, "a"), (6060, 54.4, 15.4, "b"),
                     (6120, 54.6, 15.6, "c")]
    runs["S6-U2"] = [(6000, 54.0, 15.0, "a"), (6060, 54.4, 15.4, "b"),
                     (6120, 54.6, 15.6, "c")]
    runs["S6-UNIQ"] = [(6000, -33.0, 150.0, "x"), (6060, -33.6, 150.6, "y"),
                       (6120, -34.2, 151.2, "z")]

    # S7: negative epoch seconds and exact boundary points.
    #   t=-30 -> bucket floor(-30/60) = -1 ; t=-60 -> bucket -1 (boundary)
    #   lat -90 -> cell 0 ; lat -89.5 -> cell 1 ; lon 180 -> seam cell 0
    runs["S7-N1"] = [(-30, -90.0, -180.0, "a"), (-60, -89.5, -179.5, "b"),
                     (-120, -89.0, -179.0, "c")]
    runs["S7-N2"] = [(-30, -90.0, -180.0, "a"), (-60, -89.5, -179.5, "b"),
                     (-120, -89.0, -179.0, "c")]

    return runs


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    db_path = DIST / "anon-fixtures.db"
    if db_path.exists():
        db_path.unlink()

    runs = build_runs()
    # S8: split the membership across two GPS jobs.  Job 1 takes most runs;
    # job 2 takes one run of the S4 pair and one run of the S6 pair.
    job2_runs = {run_id: runs.pop(run_id) for run_id in ("S4-Q2", "S6-U2")}
    insert_dwell_job(db_path, 1, "anon-samples-job1.csv", runs)
    insert_dwell_job(db_path, 2, "anon-samples-job2.csv", job2_runs)

    # One manifest registering every instance exactly once.  Vehicle units are
    # opaque strings assigned per scenario; repeated trips of one vehicle
    # share the unit, and "007"/"7" style distinctness is exercised in tests.
    manifest_rows: list[list[str]] = []
    units = {
        "S1-A-1": "VA", "S1-A-2": "VA",
        "S2-X": "VX", "S2-Y": "VY",
        "S3-Z1": "VZ1", "S3-Z2": "VZ2", "S3-Z3": "VZ3",
        "S4-Q1": "VQ1", "S4-Q2": "VQ2",
        "S5-P1": "VP1", "S5-P2": "VP2", "S5-RARE": "VR",
        "S6-U1": "VU1", "S6-U2": "VU2", "S6-UNIQ": "VUNIQ",
        "S7-N1": "VN1", "S7-N2": "VN2",
    }
    for run_id, unit in units.items():
        job_id = 2 if run_id in job2_runs else 1
        manifest_rows.append([str(job_id), run_id, unit])
    write_csv(DIST / "manifest.csv",
              ["job_id", "run_id", "vehicle_unit"], manifest_rows)
    (DIST / "levels.json").write_text(json.dumps(LEVELS, indent=2) + "\n",
                                      encoding="utf-8")

    expected = build_expected(runs | job2_runs, manifest_rows)
    (DIST / "anon-expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print("built %d runs across 2 jobs in %s" % (len(units), db_path))
    print("manifest rows: %d; levels: %s" % (len(manifest_rows), LEVELS))


# ---------------------------------------------------------------------------
# Hand-written expectations
# ---------------------------------------------------------------------------

def build_expected(runs: dict, manifest_rows: list[list[str]]) -> dict:
    """Return the independently computed expected sequences and support."""
    from decimal import ROUND_FLOOR, Decimal
    import math

    def cells(points, cell_microdeg: int, bucket_s: int):
        # Independent reference: chronological order is part of the contract,
        # so the reference sorts explicitly even if the input lists points in
        # another order (S7 is deliberately inserted in reverse time order).
        points = sorted(points, key=lambda p: (p[0], p[3]))
        seq = []
        last = None
        span_cells = 180_000_000 // cell_microdeg
        for t, lat, lon, _sid in points:
            # Explicit floor toward negative infinity, including t < 0.
            b = math.floor(t / bucket_s)
            la = int(((Decimal(str(lat)) + Decimal(90)) * Decimal(1_000_000)
                      / Decimal(cell_microdeg)).to_integral_value(rounding=ROUND_FLOOR))
            la = min(la, span_cells - 1)
            lo_value = lon
            lo = 0 if lo_value == 180.0 else int(
                ((Decimal(str(lo_value)) + Decimal(180)) * Decimal(1_000_000)
                 / Decimal(cell_microdeg)).to_integral_value(rounding=ROUND_FLOOR))
            triple = [b, la, lo]
            if triple != last:
                seq.append(triple)
                last = triple
        return seq

    unit_of = {run_id: unit for _job, run_id, unit in manifest_rows}
    job_of = {run_id: int(job) for job, run_id, _u in manifest_rows}

    levels_out = []
    for idx, level in enumerate(LEVELS):
        per_run = {run_id: cells(points, level["cell_microdeg"],
                                 level["bucket_s"])
                   for run_id, points in runs.items()}
        groups: dict[str, dict] = {}
        for run_id, seq in per_run.items():
            key = json.dumps(seq, separators=(",", ":"))
            group = groups.setdefault(key, {"sequence": seq, "runs": [],
                                            "vehicles": set()})
            group["runs"].append(run_id)
            group["vehicles"].add(unit_of[run_id])
        level_out = []
        for key in sorted(groups):
            group = groups[key]
            level_out.append({
                "sequence": group["sequence"],
                "runs": sorted(group["runs"]),
                "vehicles": sorted(group["vehicles"]),
                "vehicle_count": len(group["vehicles"]),
                "rare": len(group["vehicles"]) < K,
            })
        levels_out.append({"level_index": idx,
                           "cell_microdeg": level["cell_microdeg"],
                           "bucket_s": level["bucket_s"],
                           "groups": level_out})

    # Determine the selected level with the same promotion rule as production.
    selected_index = len(LEVELS) - 1
    for idx, level_out in enumerate(levels_out):
        if not any(g["rare"] for g in level_out["groups"]):
            selected_index = idx
            break
    final_groups = levels_out[selected_index]["groups"]
    public = [g for g in final_groups if not g["rare"]]
    dropped = [g for g in final_groups if g["rare"]]
    return {
        "k": K,
        "levels": levels_out,
        "selected_level_index": selected_index,
        "public_groups": [
            {"public_group_no": no,
             "sequence": g["sequence"],
             "trip_count": len(g["runs"]),
             "vehicle_count": g["vehicle_count"]}
            for no, g in enumerate(public, start=1)],
        "dropped_groups": [
            {"sequence": g["sequence"], "runs": g["runs"],
             "vehicle_count": g["vehicle_count"]} for g in dropped],
        "job_of_run": job_of,
        "unit_of_run": unit_of,
    }


if __name__ == "__main__":
    main()
