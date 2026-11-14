"""Run the documented rehearsal end to end against a fresh SQLite file.

Usage:  python3 -I tools/walkthrough.py [--db PATH]

The output is a transcript of actual queries, not summaries.  It is copied
into docs/WALKTHROUGH.md; regenerating it is cheap and deterministic except
for timestamps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.store import Store  # noqa: E402

DIST = ROOT / "dist"


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def dump(sql: str, db: Path, columns: list[str] | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()
    conn.close()
    if not rows:
        print("  (no rows)")
        return
    columns = columns or list(rows[0].keys())
    print("  " + " | ".join(columns))
    for row in rows:
        print("  " + " | ".join(str(row[c]) for c in columns))


def digest_departures(db: Path, feed: str, edition: str, date_lo: str, date_hi: str) -> str:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT service_date, trip_instance_id, stop_id, stop_sequence, departure_seconds, "
        "departure_utc, departure_local, utc_offset, certainty, min_departure_seconds, "
        "max_departure_seconds, headway_secs FROM departures "
        "WHERE feed_key=? AND edition=? AND service_date BETWEEN ? AND ? "
        "ORDER BY service_date, trip_instance_id, stop_sequence", (feed, edition, date_lo, date_hi))
    h = hashlib.sha256()
    for row in rows:
        h.update(("|".join("" if v is None else str(v) for v in row) + "\n").encode())
    conn.close()
    return h.hexdigest()


def count(db: Path, sql: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    args = parser.parse_args()
    if not DIST.exists() or not (DIST / "demo-v1.zip").exists():
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fixtures.py")],
                       check=True)

    tmp = tempfile.mkdtemp(prefix="gtfs-walkthrough-") if not args.db else None
    db = Path(args.db) if args.db else Path(tmp) / "walkthrough.db"
    if db.exists():
        db.unlink()
    store = Store(db)

    emit("1. Load editions: two versions of one operator, a second operator, three rejected packages")
    for feed, edition, package in [
        ("demo", "v1", DIST / "demo-v1.zip"),
        ("demo", "v2", DIST / "demo-v2.zip"),
        ("other", "v1", DIST / "other-operator.zip"),
        ("demo", "bad-overlap", DIST / "reject-overlap.zip"),
        ("demo", "bad-missing-time", DIST / "reject-missing-time.zip"),
        ("demo", "bad-foreign-key", DIST / "reject-foreign-key.zip"),
    ]:
        result = store.load(feed, edition, package)
        print("%-6s %-16s -> %-8s  %s" % (feed, edition, result["status"], result["message"]))
    store.close()

    emit("2. Edition state as stored (status, digest, timezone)", [])
    dump("SELECT feed_key, edition, status, substr(content_sha256,1,16) AS content_sha, "
         "timezone FROM editions ORDER BY feed_key, edition", db)

    emit("3. Refusal reasons actually recorded (file / line / field)", [])
    dump("SELECT edition, category, file_name, line_number, field, "
         "substr(message,1,70) AS message FROM load_rejections ORDER BY edition, seq", db)

    emit("4. Expand a spring-DST and a fall-DST span for demo/v1", [])
    store = Store(db)
    result = store.expand("demo", "v1", "2026-03-28", "2026-03-30")
    for day in result["days"]:
        print("  %s services=%-32s instances=%3d rows=%3d"
              % (day["service_date"], ",".join(day["enabled_services"]) or "-",
                 day["trip_instances"], day["departure_rows"]))
    result = store.expand("demo", "v1", "2026-10-24", "2026-10-26")
    for day in result["days"]:
        print("  %s services=%-32s instances=%3d rows=%3d"
              % (day["service_date"], ",".join(day["enabled_services"]) or "-",
                 day["trip_instances"], day["departure_rows"]))
    store.close()

    emit("5. Day-start instants (local noon -> UTC -> minus 12 actual hours)", [])
    dump("SELECT service_date, timezone, day_start_utc FROM service_days "
         "ORDER BY service_date", db)

    emit("6. Stop 007 on 2026-03-30: certainty split and late-night rows", [])
    dump("SELECT certainty, COUNT(*) AS rows FROM departures WHERE feed_key='demo' "
         "AND edition='v1' AND service_date='20260330' AND stop_id='007' GROUP BY certainty", db)
    dump("SELECT departure_seconds, departure_local, utc_offset, departure_utc, trip_id, "
         "trip_instance_id FROM departures WHERE feed_key='demo' AND edition='v1' "
         "AND service_date='20260330' AND stop_id='007' "
         "AND departure_seconds >= 84000 ORDER BY departure_seconds", db)

    emit("7. 007 and 7 are different stops with different departures", [])
    dump("SELECT stop_id, COUNT(*) AS rows, MIN(departure_seconds) AS first_sec FROM departures "
         "WHERE feed_key='demo' AND edition='v1' AND service_date='20260330' "
         "AND stop_id IN ('007','7') GROUP BY stop_id ORDER BY stop_id", db)

    WIDE = ("2026-03-28", "2026-12-31")
    store = Store(db)
    store.expand("demo", "v1", *WIDE)
    store.close()
    baseline_rows = count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' "
                              "AND edition='v1'")
    baseline_hash = digest_departures(db, "demo", "v1", "20260328", "20261231")
    baseline_days = count(db, "SELECT COUNT(*) FROM service_days WHERE feed_key='demo' "
                              "AND edition='v1'")
    emit("8. Expand the full span once; row count, day count and a content hash of the result",
         ["  rows=%d service_days=%d sha256=%s" % (baseline_rows, baseline_days, baseline_hash)])

    emit("9. Repeat the identical request: nothing is added, nothing changes", [])
    store = Store(db)
    store.expand("demo", "v1", *WIDE)
    store.close()
    again_rows = count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1'")
    again_hash = digest_departures(db, "demo", "v1", "20260328", "20261231")
    print("  rows=%d service_days=%d sha256=%s"
          % (again_rows,
             count(db, "SELECT COUNT(*) FROM service_days WHERE feed_key='demo' AND edition='v1'"),
             again_hash))
    print("  identical to step 8:", again_rows == baseline_rows and again_hash == baseline_hash)

    emit("10. Attempt a replacement that is interrupted after 3 service days", [])
    print("  rows before attempt:", baseline_rows, " service_days:", baseline_days)
    env = dict(os.environ, GTFS_FAULT_EXPAND_AFTER_DAYS="3")
    run = subprocess.run(
        [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db), "expand",
         "--feed-key", "demo", "--edition", "v1", "--start", WIDE[0], "--end", WIDE[1]],
        env=env, capture_output=True, text=True)
    print("  exit=%d stderr=%r" % (run.returncode, run.stderr.strip()))
    print("  rows after interrupted attempt:",
          count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1'"),
          " service_days:",
          count(db, "SELECT COUNT(*) FROM service_days WHERE feed_key='demo' AND edition='v1'"))
    # A separate reader connection must also see the last complete result.
    reader = Store(db)
    try:
        rows = reader.departures("demo", "v1", "20260330")
        print("  separate reader still sees 20260330 rows:", len(rows))
        h = hashlib.sha256()
        for row in reader.conn.execute(
                "SELECT service_date, trip_instance_id, stop_id, stop_sequence, "
                "departure_seconds, departure_utc, departure_local, utc_offset, certainty, "
                "min_departure_seconds, max_departure_seconds, headway_secs FROM departures "
                "WHERE feed_key='demo' AND edition='v1' "
                "AND service_date BETWEEN '20260328' AND '20261231' "
                "ORDER BY service_date, trip_instance_id, stop_sequence"):
            h.update(("|".join("" if v is None else str(v) for v in row) + "\n").encode())
        print("  reader's own snapshot hash of full span:", h.hexdigest())
        print("  matches step 8:", h.hexdigest() == baseline_hash)
    finally:
        reader.close()

    emit("11. Redo the same request and compare with the step-8 snapshot", [])
    store = Store(db)
    store.expand("demo", "v1", *WIDE)
    store.close()
    redo_rows = count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1'")
    redo_hash = digest_departures(db, "demo", "v1", "20260328", "20261231")
    print("  rows=%d service_days=%d sha256=%s"
          % (redo_rows,
             count(db, "SELECT COUNT(*) FROM service_days WHERE feed_key='demo' AND edition='v1'"),
             redo_hash))
    print("  identical to step 8:", redo_rows == baseline_rows and redo_hash == baseline_hash)

    emit("12. Isolation: same internal ids in demo/v1, demo/v2 and other/v1", [])
    store = Store(db)
    store.expand("demo", "v2", "2026-03-30", "2026-03-30")
    store.expand("other", "v1", "2026-03-30", "2026-03-30")
    store.close()
    dump("SELECT feed_key, edition, COUNT(*) AS rows, COUNT(DISTINCT trip_instance_id) AS instances "
         "FROM departures WHERE service_date='20260330' GROUP BY feed_key, edition "
         "ORDER BY feed_key, edition", db)
    dump("SELECT feed_key, edition, stop_id, departure_seconds, departure_local, trip_instance_id "
         "FROM departures WHERE service_date='20260330' AND trip_id='T_WKDY' "
         "ORDER BY feed_key, edition, departure_seconds", db)
    print("  demo/v1 rows for T_WKDY_V2_EXTRA (must be 0):",
          count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1' "
                    "AND trip_id='T_WKDY_V2_EXTRA'"))
    print("  demo/v2 rows for T_WKDY_V2_EXTRA (must be 2):",
          count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v2' "
                    "AND trip_id='T_WKDY_V2_EXTRA'"))
    print("  every stored departure stop_id resolves in the same feed/edition:",
          count(db, "SELECT COUNT(*) FROM departures d WHERE NOT EXISTS ("
                    "SELECT 1 FROM stops s WHERE s.feed_key=d.feed_key "
                    "AND s.edition=d.edition AND s.stop_id=d.stop_id)") == 0)
    print("  other/v1 references only its own stops (S2 times 16:07, demo's S2 is 08:05:30):",
          count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='other' "
                    "AND stop_id='S2' AND departure_local='2026-03-30T16:07:00'"))

    emit("13. Rejected edition cannot be expanded and did not disturb ready ones", [])
    store = Store(db)
    try:
        store.expand("demo", "bad-overlap", "2026-03-30", "2026-03-30")
    except Exception as exc:
        print("  expand bad-overlap ->", type(exc).__name__, ":", exc)
    finally:
        store.close()
    print("  demo/v1 rows still present:",
          count(db, "SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1'") > 0)

    print()
    print("Walkthrough database:", db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
