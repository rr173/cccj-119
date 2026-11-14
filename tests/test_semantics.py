"""Semantics: service-day selection, frequencies, DST anchor, isolation."""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from helpers import DIST, FIXTURES, StoreTestCase, build_all_fixtures, csv_text

from gtfs_store import model
from gtfs_store.errors import GtfsError
from gtfs_store.store import Store

EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))

MINIMAL_FILES = {
    "agency.txt": csv_text(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                           [["A", "Agency", "https://example.invalid", "Europe/Berlin"]]),
    "routes.txt": csv_text(["route_id", "route_type"], [["R", "3"]]),
    "calendar.txt": csv_text(
        ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
         "sunday", "start_date", "end_date"],
        [["S", "1", "1", "1", "1", "1", "1", "1", "20260101", "20261231"]]),
}


class DemoFixtureTests(StoreTestCase):
    """The documented fixture and its hand-written expectation file."""

    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()

    def setUp(self) -> None:
        super().setUp()
        result = self.load("demo", "v1", DIST / "demo-v1.zip")
        self.assertEqual(result["status"], "ready", result)

    def test_documented_day_start_utc_and_offsets(self):
        days = {d["service_date"]: d for d in EXPECTED["service_day_contract"]["days"]}
        self.expand("demo", "v1", "2026-03-28", "2026-03-30")
        self.expand("demo", "v1", "2026-10-24", "2026-10-26")
        for date_str, expected in days.items():
            row = self.sql(
                "SELECT day_start_utc FROM service_days WHERE feed_key='demo' AND edition='v1' "
                "AND service_date=?", (date_str.replace("-", ""),))[0]
            self.assertEqual(row["day_start_utc"], expected["day_start_utc"], date_str)

    def test_documented_sample_offsets_with_dst(self):
        cases = [("2026-03-28", 95400), ("2026-03-29", 90000), ("2026-10-24", 95400),
                 ("2026-10-25", 7200), ("2026-10-25", 95400)]
        for date_str, offset in cases:
            day = dt.date.fromisoformat(date_str)
            anchor = model.day_anchor_utc(day, ZoneInfo("Europe/Berlin"))
            local = model.local_at(anchor, offset, ZoneInfo("Europe/Berlin"))
            utc = model.utc_at_anchor(anchor, offset)
            entry = next(e for e in EXPECTED["service_day_contract"]["sample_offsets"][date_str]
                         if e["offset_seconds"] == offset)
            self.assertEqual(model.iso_local(local), entry["local"])
            self.assertEqual(model.utc_offset_text(local), entry["utc_offset"])
            self.assertEqual(model.iso_utc(utc), entry["utc"])

    def test_expected_stop_rows_for_2026_03_30(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        expected = EXPECTED["expected_departures"]["2026-03-30"]
        for stop, spec in expected.items():
            rows = self.rows("demo", "v1", "2026-03-30", stop)
            self.assertEqual(len(rows), spec["count"], stop)
            for notable in spec.get("notable", []):
                match = [r for r in rows if r["trip_id"] == notable["trip_id"]
                         and ("instance" not in notable
                              or r["trip_instance_id"] == notable["instance"])]
                self.assertTrue(match, "missing %r at %s" % (notable, stop))
                row = match[0]
                self.assertEqual(row["departure_seconds"], notable["departure_seconds"])
                if "local" in notable:
                    self.assertEqual(row["departure_local"], notable["local"])
                if "utc" in notable:
                    self.assertEqual(row["departure_utc"], notable["utc"])
                if "utc_offset" in notable:
                    self.assertEqual(row["utc_offset"], notable["utc_offset"])

    def test_headway_windows_are_not_concrete_trips(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        rows = self.rows("demo", "v1", "2026-03-30", "007")
        windows = [r for r in rows if r["certainty"] == "headway"]
        expected = EXPECTED["expected_departures"]["2026-03-30"]["007"]["headway_windows"]
        self.assertEqual(len(windows), len(expected))
        for row, want in zip(sorted(windows, key=lambda r: r["min_departure_seconds"]),
                             sorted(expected, key=lambda w: w["min"])):
            self.assertEqual(row["trip_id"], want["trip_id"])
            self.assertIsNotNone(row["frequency_row"])
            self.assertEqual(row["min_departure_seconds"], want["min"])
            self.assertEqual(row["max_departure_seconds"], want["max"])
            self.assertEqual(row["headway_secs"], want["headway_secs"])
            self.assertEqual(row["certainty"], "headway")
            # No concrete instance ids are invented for headway promises.
            self.assertTrue(row["trip_instance_id"].startswith("W:"))

    def test_exact_times_right_endpoint_excluded_and_adjacent_intervals(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        for stop in ("007", "S2"):
            # Later stops sit at their own offset from the template's stop 0.
            shift = 0 if stop == "007" else 600
            rows = self.rows("demo", "v1", "2026-03-30", stop)
            exact = [r for r in rows if r["certainty"] == "exact"]
            offsets = sorted(r["departure_seconds"] for r in exact
                             if r["trip_id"] == "T_FREQ_EXACT")
            # [09:30,12:00) step 10m: first 09:30+offset. Its right endpoint
            # (12:00) is excluded here and re-generated by the touching
            # [12:00,14:00) interval, so 12:00 appears exactly once.
            self.assertEqual(min(offsets), 34200 + shift)
            self.assertEqual(max(offsets), 49800 + shift)   # 13:50 + offset
            self.assertEqual(offsets.count(43200 + shift), 1)  # 12:00 + offset
            self.assertNotIn(50400 + shift, offsets)           # 14:00 + offset
            # Adjacent intervals [13:00,14:00) and [14:00,15:00): 14:00 appears
            # once, at the second interval's start, with separate identities.
            # This template has its own stop offset (S2 is +4 minutes).
            adj_shift = 0 if stop == "007" else 240
            adjacent = sorted(r["departure_seconds"] for r in exact
                              if r["trip_id"] == "T_ADJACENT")
            self.assertEqual(adjacent, [46800 + adj_shift, 48600 + adj_shift,
                                        50400 + adj_shift, 52200 + adj_shift])
            ids = sorted(r["trip_instance_id"] for r in exact if r["trip_id"] == "T_ADJACENT")
            self.assertEqual(len(set(ids)), 4)
            self.assertIn("F:T_ADJACENT:3:0", ids)
            self.assertIn("F:T_ADJACENT:4:0", ids)

    def test_late_departures_after_midnight_and_second_stop_past_window(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        late = [r for r in self.rows("demo", "v1", "2026-03-30", "007") if r["trip_id"] == "T_LATE"]
        self.assertEqual(late[0]["departure_seconds"], 90600)
        self.assertEqual(late[0]["departure_local"], "2026-03-31T01:10:00")
        self.assertEqual(late[0]["service_date"], "20260330")
        self.assertNotEqual(late[0]["service_date"], late[0]["departure_local"][:10].replace("-", ""))
        s2 = [r for r in self.rows("demo", "v1", "2026-03-30", "S2")
              if r["trip_id"] == "T_MIDNIGHT"]
        self.assertEqual([r["departure_seconds"] for r in s2], [85800, 87600, 89400])
        # 24:30 departure at stop 0 -> 24:50 at stop 1, beyond the window end.
        self.assertEqual(max(r["departure_seconds"] for r in s2), 89400)

    def test_frequency_template_not_emitted_itself(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        rows = self.rows("demo", "v1", "2026-03-30", "007")
        self.assertNotIn("T:T_FREQ_EXACT", {r["trip_instance_id"] for r in rows})
        self.assertNotIn("T:T_FREQ_WINDOW", {r["trip_instance_id"] for r in rows})
        self.assertIn("T:T_WKDY", {r["trip_instance_id"] for r in rows})

    def test_removed_and_added_weekend_and_orphan_days(self):
        self.expand("demo", "v1", "2026-03-28", "2026-04-04")
        day = {d["service_date"]: d for d in self.store.service_days(
            "demo", "v1", "2026-03-28", "2026-04-04")}
        self.assertEqual(day["20260329"]["departure_rows"], 0)
        self.assertEqual(day["20260329"]["trip_instances"], 0)
        self.assertEqual(day["20260403"]["enabled_services"] and True, True)
        rows = self.rows("demo", "v1", "2026-04-03", "007")
        self.assertEqual({r["trip_id"] for r in rows}, {"T_WKND", "T_WKND_LATE"})
        # An empty day never inherits the previous day's expansion.
        self.assertEqual(self.rows("demo", "v1", "2026-03-29"), [])
        self.assertEqual(self.rows("demo", "v1", "2026-03-28", "007") != [], True)

    def test_only_calendar_dates_service_and_id_7_vs_007(self):
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        rows_007 = self.rows("demo", "v1", "2026-03-30", "007")
        self.assertIn("T_ADDED", {r["trip_id"] for r in rows_007})
        rows_7 = self.rows("demo", "v1", "2026-03-30", "7")
        self.assertEqual(len(rows_7), 1)
        self.assertEqual(rows_7[0]["trip_id"], "T_ID7")
        self.assertEqual(rows_7[0]["stop_id"], "7")

    def test_expand_requires_explicit_dates_and_edition(self):
        with self.assertRaises(GtfsError):
            self.store.expand("demo", "v1", "2026-03-30", "2026-03-28")
        with self.assertRaises(GtfsError):
            self.store.expand("demo", "nope", "2026-03-30", "2026-03-30")
        with self.assertRaises(GtfsError):
            self.store.expand("demo", "v1", "2026-02-30", "2026-03-30")


class FrequencyValidationTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.demo = self.tmp / "demo"
        import shutil

        shutil.copytree(FIXTURES / "demo-v1", self.demo)
        self.base_texts = {p.name: p.read_text(encoding="utf-8-sig")
                           for p in self.demo.glob("*.txt")}

    def _package(self, **overrides: str) -> "Path":
        members = dict(self.base_texts)
        members.update(overrides)
        return self.make_zip("pkg.zip", members)

    def test_overlapping_intervals_are_rejected(self):
        freqs = csv_text(["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
                         [["T_FREQ_EXACT", "09:30:00", "12:00:00", "600", "1"],
                          ["T_FREQ_EXACT", "11:00:00", "13:00:00", "600", "1"]])
        result = self.rejected("f", "e", self._package(**{"frequencies.txt": freqs}))
        self.assertIn("overlapping_frequency", self.categories(result))

    def test_adjacent_intervals_are_accepted(self):
        freqs = csv_text(["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
                         [["T_FREQ_EXACT", "09:00:00", "11:00:00", "600", "1"],
                          ["T_FREQ_EXACT", "11:00:00", "13:00:00", "600", "1"]])
        result = self.load("f", "adj", self._package(**{"frequencies.txt": freqs}))
        self.assertEqual(result["status"], "ready", result)
        self.expand("f", "adj", "2026-03-30", "2026-03-30")
        offsets = [r["departure_seconds"] for r in self.rows("f", "adj", "2026-03-30", "007")
                   if r["trip_id"] == "T_FREQ_EXACT"]
        # 09:00,09:10,... exclusive of 13:00 -> last is 12:50.
        self.assertEqual(offsets[0], 32400)
        self.assertEqual(offsets[-1], 46200)
        self.assertEqual(offsets.count(39600), 1)

    def test_headway_must_be_positive(self):
        for value in ("0", "-600", "abc", ""):
            freqs = csv_text(["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
                             [["T_FREQ_EXACT", "09:00:00", "10:00:00", value, "1"]])
            result = self.rejected("f", "e" + value, self._package(**{"frequencies.txt": freqs}))
            self.assertTrue({"invalid_headway", "missing_value"} & self.categories(result))

    def test_missing_time_and_bad_ordering_are_rejected(self):
        stop_times = self.base_texts["stop_times.txt"].replace(
            "08:05:00,08:05:30,S2,20", ",08:05:30,S2,20", 1)
        result = self.rejected("f", "missing", self._package(**{"stop_times.txt": stop_times}))
        self.assertIn("missing_time", self.categories(result))

    def test_arrival_after_departure_quarantines_trip(self):
        stop_times = self.base_texts["stop_times.txt"].replace(
            "08:05:00,08:05:30,S2,20", "08:06:00,08:05:30,S2,20", 1)
        result = self.load("f", "q", self._package(**{"stop_times.txt": stop_times}))
        self.assertEqual(result["status"], "ready", result)
        self.assertIn("T_WKDY", result["quarantined_trips"])
        self.expand("f", "q", "2026-03-30", "2026-03-30")
        self.assertNotIn("T_WKDY", {r["trip_id"] for r in self.rows("f", "q", "2026-03-30", "007")})
        # The untouched times stay in the base table.
        rows = self.sql("SELECT arrival_seconds, departure_seconds FROM stop_times "
                        "WHERE feed_key='f' AND edition='q' AND trip_id='T_WKDY' "
                        "AND stop_sequence=20")
        self.assertEqual((rows[0]["arrival_seconds"], rows[0]["departure_seconds"]), (29160, 29130))

    def test_decreasing_arrival_quarantines_trip(self):
        stop_times = self.base_texts["stop_times.txt"].replace(
            "08:10:00,08:10:00,S3,30", "08:04:00,08:04:00,S3,30", 1)
        result = self.load("f", "q2", self._package(**{"stop_times.txt": stop_times}))
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual([t for t in result["quarantined_trips"]], ["T_WKDY"])
        self.expand("f", "q2", "2026-03-30", "2026-03-30")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM departures WHERE trip_id='T_WKDY'")[0][0], 0)
        # All other trips still expand.
        self.assertGreater(self.sql("SELECT COUNT(*) FROM departures")[0][0], 0)


class EdgeCaseTests(StoreTestCase):
    def test_single_stop_trip_is_allowed(self):
        stops = csv_text(["stop_id"], [["A"], ["B"]])
        stop_times = csv_text(
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            [["T", "08:00:00", "08:00:00", "A", "0"]])
        package = {**MINIMAL_FILES, "stops.txt": stops, "stop_times.txt": stop_times}
        # Remove the second trip so the package only declares one two-stop trip.
        package["trips.txt"] = csv_text(["route_id", "service_id", "trip_id"],
                                        [["R", "S", "T"]])
        result = self.load("f", "e", self.make_zip("one-stop.zip", package))
        self.assertEqual(result["status"], "ready", result)
        self.expand("f", "e", "2026-01-05", "2026-01-05")
        self.assertEqual(len(self.rows("f", "e", "2026-01-05", "A")), 1)

    def test_zero_padded_and_short_stop_sequence_both_ok(self):
        stops = csv_text(["stop_id"], [["A"], ["B"]])
        stop_times = csv_text(
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            [["T", "08:00:00", "08:00:00", "A", "000"],
             ["T", "08:10:00", "08:10:00", "B", "7"]])
        package = {**MINIMAL_FILES, "stops.txt": stops, "stop_times.txt": stop_times,
                   "trips.txt": csv_text(["route_id", "service_id", "trip_id"],
                                         [["R", "S", "T"]])}
        result = self.load("f", "e", self.make_zip("seq.zip", package))
        self.assertEqual(result["status"], "ready", result)
        seqs = [r["stop_sequence"] for r in self.sql(
            "SELECT stop_sequence FROM stop_times WHERE feed_key='f' ORDER BY stop_sequence")]
        self.assertEqual(seqs, [0, 7])


class IsolationTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()

    def test_editions_and_feed_keys_do_not_mix(self):
        self.load("demo", "v1", DIST / "demo-v1.zip")
        self.load("demo", "v2", DIST / "demo-v2.zip")
        self.load("other", "v1", DIST / "other-operator.zip")
        for feed, edition in (("demo", "v1"), ("demo", "v2"), ("other", "v1")):
            self.expand(feed, edition, "2026-03-30", "2026-03-30")
        v1 = {r["trip_id"] for r in self.rows("demo", "v1", "2026-03-30")}
        v2 = {r["trip_id"] for r in self.rows("demo", "v2", "2026-03-30")}
        other = {r["trip_id"] for r in self.rows("other", "v1", "2026-03-30")}
        self.assertTrue(v1 < v2, (v1, v2))
        self.assertEqual(v1, {"T_WKDY", "T_ADDED", "T_FREQ_WINDOW",
                              "T_FREQ_EXACT", "T_LATE", "T_WKND", "T_ADJACENT", "T_TWO_STOPS",
                              "T_WKND_LATE", "T_ID7", "T_MIDNIGHT", "T_FREQ_DEFAULT"})
        self.assertIn("T_WKDY_V2_EXTRA", v2 - v1)
        # Same internal ids, different operator: rows never join.
        self.assertEqual(other, {"T_WKDY"})
        self.assertEqual(len(self.rows("other", "v1", "2026-03-30", "007")), 1)
        self.assertEqual(self.rows("other", "v1", "2026-03-30", "007")[0]["departure_local"],
                         "2026-03-30T16:00:00")
        self.assertEqual(self.rows("demo", "v1", "2026-03-30", "007")[0]["trip_id"],
                         "T_FREQ_WINDOW")
        self.assertNotEqual(
            self.sql("SELECT COUNT(*) FROM departures WHERE feed_key='demo' AND edition='v1'")[0][0],
            0)

    def test_rejected_package_is_visible_but_not_expandable(self):
        self.load("rej", "overlap", DIST / "reject-overlap.zip")
        report = self.store.rejection_report("rej")
        self.assertEqual(report["editions"][0]["status"], "rejected")
        self.assertTrue(any(p["file_name"] == "frequencies.txt" for p in report["problems"]))
        self.assertTrue(all(p["line_number"] is not None for p in report["problems"]))
        with self.assertRaises(GtfsError):
            self.expand("rej", "overlap", "2026-03-30", "2026-03-30")


class AtomicityTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()

    def setUp(self) -> None:
        super().setUp()
        self.load("demo", "v1", DIST / "demo-v1.zip")

    def test_replacement_is_idempotent(self):
        self.expand("demo", "v1", "2026-03-28", "2026-04-04")
        before = self._snapshot()
        self.expand("demo", "v1", "2026-03-28", "2026-04-04")
        self.assertEqual(self._snapshot(), before)
        self.assertGreater(len(before), 0)

    def test_interrupted_replacement_leaves_previous_result_intact(self):
        self.expand("demo", "v1", "2026-03-28", "2026-04-04")
        before = self._snapshot()
        with mock.patch.dict(os.environ, {"GTFS_FAULT_EXPAND_AFTER_DAYS": "2"}):
            with self.assertRaises(GtfsError) as ctx:
                self.store.expand("demo", "v1", "2026-03-28", "2026-04-04")
        self.assertIn("fault injection", str(ctx.exception))
        # Same connection sees the previous complete result; a *new* reader
        # must too, so open a second connection.
        self.assertEqual(self._snapshot(), before)
        second = Store(self.db_path)
        try:
            rows = second.departures("demo", "v1", "20260330")
            days = second.service_days("demo", "v1", "20260328", "20260404")
        finally:
            second.close()
        self.assertEqual(rows, self.rows("demo", "v1", "2026-03-30"))
        self.assertTrue(all(d["departure_rows"] >= 0 for d in days))
        self.assertEqual(len(days), 8)
        # Redo works and matches the old result exactly.
        self.expand("demo", "v1", "2026-03-28", "2026-04-04")
        self.assertEqual(self._snapshot(), before)

    def test_concurrent_reader_never_sees_a_partial_replacement(self):
        import threading
        import time

        short = ("2026-03-27", "2026-05-15")
        wide = ("2026-03-27", "2026-08-31")
        self.expand("demo", "v1", *short)
        counts = {len(self.store.service_days("demo", "v1", *short))}
        stop = threading.Event()
        seen = []

        def poll():
            reader = Store(self.db_path)
            try:
                while not stop.is_set():
                    rows = reader.service_days("demo", "v1", "2026-03-27", "2026-08-31")
                    # Every observed state must be one of the two complete
                    # ranges, never an intermediate mix.
                    seen.append(len(rows))
            finally:
                reader.close()

        thread = threading.Thread(target=poll)
        thread.start()
        try:
            time.sleep(0.05)
            self.expand("demo", "v1", *wide)
            time.sleep(0.05)
        finally:
            stop.set()
            thread.join()
        allowed_floors = {len(self.store.service_days("demo", "v1", *short)),
                          len(self.store.service_days("demo", "v1", *wide))}
        self.assertTrue(seen)
        # WAL gives the reader a consistent snapshot; only complete states appear.
        self.assertTrue(set(seen) <= allowed_floors, sorted(set(seen)))

    def test_replacement_with_different_range_only_touches_those_dates(self):
        self.expand("demo", "v1", "2026-03-27", "2026-04-05")
        snapshot = self._snapshot()
        keep = [k for k in snapshot if k[0] != "20260330"]
        self.expand("demo", "v1", "2026-03-30", "2026-03-30")
        after = self._snapshot()
        self.assertEqual([k for k in after if k[0] != "20260330"], keep)
        # Same-day rows must be identical (the single-day rebuild reproduces it).
        self.assertEqual([k for k in after if k[0] == "20260330"],
                         [k for k in snapshot if k[0] == "20260330"])

    def _snapshot(self) -> list[tuple]:
        rows = self.sql(
            "SELECT service_date, trip_instance_id, stop_id, stop_sequence, departure_seconds, "
            "departure_local, utc_offset, certainty, min_departure_seconds, headway_secs "
            "FROM departures WHERE feed_key='demo' AND edition='v1' "
            "ORDER BY service_date, trip_instance_id, stop_sequence")
        return [tuple(r) for r in rows]


class CliTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()

    def test_end_to_end_cli(self):
        loaded = self.cli("--json", "load", "--feed-key", "demo", "--edition", "v1",
                          "--package", str(DIST / "demo-v1.zip"))
        self.assertEqual(loaded["status"], "ready")
        self.cli("expand", "--feed-key", "demo", "--edition", "v1",
                 "--start", "2026-03-30", "--end", "2026-03-30")
        payload = self.cli("--json", "departures", "--feed-key", "demo", "--edition", "v1",
                           "--date", "2026-03-30", "--stop", "007")
        self.assertEqual(len(payload), 41)
        text = self.cli("departures", "--feed-key", "demo", "--edition", "v1",
                        "--date", "2026-03-30", "--stop", "7")
        self.assertIn("dep=15:00:00", text)

    def test_cli_can_read_canonical_base_tables(self):
        self.cli("load", "--feed-key", "demo", "--edition", "v1",
                 "--package", str(DIST / "demo-v1.zip"))
        payload = self.cli("--json", "show", "--feed-key", "demo", "--edition", "v1",
                           "--table", "stop_times", "--limit", "2")
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["stop_id"], "007")
        # Unknown table is a usage error, not a crash.
        self.cli("show", "--feed-key", "demo", "--edition", "v1", "--table", "deps",
                 expect=2)

    def test_cli_rejection_exit_code_and_report(self):
        self.cli("load", "--feed-key", "demo", "--edition", "bad",
                 "--package", str(DIST / "reject-foreign-key.zip"), expect=1)
        report = self.cli("--json", "rejections", "--feed-key", "demo")
        self.assertEqual(report["editions"][0]["status"], "rejected")
        self.assertTrue(any(p["category"] == "foreign_key" for p in report["problems"]))
        self.assertTrue(any(p["file_name"] == "stop_times.txt" for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
