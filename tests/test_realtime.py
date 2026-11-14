"""Realtime channel: set changes, digest ledger, freshness, isolation, races.

Run with:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path

from helpers import DIST, StoreTestCase, build_all_fixtures

from gtfs_store import rtbind
from gtfs_store.errors import GtfsError
from gtfs_store.realtime import RealtimeStore
from gtfs_store.store import Store

ROOT = Path(__file__).resolve().parent.parent
RT = DIST / "rt"

pb = rtbind.bindings().pb

DATE1 = "20261114"
DATE2 = "20261115"
DATE3 = "20261116"


def build_rt_fixtures() -> None:
    subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_rt_fixtures.py")],
                   check=True, capture_output=True, cwd=str(ROOT))


def sample(name: str) -> bytes:
    return (RT / name).read_bytes()


class RealtimeTestCase(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()
        build_rt_fixtures()

    def setUp(self) -> None:
        super().setUp()
        result = self.store.load("demo", "v1", DIST / "demo-v1.zip")
        assert result["status"] == "ready", result
        self.store.load("demo", "v2", DIST / "demo-v2.zip")
        self.rt = RealtimeStore(self.store)
        self.rt.register_source("metro", "demo", "v1")

    def feed(self, name: str, source: str = "metro") -> dict:
        return self.rt.feed(source, sample(name))

    def entity_ids(self, source: str = "metro") -> list[str]:
        return [r["entity_id"] for r in self.sql(
            "SELECT entity_id FROM rt_entities WHERE source_key=? ORDER BY entity_id",
            (source,))]

    def notice(self, entity_id: str, source: str = "metro", now: int = 10 ** 9) -> dict:
        result = self.rt.notices(source, now, 10 ** 9, include_expired=True)
        found = [n for n in result["notices"] if n["entity_id"] == entity_id]
        self.assertEqual(len(found), 1, result["notices"])
        return found[0]

    def stops_of(self, entity_id: str, source: str = "metro") -> list[dict]:
        return self.notice(entity_id, source)["stop_events"]


class SetChangeTests(RealtimeTestCase):
    def test_full_then_differential_replaces_only_listed_entities(self):
        r1 = self.feed("m01-full-a-b.pb")
        self.assertEqual(r1["category"], "applied")
        self.assertEqual((r1["entities_upserted"], r1["entities_withdrawn"]), (2, 0))
        self.assertEqual(self.entity_ids(), ["A", "B"])

        r2 = self.feed("m02-diff-a.pb")
        self.assertEqual(r2["category"], "applied")
        self.assertEqual((r2["entities_upserted"], r2["entities_withdrawn"]), (1, 0))
        # B is untouched; A was replaced wholesale.
        self.assertEqual(self.entity_ids(), ["A", "B"])
        a = self.notice("A")
        self.assertEqual(a["replaced_at_header_timestamp"], 1010)
        self.assertEqual(a["entity_timestamp"], 1005)
        self.assertEqual([s["stop_sequence"] for s in a["stop_events"]], [10, 20])
        b = self.notice("B")
        self.assertEqual(b["replaced_at_header_timestamp"], 1000)
        self.assertIsNone(b["entity_timestamp"])
        self.assertEqual([s["stop_sequence"] for s in b["stop_events"]], [1, 2])

    def test_replacement_clears_absent_fields_and_keeps_explicit_zero(self):
        self.feed("m01-full-a-b.pb")
        a1 = self.notice("A")
        seq10, seq20, seq30, seq40 = a1["stop_events"]
        # m01 seq10: time, delay and uncertainty all given.
        self.assertEqual(seq10["arrival"],
                         {"time": 29000, "delay": 60, "uncertainty": 30})
        self.assertEqual(seq10["departure"],
                         {"time": 29040, "delay": None, "uncertainty": None})
        # m01 seq20: explicit delay=0 is a real value, not "absent".
        self.assertEqual(seq20["arrival"], {"time": None, "delay": 0,
                                            "uncertainty": None})
        self.assertEqual(seq20["departure"], {"time": None, "delay": None,
                                              "uncertainty": None})
        self.assertEqual(seq30["schedule_relationship"], "SKIPPED")
        self.assertIsNone(seq30["arrival"])
        self.assertIsNone(seq30["departure"])
        self.assertEqual(seq40["schedule_relationship"], "SCHEDULED")
        self.assertIsNone(seq40["arrival"])

        self.feed("m02-diff-a.pb")
        a2 = self.notice("A")
        seq10, seq20 = a2["stop_events"]
        # The new payload has only time on seq10: delay/uncertainty are cleared.
        self.assertEqual(seq10["arrival"], {"time": 29010, "delay": None,
                                            "uncertainty": None})
        self.assertIsNone(seq10["departure"])
        # seq20 now carries no events at all; seq30/seq40 are gone.
        self.assertIsNone(seq20["arrival"])
        self.assertIsNone(seq20["departure"])

    def test_time_zero_is_not_absent(self):
        # time=0 and delay=0 are explicit values, distinct from "not given".
        m = pb.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.incrementality = pb.FeedHeader.FULL_DATASET
        m.header.timestamp = 5000
        e = m.entity.add()
        e.id = "Z"
        e.trip_update.trip.trip_id = "T_WKDY"
        e.trip_update.trip.start_date = DATE1
        stu = e.trip_update.stop_time_update.add()
        stu.stop_sequence = 10
        stu.arrival.time = 0
        stu.arrival.delay = 0
        result = self.rt.feed("metro", m.SerializeToString())
        self.assertEqual(result["category"], "applied", result)
        z = self.notice("Z")
        self.assertEqual(z["stop_events"][0]["arrival"],
                         {"time": 0, "delay": 0, "uncertainty": None})

    def test_cancel_then_withdraw_read_differently(self):
        self.feed("m06-full-d-cancel.pb")
        d = self.notice("D")
        self.assertEqual(d["trip_schedule_relationship"], "CANCELED")
        self.assertEqual(d["stop_events"], [])
        self.feed("m07-diff-del-d.pb")
        self.assertEqual(self.entity_ids(), [])
        result = self.rt.notices("metro", 10 ** 9, 10 ** 9, include_expired=True)
        self.assertEqual(result["notices"], [])

    def test_full_omission_withdraws_and_empty_full_withdraws_all(self):
        self.feed("m01-full-a-b.pb")
        r = self.feed("m04-full-c.pb")
        self.assertEqual((r["entities_upserted"], r["entities_withdrawn"]), (1, 2))
        self.assertEqual(self.entity_ids(), ["C"])
        r = self.feed("m05-full-empty.pb")
        self.assertEqual((r["entities_upserted"], r["entities_withdrawn"]), (0, 1))
        self.assertEqual(self.entity_ids(), [])
        # The success ledger keeps every applied timestamp even though the
        # entities themselves are long gone.
        self.assertEqual([row["header_timestamp"] for row in self.rt.ledger("metro")],
                         [1000, 1030, 1040])

    def test_rename_hands_instance_to_new_entity_id(self):
        self.feed("m08-full-x.pb")
        self.assertEqual(self.entity_ids(), ["X"])
        r = self.feed("m09-diff-rename.pb")
        self.assertEqual(r["category"], "applied")
        self.assertEqual(self.entity_ids(), ["Y"])
        y = self.notice("Y")
        self.assertEqual((y["trip_id"], y["service_date"], y["start_time"]),
                         ("T_FREQ_EXACT", DATE3, "09:30:00"))
        self.assertEqual(y["replaced_at_header_timestamp"], 1080)

    def test_entity_order_inside_message_does_not_matter(self):
        self.rt.register_source("swap", "demo", "v1")
        self.feed("m08-full-x.pb")
        self.feed("m09-diff-rename.pb")
        self.feed("m08-full-x.pb", "swap")
        self.feed("m09b-diff-rename-swapped.pb", "swap")
        metro = self.rt.notices("metro", 10 ** 9, 10 ** 9, include_expired=True)["notices"]
        swap = self.rt.notices("swap", 10 ** 9, 10 ** 9, include_expired=True)["notices"]
        strip = lambda ns: [{k: v for k, v in n.items()
                             if k not in ("reception_id", "reception_sha256")}
                            for n in ns]
        self.assertEqual(strip(metro), strip(swap))

    def test_instance_conflict_with_current_entity(self):
        self.feed("m08-full-x.pb")              # baseline
        self.feed("m11-diff-bad-seq-fixed.pb")  # E holds (T_WKDY, DATE1)
        result = self.feed("bad-instance-conflict.pb")
        self.assertEqual(result["category"], "rejected")
        self.assertEqual([d["category"] for d in result["diagnostics"]],
                         ["instance_conflict"])
        self.assertEqual(self.entity_ids(), ["E", "X"])

    def test_instance_conflict_inside_one_full_message(self):
        m = pb.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.incrementality = pb.FeedHeader.FULL_DATASET
        m.header.timestamp = 4000
        for entity_id in ("G1", "G2"):
            e = m.entity.add()
            e.id = entity_id
            e.trip_update.trip.trip_id = "T_WKDY"
            e.trip_update.trip.start_date = DATE1
        result = self.rt.feed("metro", m.SerializeToString())
        self.assertEqual(result["category"], "rejected")
        self.assertEqual([d["category"] for d in result["diagnostics"]],
                         ["instance_conflict"])
        self.assertEqual(self.entity_ids(), [])

    def test_instance_swap_between_two_entities_in_one_message(self):
        # X holds (T_WKDY, DATE1) and Y holds (T_LATE, DATE1); one
        # differential swaps the two instances between the entities.
        def full(ts, pairs):
            m = pb.FeedMessage()
            m.header.gtfs_realtime_version = "2.0"
            m.header.incrementality = pb.FeedHeader.FULL_DATASET
            m.header.timestamp = ts
            for entity_id, trip_id in pairs:
                e = m.entity.add()
                e.id = entity_id
                e.trip_update.trip.trip_id = trip_id
                e.trip_update.trip.start_date = DATE1
            return m.SerializeToString()

        self.rt.feed("metro", full(4000, [("X", "T_WKDY"), ("Y", "T_LATE")]))
        m = pb.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.incrementality = pb.FeedHeader.DIFFERENTIAL
        m.header.timestamp = 4001
        for entity_id, trip_id in (("X", "T_LATE"), ("Y", "T_WKDY")):
            e = m.entity.add()
            e.id = entity_id
            e.trip_update.trip.trip_id = trip_id
            e.trip_update.trip.start_date = DATE1
        result = self.rt.feed("metro", m.SerializeToString())
        self.assertEqual(result["category"], "applied", result)
        self.assertEqual(
            sorted((r["entity_id"], r["trip_id"]) for r in self.sql(
                "SELECT entity_id, trip_id FROM rt_entities WHERE source_key='metro'")),
            [("X", "T_LATE"), ("Y", "T_WKDY")])

    def test_differential_before_baseline_is_rejected(self):
        self.rt.register_source("latejoin", "demo", "v1")
        result = self.feed("m03-diff-del-b.pb", "latejoin")
        self.assertEqual(result["category"], "rejected")
        self.assertEqual([d["category"] for d in result["diagnostics"]], ["no_baseline"])
        status = self.rt.source_status("latejoin")
        self.assertIsNone(status["head_timestamp"])
        self.assertFalse(status["baseline_seen"])

    def test_empty_differential_moves_only_the_head(self):
        self.feed("m01-full-a-b.pb")
        before = [tuple(r) for r in self.sql(
            "SELECT * FROM rt_entities WHERE source_key='metro' ORDER BY entity_id")]
        r = self.feed("m14-heartbeat.pb")
        self.assertEqual(r["category"], "applied")
        self.assertEqual((r["entities_upserted"], r["entities_withdrawn"]), (0, 0))
        after = [tuple(r) for r in self.sql(
            "SELECT * FROM rt_entities WHERE source_key='metro' ORDER BY entity_id")]
        self.assertEqual(before, after)
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1100)


class LedgerTests(RealtimeTestCase):
    def test_rejected_message_is_not_in_success_ledger_and_timestamp_is_reusable(self):
        self.feed("m08-full-x.pb")
        bad = self.feed("m10-diff-bad-seq.pb")
        self.assertEqual(bad["category"], "rejected")
        self.assertEqual([d["category"] for d in bad["diagnostics"]],
                         ["unknown_stop_sequence"])
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1070)
        self.assertNotIn(1090, [row["header_timestamp"] for row in self.rt.ledger("metro")])
        fixed = self.feed("m11-diff-bad-seq-fixed.pb")
        self.assertEqual(fixed["category"], "applied")
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1090)
        self.assertEqual(self.entity_ids(), ["E", "X"])

    def test_redelivery_after_entities_gone_returns_original_conclusion(self):
        first = self.feed("m01-full-a-b.pb")
        self.feed("m05-full-empty.pb")  # everything withdrawn
        self.assertEqual(self.entity_ids(), [])
        again = self.feed("m01-full-a-b.pb")
        self.assertEqual(again["category"], "duplicate")
        self.assertEqual(again["original_reception_id"], first["reception_id"])
        self.assertEqual(again["original_category"], "applied")
        # Nothing came back to life.
        self.assertEqual(self.entity_ids(), [])
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1040)

    def test_same_timestamp_different_bytes_is_a_conflict(self):
        self.feed("m08-full-x.pb")
        self.feed("m09-diff-rename.pb")
        conflict = self.feed("m12-conflict-full-z.pb")
        self.assertEqual(conflict["category"], "conflict")
        self.assertEqual([d["category"] for d in conflict["diagnostics"]],
                         ["timestamp_conflict"])
        # The applied set is untouched.
        self.assertEqual(self.entity_ids(), ["Y"])
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1080)
        # The ledger still points at the original message only.
        self.assertEqual([row["header_timestamp"] for row in self.rt.ledger("metro")],
                         [1070, 1080])

    def test_older_new_message_is_late_only(self):
        self.feed("m08-full-x.pb")
        self.feed("m11-diff-bad-seq-fixed.pb")  # head 1090
        late = self.feed("m13-late-diff.pb")
        self.assertEqual(late["category"], "late")
        self.assertEqual(self.entity_ids(), ["E", "X"])
        self.assertNotIn(1075, [row["header_timestamp"] for row in self.rt.ledger("metro")])
        # The late arrival is still on the reception ledger.
        rows = [r for r in self.rt.receptions("metro") if r["category"] == "late"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["header_timestamp"], 1075)

    def test_reception_rows_keep_blob_digest_and_back_reference(self):
        applied = self.feed("m01-full-a-b.pb")
        detail = self.rt.reception_detail(applied["reception_id"], include_blob=True)
        self.assertEqual(detail["blob"], sample("m01-full-a-b.pb"))
        self.assertEqual(detail["header_timestamp"], 1000)
        self.assertEqual(detail["category"], "applied")
        a = self.notice("A")
        self.assertEqual(a["reception_id"], applied["reception_id"])
        self.assertEqual(a["reception_sha256"], detail["sha256"])
        # From the current detail back to its source message.
        back = self.rt.reception_detail(a["reception_id"], include_blob=True)
        self.assertEqual(back["blob"], sample("m01-full-a-b.pb"))

    def test_ledger_survives_withdrawal(self):
        self.feed("m01-full-a-b.pb")
        self.feed("m03-diff-del-b.pb")
        self.feed("m05-full-empty.pb")
        self.assertEqual(self.entity_ids(), [])
        self.assertEqual([row["header_timestamp"] for row in self.rt.ledger("metro")],
                         [1000, 1020, 1040])


class FreshnessTests(RealtimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.feed("m08-full-x.pb")           # X @1070
        self.feed("m11-diff-bad-seq-fixed.pb")  # E @1090
        self.feed("m14-heartbeat.pb")        # head 1100
        self.feed("m15-heartbeat.pb")        # head 1110

    def test_boundary_is_expired_and_heartbeats_do_not_refresh(self):
        # E was last replaced @1090; with max_age=100 it expires exactly at 1190.
        fresh = self.rt.notices("metro", 1189, 100)
        self.assertEqual([n["entity_id"] for n in fresh["notices"]], ["E"])
        boundary = self.rt.notices("metro", 1190, 100)
        self.assertEqual(boundary["notices"], [])
        everything = self.rt.notices("metro", 1190, 100, include_expired=True)
        flags = {n["entity_id"]: n["fresh"] for n in everything["notices"]}
        self.assertEqual(flags, {"E": False, "X": False})
        self.assertEqual(everything["head_timestamp"], 1110)

    def test_include_expired_rejects_now_behind_head(self):
        with self.assertRaises(GtfsError):
            self.rt.notices("metro", 1105, 100, include_expired=True)
        # The default read has no such rule.
        self.assertEqual(len(self.rt.notices("metro", 1105, 100)["notices"]), 2)

    def test_max_age_must_be_positive(self):
        with self.assertRaises(GtfsError):
            self.rt.notices("metro", 1200, 0)
        with self.assertRaises(GtfsError):
            self.rt.notices("metro", 1200, -5, include_expired=True)


class IsolationTests(RealtimeTestCase):
    def test_two_sources_same_entity_ids_do_not_interfere(self):
        self.rt.register_source("regional", "demo", "v1")
        self.feed("m01-full-a-b.pb")            # metro: A, B
        self.feed("m01-full-a-b.pb", "regional")  # regional: A, B (same bytes!)
        self.feed("m03-diff-del-b.pb")          # metro drops B
        self.assertEqual(self.entity_ids("metro"), ["A"])
        self.assertEqual(self.entity_ids("regional"), ["A", "B"])
        # Ledgers and heads are per source.
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], 1020)
        self.assertEqual(self.rt.source_status("regional")["head_timestamp"], 1000)
        # Same bytes at the same timestamp are independent ledger entries.
        self.assertEqual(len(self.rt.ledger("metro")), 2)
        self.assertEqual(len(self.rt.ledger("regional")), 1)

    def test_rebind_is_refused_and_same_binding_is_idempotent(self):
        again = self.rt.register_source("metro", "demo", "v1")
        self.assertTrue(again["already_registered"])
        with self.assertRaises(GtfsError):
            self.rt.register_source("metro", "demo", "v2")
        self.store.load("other", "v1", DIST / "other-operator.zip")
        with self.assertRaises(GtfsError):
            self.rt.register_source("metro", "other", "v1")

    def test_register_requires_ready_edition(self):
        with self.assertRaises(GtfsError):
            self.rt.register_source("ghost", "demo", "no-such-edition")
        self.store.load("demo", "bad", DIST / "reject-overlap.zip")
        with self.assertRaises(GtfsError):
            self.rt.register_source("ghost", "demo", "bad")


class RejectionCatalogTests(RealtimeTestCase):
    EXPECTED = {
        "bad-version.pb": "unsupported_version",
        "bad-no-version.pb": "missing_version",
        "bad-no-incrementality.pb": "missing_incrementality",
        "bad-no-timestamp.pb": "missing_timestamp",
        "bad-zero-timestamp.pb": "non_positive_timestamp",
        "bad-no-header.pb": "missing_header",
        "corrupt.pb": "encoding",
        "bad-vehicle.pb": "unsupported_entity",
        "bad-mixed.pb": "mixed_entity",
        "bad-empty-payload.pb": "missing_payload",
        "bad-empty-id.pb": "empty_entity_id",
        "bad-no-id.pb": "missing_entity_id",
        "bad-dup-id.pb": "duplicate_entity_id",
        "bad-deleted-payload.pb": "deleted_with_payload",
        "bad-deleted-in-full.pb": "deleted_in_full_dataset",
        "bad-unknown-trip.pb": "unknown_trip",
        "bad-stop-id-mismatch.pb": "stop_id_mismatch",
        "bad-dup-seq.pb": "duplicate_stop_sequence",
        "bad-neg-uncertainty.pb": "negative_uncertainty",
        "bad-canceled-with-stops.pb": "canceled_with_stop_events",
        "bad-trip-rel.pb": "unsupported_trip_relationship",
        "bad-stop-rel.pb": "unsupported_stop_relationship",
        "bad-no-start-date.pb": "missing_start_date",
        "bad-skipped-with-event.pb": "event_on_non_scheduled_stop",
    }

    def test_every_bad_sample_leaves_a_rejection_note_and_changes_nothing(self):
        self.feed("m01-full-a-b.pb")
        head_before = self.rt.source_status("metro")["head_timestamp"]
        for name, category in sorted(self.EXPECTED.items()):
            with self.subTest(sample=name):
                result = self.feed(name)
                self.assertEqual(result["category"], "rejected", result)
                categories = [d["category"] for d in result["diagnostics"]]
                self.assertIn(category, categories, result)
        self.assertEqual(self.entity_ids(), ["A", "B"])
        self.assertEqual(self.rt.source_status("metro")["head_timestamp"], head_before)
        self.assertEqual(self.rt.ledger("metro"),
                         [row for row in self.rt.ledger("metro") if row["header_timestamp"] == 1000])
        # Every attempt is on the reception ledger with its diagnostics.
        rejected = [r for r in self.rt.receptions("metro") if r["category"] == "rejected"]
        self.assertEqual(len(rejected), len(self.EXPECTED))


class RaceTests(RealtimeTestCase):
    def outcomes(self, source: str) -> tuple:
        status = self.rt.source_status(source)
        entities = sorted(
            (n["entity_id"], n["replaced_at_header_timestamp"])
            for n in self.rt.notices(source, 10 ** 9, 10 ** 9,
                                     include_expired=True)["notices"])
        ledger = tuple(row["header_timestamp"] for row in self.rt.ledger(source))
        return status["head_timestamp"], entities, ledger

    FULL_FIRST = (2001, [("X", 2000), ("Y", 2001)], (2000, 2001))
    DIFF_FIRST = (2000, [("X", 2000)], (2000,))

    def test_sequential_orders_give_the_two_documented_outcomes(self):
        self.rt.register_source("race-full-first", "demo", "v1")
        self.assertEqual(self.feed("race-full.pb", "race-full-first")["category"], "applied")
        self.assertEqual(self.feed("race-diff.pb", "race-full-first")["category"], "applied")
        self.assertEqual(self.outcomes("race-full-first"), self.FULL_FIRST)

        self.rt.register_source("race-diff-first", "demo", "v1")
        self.assertEqual(self.feed("race-diff.pb", "race-diff-first")["category"], "rejected")
        self.assertEqual(self.feed("race-full.pb", "race-diff-first")["category"], "applied")
        self.assertEqual(self.outcomes("race-diff-first"), self.DIFF_FIRST)

    def test_competing_receivers_land_on_one_serial_outcome_and_survive_reopen(self):
        self.rt.register_source("race", "demo", "v1")
        self.store.close()  # both receivers open their own connections
        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def receiver(name: str, blob: bytes) -> None:
            store = Store(self.db_path)
            try:
                rt = RealtimeStore(store)
                barrier.wait()
                results[name] = rt.feed("race", blob)["category"]
            finally:
                store.close()

        threads = [threading.Thread(target=receiver, args=("full", sample("race-full.pb"))),
                   threading.Thread(target=receiver, args=("diff", sample("race-diff.pb")))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["full"], "applied")
        self.assertIn(results["diff"], ("applied", "rejected"))

        self.store = Store(self.db_path)
        self.rt = RealtimeStore(self.store)
        outcome = self.outcomes("race")
        self.assertIn(outcome, (self.FULL_FIRST, self.DIFF_FIRST))
        if outcome == self.FULL_FIRST:
            self.assertEqual(results["diff"], "applied")
        else:
            self.assertEqual(results["diff"], "rejected")
        # Reopening the database shows the same outcome: head, entities and
        # the success ledger still agree.
        self.store.close()
        self.store = Store(self.db_path)
        self.rt = RealtimeStore(self.store)
        self.assertEqual(self.outcomes("race"), outcome)


class CliTests(RealtimeTestCase):
    def test_cli_roundtrip(self):
        out = self.cli("--json", "rt-register", "--source", "cli", "--feed-key", "demo",
                       "--edition", "v1")
        self.assertFalse(out["already_registered"])
        message = str(RT / "m01-full-a-b.pb")
        out = self.cli("--json", "rt-feed", "--source", "cli", "--message", message)
        self.assertEqual(out["category"], "applied")
        out = self.cli("--json", "rt-feed", "--source", "cli", "--message", message)
        self.assertEqual(out["category"], "duplicate")
        out = self.cli("--json", "rt-notices", "--source", "cli", "--now", "1050",
                       "--max-age", "100")
        self.assertEqual([n["entity_id"] for n in out["notices"]], ["A", "B"])
        out = self.cli("--json", "rt-notices", "--source", "cli", "--now", "1101",
                       "--max-age", "100")
        self.assertEqual(out["notices"], [])
        out = self.cli("--json", "rt-receptions", "--source", "cli")
        self.assertEqual([r["category"] for r in out], ["applied", "duplicate"])
        out = self.cli("--json", "rt-sources")
        self.assertEqual(len(out), 2)  # metro (setUp) and cli
        bad = self.cli("--json", "rt-feed", "--source", "cli",
                       "--message", str(RT / "bad-unknown-trip.pb"), expect=1)
        self.assertEqual(bad["category"], "rejected")

    def test_cli_reception_blob_dump(self):
        self.cli("rt-feed", "--source", "metro", "--message", str(RT / "m01-full-a-b.pb"))
        out = self.cli("--json", "rt-receptions", "--source", "metro")
        blob_path = self.tmp / "dump.pb"
        self.cli("rt-reception", "--id", str(out[0]["id"]), "--blob", str(blob_path))
        self.assertEqual(blob_path.read_bytes(), sample("m01-full-a-b.pb"))


class StaticCoexistenceTests(RealtimeTestCase):
    def test_static_features_keep_working_next_to_realtime_tables(self):
        self.feed("m01-full-a-b.pb")
        result = self.store.expand("demo", "v1", "2026-03-30", "2026-03-30")
        self.assertEqual(result["days"][0]["service_date"], "20260330")
        rows = self.store.departures("demo", "v1", "2026-03-30", "007")
        self.assertTrue(rows)
        # Realtime state is still there and unchanged.
        self.assertEqual(self.entity_ids(), ["A", "B"])


if __name__ == "__main__":
    unittest.main()
