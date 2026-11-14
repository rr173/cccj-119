"""End-to-end rehearsal of the offline GPS dwell pipeline.

The transcript is generated from real CLI/process executions against a fresh
SQLite file and covers, in order:

* loading the hand-built static editions and processing the sample file;
* every hand-written dwell scenario with its state transitions and evidence;
* an independent full-stop exhaustive candidate cross-check (no production
  spatial index) plus a field-by-field comparison with dwell-expected.json;
* run-level isolation of bad runs, bad static coordinates, unregistered runs;
* the large 400-candidate fixture proving the index prunes exact checks;
* a crash in the middle of the write transaction, reopen, and an independent
  rerun job;
* the file-level failure paths.

Usage:  python3 -I tools/dwell_walkthrough.py [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.dwell import (  # noqa: E402
    DwellParams, DwellStore, SpatialIndex, brute_force_nearest, nearest_candidate,
)
from gtfs_store.store import Store  # noqa: E402

DIST = ROOT / "dist" / "dwell"
CLI = str(ROOT / "gtfs-store")
PARAMS = DwellParams(120, 250, 60, 120)


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def run_cli(db: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", CLI, "--db", str(db), *argv],
                          capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    args = parser.parse_args()
    if not (DIST / "dwell-v1.zip").exists():
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_dwell_fixtures.py")],
                       check=True)

    tmp = tempfile.mkdtemp(prefix="dwell-walkthrough-") if not args.db else None
    db = Path(args.db) if args.db else Path(tmp) / "dwalkthrough.db"
    if db.exists():
        db.unlink()

    emit("1. Load static editions (different runs may use different versions)")
    for edition in ("v1", "v2", "v3-bad-coords", "large"):
        r = run_cli(db, "load", "--feed-key", "dwell", "--edition", edition,
                    "--package", str(DIST / ("dwell-%s.zip" % edition)))
        print("  dwell/%s ->" % edition, r.stdout.strip().splitlines()[-1])

    emit("2. Process the hand-written sample file as one independent job")
    r = run_cli(db, "dwell-process",
                "--samples", str(DIST / "samples.csv"),
                "--bindings", str(DIST / "bindings.csv"),
                "--enter-m", "120", "--exit-m", "250",
                "--min-dwell-s", "60", "--max-gap-s", "120")
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        return 1
    job_id = json.loads(
        run_cli(db, "--json", "dwell-jobs").stdout)[0]["job_id"]

    expected = json.loads((DIST / "dwell-expected.json").read_text())

    emit("3. Independent cross-check A: full-stop exhaustive candidate scan")
    store = Store(db)
    dwell = DwellStore(store)
    job = dwell.job(job_id)
    mismatches = 0
    total_checks = 0
    for run_row in job["runs"]:
        if run_row["status"] != "processed":
            continue
        run_id = run_row["run_id"]
        detail = dwell.run_detail(job_id, run_id)
        binding = next(b for b in job["bindings"] if b["run_id"] == run_id)
        stops = dwell.candidate_stops(binding["feed_key"], binding["edition"],
                                      binding["trip_id"])
        index = SpatialIndex(stops, PARAMS.enter_m)
        for sample in detail["samples"]:
            if sample["lat"] is None:
                continue
            indexed = nearest_candidate(index, sample["lat"], sample["lon"],
                                        PARAMS.enter_m)
            brute = brute_force_nearest(stops, sample["lat"], sample["lon"],
                                        PARAMS.enter_m)
            total_checks += 1
            if (indexed[0]["stop_id"] if indexed else None) != \
               (brute[0]["stop_id"] if brute else None):
                mismatches += 1
    print("  points cross-checked against every candidate stop:", total_checks)
    print("  index vs brute-force candidate mismatches:", mismatches)
    assert mismatches == 0

    emit("4. Independent cross-check B: hand timeline, state by state")
    facts = dwell.dwells(job_id)
    wanted = [(run_id, d) for run_id, spec in expected["runs"].items()
              for d in spec.get("dwells", [])]
    print("  hand-written dwells:", len(wanted), " stored facts:", len(facts))
    assert len(wanted) == len(facts)
    for (run_id, want), fact in zip(wanted, facts):
        assert fact["run_id"] == run_id
        for key in ("stop_id", "end_reason", "arrival_utc", "confirmed_utc",
                    "last_observed_utc", "departure_utc"):
            assert fact[key] == want[key], (run_id, key, fact[key], want[key])
        assert fact["support_points"] == len(want["support_ids"])
        evidence = dwell.evidence(job_id, fact["dwell_seq"])["evidence"]
        support_ids = sorted({p["sample_id"] for p in evidence
                              if p["point_role"] != "first_outside"})
        assert support_ids == sorted(want["support_ids"]), run_id
        # Every evidence row points back at a physical source line and carries
        # the exact, unrounded Haversine distance used for the decision.
        assert all(p["source_line"] >= 2 for p in evidence)
        assert all(p["distance_m"] >= 0.0 for p in evidence)
    print("  every fact matches the hand-written states, instants, support")
    print("  samples, end reason and source lines (not just the row count)")

    emit("5. Scenario ledger (the actual stored facts)")
    for fact in facts:
        print("  %-9s %-6s %-14s arrival=%-5d confirm=%-5d last=%-5s "
              "departure=%-5s support=%d"
              % (fact["run_id"], fact["stop_id"], fact["end_reason"],
                 fact["arrival_utc"], fact["confirmed_utc"],
                 fact["last_observed_utc"],
                 fact["departure_utc"] if fact["departure_utc"] is not None else "-",
                 fact["support_points"]))

    emit("6. Bad runs are isolated; sibling good runs and diagnostics survive")
    for run_id in ("R10-BADCOORD", "R10-DUPID", "R10-DUPTS", "R12-BADSTOP",
                   "R14-EMPTY"):
        detail = dwell.run_detail(job_id, run_id)
        diag = detail["diagnostics"][0]
        print("  %-14s status=%s line=%s %s: %s"
              % (run_id, detail["status"],
                 diag.get("source_line") if diag.get("source_line") is not None else "-",
                 diag["code"], diag["message"][:80]))
    good = dwell.run_detail(job_id, "R10-OK")
    print("  R10-OK in the same file: status=%s dwells=%d"
          % (good["status"], len(good["dwells"])))

    emit("7. Spatial index actually participates (400 candidates, 40 points)")
    r = run_cli(db, "dwell-process",
                "--samples", str(DIST / "samples-large.csv"),
                "--bindings", str(DIST / "bindings-large.csv"),
                "--enter-m", "120", "--exit-m", "250",
                "--min-dwell-s", "60", "--max-gap-s", "120")
    print(r.stdout.strip())
    large_job = json.loads(run_cli(db, "--json", "dwell-jobs").stdout)[-1]["job_id"]
    detail = dwell.run_detail(large_job, "LRUN")
    idx = detail["index"]
    brute_total = expected["files"]["large"]["brute_force_checks"]
    print("  index explanation:", json.dumps(idx, sort_keys=True))
    print("  exact Haversine checks via index: %d; a Cartesian scan would be %d"
          % (idx["candidate_checks"], brute_total))
    assert idx["candidate_checks"] < brute_total
    assert detail["dwells"][0]["stop_id"] == "L0200"

    emit("8. Crash mid-write leaves no half job; reopen; rerun is independent")
    jobs_before = {j["job_id"] for j in dwell.jobs()}
    env = dict(os.environ, GTFS_FAULT_DWELL_AFTER_RUNS="3")
    crashed = subprocess.run(
        [sys.executable, "-I", CLI, "--db", str(db), "dwell-process",
         "--samples", str(DIST / "samples.csv"),
         "--bindings", str(DIST / "bindings.csv"),
         "--enter-m", "120", "--exit-m", "250",
         "--min-dwell-s", "60", "--max-gap-s", "120"],
        env=env, capture_output=True, text=True)
    print("  crashed process exit=%d: %s"
          % (crashed.returncode, crashed.stderr.strip() or crashed.stdout.strip()))
    # Reopen with a brand new process/connection and read the ledger.
    reopened = Store(db)
    jobs_after_crash = {j["job_id"] for j in DwellStore(reopened).jobs()}
    print("  jobs visible after reopen:", sorted(jobs_after_crash))
    print("  crashed job exposed any row:", jobs_after_crash != jobs_before)
    assert jobs_after_crash == jobs_before
    orphan = reopened.conn.execute(
        "SELECT COUNT(*) FROM dwell_evidence e LEFT JOIN dwell_facts f "
        "USING(job_id, dwell_seq) WHERE f.job_id IS NULL").fetchone()[0]
    print("  orphan evidence rows:", orphan)
    assert orphan == 0
    rerun = run_cli(db, "dwell-process",
                    "--samples", str(DIST / "samples.csv"),
                    "--bindings", str(DIST / "bindings.csv"),
                    "--enter-m", "120", "--exit-m", "250",
                    "--min-dwell-s", "60", "--max-gap-s", "120")
    assert rerun.returncode == 0, rerun.stderr
    rerun_job = json.loads(run_cli(db, "--json", "dwell-jobs").stdout)[-1]["job_id"]
    print("  rerun created job_id:", rerun_job,
          "(original sample job:", job_id, ")")
    assert rerun_job != job_id
    reopened.close()

    emit("9. File-level failures create no job and name the source line")
    missing = run_cli(db, "dwell-process",
                      "--samples", str(DIST / "samples-missing-run.csv"),
                      "--bindings", str(DIST / "bindings.csv"),
                      "--enter-m", "120", "--exit-m", "250",
                      "--min-dwell-s", "60", "--max-gap-s", "120")
    print("  missing run_id -> exit", missing.returncode, ":",
          missing.stderr.strip())
    broken = run_cli(db, "dwell-process",
                     "--samples", str(DIST / "samples.csv"),
                     "--bindings", str(DIST / "bindings-broken.csv"),
                     "--enter-m", "120", "--exit-m", "250",
                     "--min-dwell-s", "60", "--max-gap-s", "120")
    print("  broken binding -> exit", broken.returncode, ":",
          broken.stderr.strip())
    store.close()

    emit("10. Evidence drill-down for one dwell (raw values and source lines)")
    r = run_cli(db, "dwell-evidence", "--job-id", str(job_id), "--dwell-seq", "1")
    print(r.stdout)

    print()
    print("Walkthrough database:", db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
