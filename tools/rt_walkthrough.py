"""Realtime channel drill: every documented scenario against a fresh database.

Usage:  python3 -I tools/rt_walkthrough.py [--db PATH]

The output is a transcript of actual reads (SQL and API results), and every
step asserts its deterministic expectations; the script exits non-zero on the
first mismatch.  The transcript is copied into docs/RT_WALKTHROUGH.md.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.errors import GtfsError  # noqa: E402
from gtfs_store.realtime import RealtimeStore  # noqa: E402
from gtfs_store.store import Store  # noqa: E402

DIST = ROOT / "dist"
RT = DIST / "rt"

FAILURES: list[str] = []


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    print("  %-66s %s" % (label, "OK" if ok
                          else "MISMATCH\n    expected: %r\n    actual:   %r"
                          % (expected, actual)))
    if not ok:
        FAILURES.append(label)


def dump(db: Path, sql: str, columns: list[str] | None = None) -> None:
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


class Drill:
    def __init__(self, db: Path) -> None:
        self.db = db
        self.store = Store(db)
        self.rt = RealtimeStore(self.store)

    def reopen(self) -> None:
        self.store.close()
        self.store = Store(self.db)
        self.rt = RealtimeStore(self.store)

    def feed(self, name: str, source: str = "metro") -> dict:
        result = self.rt.feed(source, (RT / name).read_bytes())
        extra = ""
        if result["category"] == "applied":
            extra = " upserts=%d withdrawals=%d stop_events=%d" % (
                result["entities_upserted"], result["entities_withdrawn"],
                result["stop_events"])
        elif result["category"] == "duplicate":
            extra = " original=#%s (%s)" % (result["original_reception_id"],
                                            result["original_category"])
        problems = ",".join(d["category"] for d in result["diagnostics"]
                            if d["category"] != "summary")
        print("  %-30s -> %-9s ts=%-5s%s %s"
              % (name, result["category"], result["header_timestamp"], extra,
                 problems))
        return result

    def entities(self, source: str = "metro") -> list[tuple]:
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT entity_id, trip_id, service_date, start_time, "
            "trip_schedule_relationship, entity_timestamp, head_timestamp, "
            "reception_id FROM rt_entities WHERE source_key=? ORDER BY entity_id",
            (source,))]

    def stop_events(self, source: str = "metro") -> list[tuple]:
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT entity_id, stop_sequence, stop_id, schedule_relationship, "
            "has_arrival, arr_time, arr_delay, arr_uncertainty, has_departure, "
            "dep_time, dep_delay, dep_uncertainty FROM rt_stop_events "
            "WHERE source_key=? ORDER BY entity_id, stop_sequence", (source,))]

    def show_state(self, source: str = "metro") -> None:
        print("  entities (id|trip|date|start|rel|entity_ts|head_ts|reception):")
        for row in self.entities(source):
            print("    " + " | ".join(str(v) for v in row))
        print("  stop events (entity|seq|stop_id|rel|has_arr|time|delay|unc|"
              "has_dep|time|delay|unc):")
        for row in self.stop_events(source):
            print("    " + " | ".join(str(v) for v in row))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    args = parser.parse_args()
    if not (DIST / "demo-v1.zip").exists():
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fixtures.py")],
                       check=True)
    if not RT.exists():
        subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_rt_fixtures.py")],
                       check=True)

    tmp = tempfile.mkdtemp(prefix="gtfs-rt-walkthrough-") if not args.db else None
    db = Path(args.db) if args.db else Path(tmp) / "rt-walkthrough.db"
    if db.exists():
        db.unlink()
    d = Drill(db)

    emit("1. Static base and permanent source registrations")
    print("  load demo/v1:", d.store.load("demo", "v1", DIST / "demo-v1.zip")["status"])
    print("  load demo/v2:", d.store.load("demo", "v2", DIST / "demo-v2.zip")["status"])
    for source in ("metro", "regional", "swap", "latejoin", "race-ab", "race-ba",
                   "race"):
        d.rt.register_source(source, "demo", "v1")
    print("  registered: metro, regional, swap, latejoin, race-ab, race-ba, race "
          "(all bound to demo/v1)")
    try:
        d.rt.register_source("metro", "demo", "v2")
        print("  REBIND WAS ALLOWED -- BUG")
        FAILURES.append("rebind allowed")
    except GtfsError as exc:
        print("  rebind metro->demo/v2 refused:", exc)
    again = d.rt.register_source("metro", "demo", "v1")
    check("same-binding re-registration is an idempotent no-op",
          again["already_registered"], True)

    emit("2. Baseline FULL_DATASET {A,B}, then DIFFERENTIAL replacing only A")
    d.feed("m01-full-a-b.pb")
    d.show_state()
    d.feed("m02-diff-a.pb")
    d.show_state()
    check("A replaced: only seq 10/20 remain, delay/uncertainty of seq 10 cleared",
          d.stop_events()[:2],
          [("A", 10, None, "SCHEDULED", 1, 29010, None, None, 0, None, None, None),
           ("A", 20, None, "SCHEDULED", 0, None, None, None, 0, None, None, None)])
    check("B untouched, still aged from ts=1000",
          d.entities()[1],
          ("B", "T_LATE", "20261114", "", "SCHEDULED", None, 1000, 1))

    emit("3. Explicit zero vs absent (read back of m01 before it was replaced)")
    # m01's A is gone, but B's delay=-30 and the m01 reception blob prove the
    # presence semantics; inspect the stored m01 payload through its reception.
    detail = d.rt.reception_detail(1, include_blob=True)
    check("reception #1 blob is byte-identical to the file",
          detail["blob"], (RT / "m01-full-a-b.pb").read_bytes())
    from gtfs_store import rtbind
    pb = rtbind.bindings().pb
    replay = pb.FeedMessage()
    replay.ParseFromString(detail["blob"])
    a_seq20 = replay.entity[0].trip_update.stop_time_update[1]
    check("m01 A seq20 arrival: delay given and == 0",
          (a_seq20.arrival.HasField("delay"), a_seq20.arrival.delay), (True, 0))
    check("m01 A seq20 arrival: time not given",
          a_seq20.arrival.HasField("time"), False)

    emit("4. Incremental withdrawal of B; then a full dataset without A")
    d.feed("m03-diff-del-b.pb")
    check("after del B", [r[0] for r in d.entities()], ["A"])
    d.feed("m04-full-c.pb")
    check("full {C} withdraws A by omission", [r[0] for r in d.entities()], ["C"])
    d.feed("m05-full-empty.pb")
    check("empty full dataset withdraws everything", d.entities(), [])
    check("success ledger keeps 1000..1040 even with no entities left",
          [row["header_timestamp"] for row in d.rt.ledger("metro")],
          [1000, 1010, 1020, 1030, 1040])

    emit("5. CANCELED is a notice; withdrawal is absence")
    d.feed("m06-full-d-cancel.pb")
    check("D is a readable CANCELED notice without stop events",
          d.entities(), [("D", "T_TWO_STOPS", "20261114", "", "CANCELED", None, 1050, 6)])
    check("D has no stop events", d.stop_events(), [])
    d.feed("m07-diff-del-d.pb")
    check("after withdrawal the source simply has nothing", d.entities(), [])

    emit("6. Rename: same message withdraws X and hands the instance to Y")
    d.feed("m08-full-x.pb")
    d.feed("m09-diff-rename.pb")
    check("only Y remains, holding the (T_FREQ_EXACT, 20261116, 09:30:00) instance",
          d.entities(),
          [("Y", "T_FREQ_EXACT", "20261116", "09:30:00", "SCHEDULED", None, 1080, 9)])
    d.feed("m08-full-x.pb", "swap")
    d.feed("m09b-diff-rename-swapped.pb", "swap")
    check("swapped entity order on a second source gives the identical set",
          [r[:7] for r in d.entities("swap")], [r[:7] for r in d.entities("metro")])

    emit("7. Bad stop_sequence rejected; corrected resend reuses the timestamp")
    bad = d.feed("m10-diff-bad-seq.pb")
    check("m10 rejected with unknown_stop_sequence",
          [p["category"] for p in bad["diagnostics"]], ["unknown_stop_sequence"])
    check("head did not move", d.rt.source_status("metro")["head_timestamp"], 1080)
    check("1090 not in the success ledger",
          1090 in [row["header_timestamp"] for row in d.rt.ledger("metro")], False)
    d.feed("m11-diff-bad-seq-fixed.pb")
    check("corrected message at the same timestamp applied",
          [r[0] for r in d.entities()], ["E", "Y"])

    emit("8. Digest ledger: duplicate, conflict, late")
    dup = d.feed("m01-full-a-b.pb")
    check("redelivered m01 is a duplicate of reception #1, nothing applied",
          (dup["category"], dup["original_reception_id"], dup["original_category"]),
          ("duplicate", 1, "applied"))
    check("the duplicate did not resurrect A/B", [r[0] for r in d.entities()],
          ["E", "Y"])
    conflict = d.feed("m12-conflict-full-z.pb")
    check("same timestamp 1080 with different bytes is a conflict",
          (conflict["category"], [p["category"] for p in conflict["diagnostics"]]),
          ("conflict", ["timestamp_conflict"]))
    late = d.feed("m13-late-diff.pb")
    check("new bytes older than the head only leave a late record",
          late["category"], "late")
    check("set unchanged after duplicate/conflict/late",
          [r[0] for r in d.entities()], ["E", "Y"])

    emit("9. Heartbeats move only the head; freshness boundary is exact")
    d.feed("m14-heartbeat.pb")
    d.feed("m15-heartbeat.pb")
    check("head is now 1110", d.rt.source_status("metro")["head_timestamp"], 1110)
    check("E still aged from its replacing message at 1090",
          d.entities()[0][6], 1090)
    fresh = d.rt.notices("metro", now=1189, max_age=100)
    check("now=1189: E (expires 1190) fresh, Y (expires 1180) hidden",
          [(n["entity_id"], n["fresh"]) for n in fresh["notices"]], [("E", True)])
    boundary = d.rt.notices("metro", now=1190, max_age=100)
    check("now=1190 == ts+max_age: E is expired, default read is empty",
          boundary["notices"], [])
    everything = d.rt.notices("metro", now=1190, max_age=100, include_expired=True)
    check("include-expired read still shows E and Y, flagged expired",
          [(n["entity_id"], n["fresh"]) for n in everything["notices"]],
          [("E", False), ("Y", False)])
    try:
        d.rt.notices("metro", now=1105, max_age=100, include_expired=True)
        check("now behind the head is refused", "allowed", "refused")
    except GtfsError as exc:
        print("  include-expired at now=1105 refused:", exc)
        check("now behind the head is refused", "refused", "refused")
    try:
        d.rt.notices("metro", now=1200, max_age=0)
        check("max_age=0 refused", "allowed", "refused")
    except GtfsError as exc:
        print("  max_age=0 refused:", exc)
        check("max_age=0 refused", "refused", "refused")

    emit("10. Every bad sample is rejected with a reason; nothing changes")
    expected = [
        ("bad-version.pb", "unsupported_version"),
        ("bad-no-version.pb", "missing_version"),
        ("bad-no-incrementality.pb", "missing_incrementality"),
        ("bad-no-timestamp.pb", "missing_timestamp"),
        ("bad-zero-timestamp.pb", "non_positive_timestamp"),
        ("bad-no-header.pb", "missing_header"),
        ("corrupt.pb", "encoding"),
        ("bad-vehicle.pb", "unsupported_entity"),
        ("bad-mixed.pb", "mixed_entity"),
        ("bad-empty-payload.pb", "missing_payload"),
        ("bad-empty-id.pb", "empty_entity_id"),
        ("bad-no-id.pb", "missing_entity_id"),
        ("bad-dup-id.pb", "duplicate_entity_id"),
        ("bad-deleted-payload.pb", "deleted_with_payload"),
        ("bad-deleted-in-full.pb", "deleted_in_full_dataset"),
        ("bad-unknown-trip.pb", "unknown_trip"),
        ("bad-stop-id-mismatch.pb", "stop_id_mismatch"),
        ("bad-dup-seq.pb", "duplicate_stop_sequence"),
        ("bad-neg-uncertainty.pb", "negative_uncertainty"),
        ("bad-canceled-with-stops.pb", "canceled_with_stop_events"),
        ("bad-trip-rel.pb", "unsupported_trip_relationship"),
        ("bad-stop-rel.pb", "unsupported_stop_relationship"),
        ("bad-no-start-date.pb", "missing_start_date"),
        ("bad-skipped-with-event.pb", "event_on_non_scheduled_stop"),
        ("bad-instance-conflict.pb", "instance_conflict"),
    ]
    for name, category in expected:
        result = d.feed(name)
        got = [p["category"] for p in result["diagnostics"]]
        if result["category"] != "rejected" or category not in got:
            FAILURES.append("%s should reject with %s" % (name, category))
            print("    ^^ expected rejected/%s" % category)
    check("after all rejections the set is still {E, Y}",
          [r[0] for r in d.entities()], ["E", "Y"])
    check("head is still 1110", d.rt.source_status("metro")["head_timestamp"], 1110)
    check("success ledger has no 3000 entry",
          3000 in [row["header_timestamp"] for row in d.rt.ledger("metro")], False)

    emit("11. Reception ledger: every arrival is accounted for")
    dump(db, "SELECT id, category, header_incrementality, header_timestamp, applied, "
             "substr(sha256,1,12) AS sha, size_bytes FROM rt_receptions "
             "WHERE source_key='metro' ORDER BY id")
    dump(db, "SELECT header_timestamp, substr(sha256,1,12) AS sha, reception_id "
             "FROM rt_head_ledger WHERE source_key='metro' ORDER BY header_timestamp")
    print("  current entities point back at their producing reception:")
    dump(db, "SELECT entity_id, reception_id, head_timestamp FROM rt_entities "
             "WHERE source_key='metro' ORDER BY entity_id")

    emit("12. Two sources, same entity ids, fully independent")
    d.feed("m01-full-a-b.pb", "regional")
    d.feed("m02-diff-a.pb", "regional")
    d.feed("m03-diff-del-b.pb", "regional")
    check("regional has its own A (replaced at 1010)",
          [r[0] for r in d.entities("regional")], ["A"])
    check("metro is unaffected (still {E, Y})",
          [r[0] for r in d.entities()], ["E", "Y"])
    latejoin = d.feed("m03-diff-del-b.pb", "latejoin")
    check("differential before any baseline is rejected",
          (latejoin["category"],
           [p["category"] for p in latejoin["diagnostics"]]),
          ("rejected", ["no_baseline"]))

    emit("13. Competing receivers: one full and one differential, both orders")
    print("  sequential, full first:")
    r1 = d.feed("race-full.pb", "race-ab")
    r2 = d.feed("race-diff.pb", "race-ab")
    check("race-ab: both applied", (r1["category"], r2["category"]),
          ("applied", "applied"))
    check("race-ab holds X@2000 and Y@2001, head 2001",
          ([r[0] for r in d.entities("race-ab")],
           d.rt.source_status("race-ab")["head_timestamp"]),
          (["X", "Y"], 2001))
    print("  sequential, differential first:")
    r1 = d.feed("race-diff.pb", "race-ba")
    r2 = d.feed("race-full.pb", "race-ba")
    check("race-ba: differential rejected (no baseline), full applied",
          (r1["category"], r2["category"]), ("rejected", "applied"))
    check("race-ba holds only X@2000, head 2000",
          ([r[0] for r in d.entities("race-ba")],
           d.rt.source_status("race-ba")["head_timestamp"]),
          (["X"], 2000))
    print("  threaded race on source 'race' (both receivers start together):")
    full_bytes = (RT / "race-full.pb").read_bytes()
    diff_bytes = (RT / "race-diff.pb").read_bytes()
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def receiver(name: str, blob: bytes) -> None:
        store = Store(db)
        rt = RealtimeStore(store)
        barrier.wait()
        try:
            results[name] = rt.feed("race", blob)["category"]
        finally:
            store.close()

    threads = [threading.Thread(target=receiver, args=("full", full_bytes)),
               threading.Thread(target=receiver, args=("diff", diff_bytes))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("  outcomes: full=%s diff=%s" % (results["full"], results["diff"]))
    d.reopen()
    ents = [r[0] for r in d.entities("race")]
    head = d.rt.source_status("race")["head_timestamp"]
    ledger = [row["header_timestamp"] for row in d.rt.ledger("race")]
    full_first = (results == {"full": "applied", "diff": "applied"}
                  and ents == ["X", "Y"] and head == 2001 and ledger == [2000, 2001])
    diff_first = (results == {"full": "applied", "diff": "rejected"}
                  and ents == ["X"] and head == 2000 and ledger == [2000])
    print("  stored state: entities=%s head=%s ledger=%s" % (ents, head, ledger))
    check("threaded race landed exactly on one of the two serial outcomes",
          full_first != diff_first, True)

    emit("14. Reopen the database: head, entities and ledger still agree")
    before = (d.entities("metro"), d.stop_events("metro"),
              [row["header_timestamp"] for row in d.rt.ledger("metro")],
              d.rt.source_status("metro")["head_timestamp"])
    d.reopen()
    after = (d.entities("metro"), d.stop_events("metro"),
             [row["header_timestamp"] for row in d.rt.ledger("metro")],
             d.rt.source_status("metro")["head_timestamp"])
    check("metro state identical across reopen", after, before)
    race_before = (d.entities("race"),
                   d.rt.source_status("race")["head_timestamp"],
                   [row["header_timestamp"] for row in d.rt.ledger("race")])
    d.reopen()
    race_after = (d.entities("race"),
                  d.rt.source_status("race")["head_timestamp"],
                  [row["header_timestamp"] for row in d.rt.ledger("race")])
    check("race outcome identical across reopen", race_after, race_before)

    emit("15. Static features keep working in the same database")
    expanded = d.store.expand("demo", "v1", "2026-03-30", "2026-03-30")
    print("  expand demo/v1 2026-03-30:", expanded["days"][0])
    departures = d.store.departures("demo", "v1", "2026-03-30", "007")
    print("  departures at 007 on 2026-03-30: %d row(s), first trip %s"
          % (len(departures), departures[0]["trip_id"] if departures else "-"))
    check("static expansion unaffected by realtime tables",
          len(departures) > 0, True)
    notices = d.rt.notices("metro", now=1190, max_age=100, include_expired=True)
    check("realtime reads unaffected by expansion",
          [n["entity_id"] for n in notices["notices"]], ["E", "Y"])

    print()
    if FAILURES:
        print("DRILL FAILED: %d expectation(s) mismatched:" % len(FAILURES))
        for f in FAILURES:
            print("  -", f)
        return 1
    print("Drill finished: every expectation held.  Database:", db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
