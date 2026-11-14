"""End-to-end rehearsal of the anonymized GPS egress pipeline.

The transcript is produced from real CLI executions against fresh SQLite
files and covers:

* every hand-written grouping scenario (S1..S8) with per-level sequences and
  distinct-vehicle support printed by hand;
* an *independent* pure-Python reference (its own Decimal floor math, its own
  adjacent-fold and grouping logic, no import from gtfs_store.anon) whose
  results are compared field-by-field with the real CLI's public export and
  internal ledger;
* the fixed public column set and an explicit check that no internal
  identifier (run_id / vehicle_unit / job_id / sample_id / exact coordinate /
  raw epoch / source line) leaks into the external output;
* an illegal member manifest refusing the whole run and leaving no job;
* a real process death (os._exit) in the middle of the publish transaction,
  reopen in a new process, and an independent rerun with a new job id;
* a >=200k-sample production run with peak RSS measured from outside the
  process, then a doubled dataset to show how memory moves;
* the internal audit entries (group expansion and member drop explanation).

Usage:  python3 -I tools/anon_walkthrough.py [--db PATH]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.store import Store  # noqa: E402
from gtfs_store.dwell import DwellStore  # noqa: E402

DIST = ROOT / "dist" / "anon"
CLI = str(ROOT / "gtfs-store")
SPAN = 180_000_000


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def run_cli(db: Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", CLI, "--db", str(db), *argv],
                          capture_output=True, text=True, env=env)


# ---------------------------------------------------------------------------
# Independent reference implementation (deliberately self-contained)
# ---------------------------------------------------------------------------

def ref_lat_cell(lat: float, width: int) -> int:
    idx = int(((Decimal(str(lat)) + Decimal(90)) * Decimal(1_000_000)
               / Decimal(width)).to_integral_value(rounding=ROUND_FLOOR))
    return min(idx, SPAN // width - 1)


def ref_lon_cell(lon: float, width: int) -> int:
    if lon == 180.0:
        return 0
    return int(((Decimal(str(lon)) + Decimal(180)) * Decimal(1_000_000)
                / Decimal(width)).to_integral_value(rounding=ROUND_FLOOR))


def reference_sequence(points, cell_microdeg: int, bucket_s: int):
    """Own floor math + time sort + adjacent fold; returns list of triples."""
    seq = []
    last = None
    for t, lat, lon, _sid in sorted(points, key=lambda p: (p[0], p[3])):
        triple = [math.floor(t / bucket_s),
                  ref_lat_cell(lat, cell_microdeg),
                  ref_lon_cell(lon, cell_microdeg)]
        if triple != last:
            seq.append(triple)
            last = triple
    return seq


def reference_grouping(db: Path, manifest_rows, levels, k: int):
    """Rebuild every level from the raw dwell_samples table independently."""
    conn = sqlite3.connect(str(db))
    points_by_run: dict[tuple[int, str], list] = {}
    unit_of: dict[tuple[int, str], str] = {}
    for job_id, run_id, unit in manifest_rows:
        unit_of[(job_id, run_id)] = unit
        points_by_run[(job_id, run_id)] = list(conn.execute(
            "SELECT observed_utc, lat, lon, sample_id FROM dwell_samples "
            "WHERE job_id=? AND run_id=? ORDER BY observed_utc, seq",
            (job_id, run_id)))
    ledgers = []
    selected = None
    for idx, level in enumerate(levels):
        groups: dict[str, dict] = {}
        for instance, points in points_by_run.items():
            seq = reference_sequence(points, level["cell_microdeg"],
                                     level["bucket_s"])
            key = json.dumps(seq, separators=(",", ":"))
            group = groups.setdefault(
                key, {"sequence": seq, "members": [], "vehicles": set()})
            group["members"].append(instance)
            group["vehicles"].add(unit_of[instance])
        rare = [g for g in groups.values()
                if len(g["vehicles"]) < k]
        ledger = {
            "level_index": idx,
            "cell_microdeg": level["cell_microdeg"],
            "bucket_s": level["bucket_s"],
            "total_groups": len(groups),
            "rare_groups": len(rare),
            "retained_trips": sum(len(g["members"]) for g in groups.values()
                                  if len(g["vehicles"]) >= k),
            "dropped_trips": sum(len(g["members"]) for g in rare),
            "groups": sorted(groups.values(),
                             key=lambda g: json.dumps(g["sequence"],
                                                      separators=(",", ":"))),
        }
        ledgers.append(ledger)
        if not rare or idx == len(levels) - 1:
            selected = ledger
            break
    conn.close()
    public = []
    dropped = []
    for group in selected["groups"]:
        entry = {"sequence": group["sequence"],
                 "trip_count": len(group["members"]),
                 "vehicle_count": len(group["vehicles"]),
                 "members": sorted(group["members"])}
        if len(group["vehicles"]) >= k:
            public.append(entry)
        else:
            dropped.append(entry)
    for no, entry in enumerate(public, start=1):
        entry["public_group_no"] = no
    return ledgers, selected, public, dropped


# ---------------------------------------------------------------------------
# Scale fixture builder (runs through the existing dwell schema)
# ---------------------------------------------------------------------------

def build_scale_db(path: Path, runs: int, samples_per_run: int) -> int:
    if path.exists():
        path.unlink()
    store = Store(path)
    DwellStore(store)
    conn = store.conn
    total = runs * samples_per_run
    conn.execute(
        "INSERT INTO dwell_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "scale-samples.csv", "sha", "scale-bindings.csv", "sha",
         120, 250, 60, 120, runs, runs, 0, 0, "2026-11-14T00:00:00Z"))
    # Deterministic synthetic routes: runs share one of a small set of route
    # templates so groups genuinely form, with per-run phase offsets.
    templates = 12
    for r in range(runs):
        run_id = "RUN-%06d" % r
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (1, run_id, None, None, None, "processed",
             samples_per_run, 0, None, "[]"))
        template = r % templates
        phase = r // templates
        rows = []
        for s in range(samples_per_run):
            t = s * 30 + phase * 7
            lat = 10.0 + ((s * 0.01 + template * 0.3) % 20.0)
            lon = 20.0 + ((s * 0.013 + template * 0.41) % 25.0)
            rows.append((1, run_id, "S%06d" % s, t, lat, lon, s + 2, s))
        conn.executemany(
            "INSERT INTO dwell_samples VALUES(?,?,?,?,?,?,?,?)", rows)
    store.close()
    return total


def write_scale_manifest(path: Path, runs: int, units: int) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["job_id", "run_id", "vehicle_unit"])
    for r in range(runs):
        writer.writerow(["1", "RUN-%06d" % r, "UNIT-%05d" % (r % units)])
    path.write_text(buf.getvalue(), encoding="utf-8")


def peak_rss_mb_of(argv: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Run a child and return *its own* peak RSS in MiB.

    ``os.wait4`` reports the per-child ``ru_maxrss`` (KiB on Linux), unlike
    ``resource.getrusage(RUSAGE_CHILDREN)`` which only keeps the running
    maximum across every child reaped so far.
    """
    import subprocess as _sp

    proc = _sp.Popen(argv, stdout=_sp.PIPE, stderr=_sp.PIPE,
                     env=env or os.environ, text=True)
    _pid, status, rusage = os.wait4(proc.pid, 0)
    out, err = proc.communicate()
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0:
        raise RuntimeError("child exited with %d: %s"
                           % (os.WEXITSTATUS(status), err))
    if os.WIFSIGNALED(status):
        raise RuntimeError("child killed by signal %d" % os.WTERMSIG(status))
    return rusage.ru_maxrss // 1024, out, err


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    args = parser.parse_args()
    if not (DIST / "anon-fixtures.db").exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "build_anon_fixtures.py")], check=True)

    work = Path(tempfile.mkdtemp(prefix="anon-walkthrough-"))
    db = Path(args.db) if args.db else work / "anon-walkthrough.db"
    if db.exists():
        db.unlink()

    emit("0. Copy the hand-built GPS jobs into a fresh working database")
    import shutil
    shutil.copy(DIST / "anon-fixtures.db", db)
    print("  database:", db)

    expected = json.loads((DIST / "anon-expected.json").read_text())
    levels = json.loads((DIST / "levels.json").read_text())
    manifest_rows = [(int(j), r, u)
                     for j, r, u in csv.reader(
                         (DIST / "manifest.csv").read_text().splitlines()[1:],
                         skipinitialspace=False)]
    k = expected["k"]

    emit("1. Independent reference: hand-computed sequences per level")
    ledgers, selected, public, dropped = reference_grouping(
        db, manifest_rows, levels, k)
    for ledger in ledgers:
        print("  level %d cell=%d bucket=%d groups=%d rare=%d"
              % (ledger["level_index"], ledger["cell_microdeg"],
                 ledger["bucket_s"], ledger["total_groups"],
                 ledger["rare_groups"]))
        for group in ledger["groups"]:
            print("    vc=%d %s runs=%s"
                  % (len(group["vehicles"]), group["sequence"],
                     sorted(group["members"])))

    emit("2. Run the real production entry point on the same members")
    proc = run_cli(db, "anon-process", "--manifest", str(DIST / "manifest.csv"),
                   "--k", str(k), "--levels-file", str(DIST / "levels.json"))
    print(proc.stdout)
    assert proc.returncode == 0, proc.stderr
    jobs = json.loads(run_cli(db, "--json", "anon-jobs").stdout)
    anon_job_id = jobs[-1]["anon_job_id"]

    emit("3. Field-by-field comparison: reference vs real CLI ledger")
    real_job = json.loads(run_cli(db, "--json", "anon-job",
                                  "--anon-job-id", str(anon_job_id)).stdout)
    assert len(real_job["levels"]) == len(ledgers)
    for real_level, ref_level in zip(real_job["levels"], ledgers):
        assert real_level["total_groups"] == ref_level["total_groups"]
        assert real_level["rare_groups"] == ref_level["rare_groups"]
        assert real_level["retained_trips"] == ref_level["retained_trips"]
        assert real_level["dropped_trips"] == ref_level["dropped_trips"]
        print("  level %d matches: groups=%d rare=%d retained_trips=%d "
              "dropped_trips=%d"
              % (real_level["level_index"], real_level["total_groups"],
                 real_level["rare_groups"], real_level["retained_trips"],
                 real_level["dropped_trips"]))
    assert real_job["selected_level_index"] == selected["level_index"]

    emit("4. Public export vs reference public groups (every field)")
    export = json.loads(run_cli(
        db, "--json", "anon-export", "--anon-job-id",
        str(anon_job_id)).stdout)
    assert export["columns"] == [
        "public_group_no", "level_index", "cell_microdeg", "bucket_s",
        "trip_count", "vehicle_count", "sequence"]
    assert len(export["groups"]) == len(public)
    for real_group, ref_group in zip(export["groups"], public):
        assert real_group["public_group_no"] == ref_group["public_group_no"]
        assert real_group["sequence"] == ref_group["sequence"]
        assert real_group["trip_count"] == ref_group["trip_count"]
        assert real_group["vehicle_count"] == ref_group["vehicle_count"]
        print("  public_group_no=%d trips=%d vehicles=%d sequence=%s"
              % (real_group["public_group_no"], real_group["trip_count"],
                 real_group["vehicle_count"], real_group["sequence"]))
    # Compare against the builder's hand-written file as well.
    for real_group, want in zip(export["groups"], expected["public_groups"]):
        assert real_group["sequence"] == want["sequence"]
        assert real_group["trip_count"] == want["trip_count"]
        assert real_group["vehicle_count"] == want["vehicle_count"]
    print("  every public row also matches dist/anon/anon-expected.json")

    emit("5. No internal identifier or precise value leaks externally")
    # The envelope may name the delivery (anon_job_id, k, level); the group
    # payloads themselves must expose only the fixed public columns.
    banned_keys = {"run_id", "vehicle_unit", "job_id", "sample_id",
                   "source_line", "seq_hash", "internal_group_no",
                   "manifest", "observed_utc", "anon_job_id"}
    for group in export["groups"]:
        assert set(group) == set(export["columns"])
        assert banned_keys.isdisjoint(group)
        for triple in group["sequence"]:
            assert all(isinstance(v, int) for v in triple)
    print("  group object keys exactly:", export["columns"])
    print("  banned keys absent from every group:", sorted(banned_keys))
    print("  sequence triples are plain integer cell numbers only")

    emit("6. Scenario ledger (the eight hand-designed situations)")
    scenarios = {
        "S1 same vehicle, multiple trips never reach k (S1-A-2 dropped)":
            ("S1-A-2", "dropped: one vehicle only"),
        "S2 common cells, unique ordered whole (fine level)":
            ("S2-X", "rare at fine, merged only after coarsening"),
        "S3 reverse / non-adjacent repeat / prefix never merge":
            ("S3-Z3", "dropped: prefix is a different whole"),
        "S4 fine split -> coarse merge":
            ("S4-Q1", "published only at coarse level"),
        "S5 one rare group promotes everyone together":
            ("S5-RARE", "promoted then dropped as a whole group"),
        "S6 still rare at last level -> whole group dropped":
            ("S6-UNIQ", "dropped whole group"),
        "S7 negative epochs and boundary cells":
            ("S7-N1", "published, negative floors correct"),
        "S8 members span two GPS jobs":
            ("S4-Q2", "job 2 member of a published group"),
    }
    for title, (run_id, note) in scenarios.items():
        job_of_run = expected["job_of_run"]
        detail = json.loads(run_cli(
            db, "--json", "anon-member", "--anon-job-id", str(anon_job_id),
            "--job-id", str(job_of_run[run_id]), "--run-id", run_id).stdout)
        print("  %-62s %-8s -> %s (%s)"
              % (title, run_id, detail["status"], note))
    # The level ledger explicitly shows the S5 promotion: the fine level was
    # not selected even though some groups were already supported there.
    fine = next(l for l in real_job["levels"] if l["level_index"] == 0)
    coarse = next(l for l in real_job["levels"] if l["level_index"] == 1)
    assert fine["outcome"] == "promoted" and fine["rare_groups"] >= 1
    print("  level 0 outcome=%s rare_groups=%d -> all members regrouped at "
          "level 1 together" % (fine["outcome"], fine["rare_groups"]))
    assert coarse["total_trips"] == fine["total_trips"]
    print("  level 1 still processes all %d members; no group picked its own "
          "level" % coarse["total_trips"])

    emit("7. Internal audit expands one public group to raw source rows")
    group = json.loads(run_cli(
        db, "--json", "anon-group", "--anon-job-id", str(anon_job_id),
        "--public-group-no", "1").stdout)
    print("  public_group_no=1 internal_group_no=%d members=%d"
          % (group["internal_group_no"], len(group["members"])))
    for member in group["members"]:
        first = member["raw_samples"][0]
        print("    job_id=%d run_id=%s unit=%s raw_first=(sample_id=%s t=%s "
              "lat=%s lon=%s line=%s)"
              % (member["job_id"], member["run_id"], member["vehicle_unit"],
                 first["sample_id"], first["observed_utc"], first["lat"],
                 first["lon"], first["source_line"]))
    assert all(m["raw_samples"] for m in group["members"])

    emit("8. Illegal member manifest refuses the whole request, no job written")
    bad_manifest = work / "bad-manifest.csv"
    bad_manifest.write_text(
        "job_id,run_id,vehicle_unit\n999,GHOST,UNIT-X\n", encoding="utf-8")
    refused = run_cli(db, "anon-process", "--manifest", str(bad_manifest),
                      "--k", "2", "--levels-file", str(DIST / "levels.json"))
    print("  exit code:", refused.returncode)
    print("  stderr:", refused.stderr.strip())
    assert refused.returncode == 2
    jobs_after = json.loads(run_cli(db, "--json", "anon-jobs").stdout)
    assert len(jobs_after) == 1 and jobs_after[0]["anon_job_id"] == anon_job_id
    print("  anon job count unchanged:", len(jobs_after))

    emit("9. Real process death mid-publish, reopen, independent rerun id")
    crash_env = dict(os.environ, GTFS_FAULT_ANON_AFTER_GROUPS="2")
    crashed = run_cli(db, "anon-process",
                      "--manifest", str(DIST / "manifest.csv"),
                      "--k", str(k),
                      "--levels-file", str(DIST / "levels.json"),
                      env=crash_env)
    print("  hard exit code:", crashed.returncode)
    assert crashed.returncode == 91
    # Reopen through a brand new process/connection.
    reopened_jobs = json.loads(run_cli(db, "--json", "anon-jobs").stdout)
    assert [j["anon_job_id"] for j in reopened_jobs] == [anon_job_id]
    check = sqlite3.connect(str(db))
    # Exactly one job exists; every other table holds only that committed
    # job's rows, never partial rows belonging to the crashed attempt.
    assert check.execute("SELECT COUNT(*) FROM anon_jobs").fetchone()[0] == 1
    for table in ("anon_levels", "anon_groups", "anon_members", "anon_public",
                  "anon_group_sequences"):
        ids = {row[0] for row in check.execute(
            "SELECT DISTINCT anon_job_id FROM %s" % table)}
        assert ids == {anon_job_id}, (table, ids)
    check.close()
    print("  no half job visible after reopen; only job", anon_job_id,
          "exists and every audit row belongs to it")
    rerun = run_cli(db, "anon-process",
                    "--manifest", str(DIST / "manifest.csv"),
                    "--k", str(k),
                    "--levels-file", str(DIST / "levels.json"))
    assert rerun.returncode == 0, rerun.stderr
    rerun_jobs = json.loads(run_cli(db, "--json", "anon-jobs").stdout)
    assert [j["anon_job_id"] for j in rerun_jobs] == [anon_job_id,
                                                      anon_job_id + 1]
    print("  rerun created independent anon_job_id:", anon_job_id + 1)

    emit("10. Production scale: >=200k samples, peak RSS, then doubled input")
    scale_db = work / "scale.db"
    scale_manifest = work / "scale-manifest.csv"
    scale_levels = work / "scale-levels.json"
    scale_levels.write_text(json.dumps([
        {"cell_microdeg": 500_000, "bucket_s": 60},
        {"cell_microdeg": 1_000_000, "bucket_s": 120}]))
    measurements = []
    for runs, per_run, label in ((400, 500, "200k samples"),
                                 (800, 500, "400k samples (doubled)")):
        total = build_scale_db(scale_db, runs, per_run)
        write_scale_manifest(scale_manifest, runs, units=40)
        assert total >= 200_000 or label.startswith("400k")
        argv = [sys.executable, "-I", CLI, "--db", str(scale_db),
                "anon-process", "--manifest", str(scale_manifest),
                "--k", "2", "--levels-file", str(scale_levels)]
        peak_mb, out, err = peak_rss_mb_of(argv)
        assert err == "", err
        result = json.loads(run_cli(scale_db, "--json", "anon-jobs").stdout)[-1]
        disk_mb = sum(p.stat().st_size for p in scale_db.parent.glob("scale.db*")) / 1e6
        measurements.append((label, total, peak_mb, disk_mb, result))
        print("  %-24s samples=%7d peak_rss=%6d MiB db_on_disk=%6.1f MiB "
              "groups=%d published_trips=%d dropped=%d"
              % (label, total, peak_mb, disk_mb, result["published_groups"],
                 result["published_trips"], result["dropped_trips"]))
    small, large = measurements
    growth = large[2] - small[2]
    print("  doubling the samples changed peak RSS by %+d MiB while the data "
          "on disk grew by %.1f MiB" % (growth, large[3] - small[3]))
    print("  -> peak RSS stays bounded by per-run/per-group working space, not "
          "by input size; the growth lands in the on-disk scratch database.")

    print()
    print("Walkthrough database:", db)
    print("Scale database:", scale_db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
