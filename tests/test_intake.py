"""Intake: archive safety, CSV dialect, required files, keys, timezone."""

from __future__ import annotations

import csv
import io
import json
import unittest
import zipfile
from pathlib import Path

from helpers import DIST, FIXTURES, StoreTestCase, build_all_fixtures, csv_text

MINIMAL = {
    "agency.txt": csv_text(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                           [["A", "Agency", "https://example.invalid", "Europe/Berlin"]]),
    "stops.txt": csv_text(["stop_id", "stop_name"], [["007", "Seven"], ["7", "Seven2"]]),
    "routes.txt": csv_text(["route_id", "route_type"], [["R", "3"]]),
    "trips.txt": csv_text(["route_id", "service_id", "trip_id"],
                          [["R", "S", "T"], ["R", "S", "T2"]]),
    "stop_times.txt": csv_text(
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        [["T", "08:00:00", "08:00:00", "007", "1"],
         ["T", "08:10:00", "08:10:00", "7", "2"],
         ["T2", "09:00:00", "09:00:00", "007", "1"],
         ["T2", "09:10:00", "09:10:00", "7", "2"]]),
    "calendar.txt": csv_text(
        ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
         "sunday", "start_date", "end_date"],
        [["S", "1", "0", "0", "0", "0", "0", "0", "20260101", "20261231"]]),
}


class IntakeTests(StoreTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()

    def test_minimal_package_loads_and_ids_keep_leading_zeros(self):
        result = self.load("f", "e", self.make_zip("min.zip", MINIMAL))
        self.assertEqual(result["status"], "ready")
        ids = [r["stop_id"] for r in self.sql(
            "SELECT stop_id FROM stops WHERE feed_key='f' AND edition='e' ORDER BY rowid")]
        self.assertEqual(ids, ["007", "7"])
        self.assertNotIn("7", ids[:1])
        # Identifiers survive verbatim in trip keys too.
        self.assertEqual(self.sql("SELECT COUNT(*) FROM stop_times WHERE feed_key='f'")[0][0], 4)

    def test_utf8_bom_and_quoted_commas_and_newlines(self):
        stops = csv_text(["stop_id", "stop_name", "stop_desc"],
                         [["007", "Comma, Street", "line one\nline two"],
                          ["7", "Quoted \"stop\"", "desc"]], bom=True)
        members = {**MINIMAL, "stops.txt": stops}
        result = self.load("f", "e", self.make_zip("bom.zip", members))
        self.assertEqual(result["status"], "ready", result)
        names = dict(self.sql("SELECT stop_id, stop_name FROM stops WHERE feed_key='f'"))
        self.assertEqual(names["007"], "Comma, Street")
        self.assertEqual(names["7"], 'Quoted "stop"')

    def test_bom_is_stripped_from_header(self):
        text = MINIMAL["agency.txt"]
        self.assertTrue(text.startswith("﻿") or not text.startswith("﻿"))
        bommed = "﻿" + MINIMAL["stops.txt"]
        result = self.load("f", "e", self.make_zip("bom2.zip", {**MINIMAL, "stops.txt": bommed}))
        self.assertEqual(result["status"], "ready", result)
        cols = [r[1] for r in self.sql("PRAGMA table_info(stops)")]
        self.assertNotIn("﻿stop_id", cols)

    def test_identical_resend_is_a_no_op_and_changed_content_conflicts(self):
        package = self.make_zip("min.zip", MINIMAL)
        first = self.load("f", "e", package)
        self.assertEqual(first["status"], "ready")
        before = [tuple(r) for r in self.sql("SELECT * FROM stop_times ORDER BY trip_id, stop_sequence")]
        again = self.load("f", "e", package)
        self.assertTrue(again["resend"])
        after = [tuple(r) for r in self.sql("SELECT * FROM stop_times ORDER BY trip_id, stop_sequence")]
        self.assertEqual(before, after)
        other = {**MINIMAL, "stops.txt": csv_text(["stop_id"], [["007"], ["7"], ["S9"]])}
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", self.make_zip("changed.zip", other))
        self.assertIn("different contents", str(ctx.exception))

    def test_rejection_does_not_touch_existing_ready_edition(self):
        self.load("f", "good", self.make_zip("min.zip", MINIMAL))
        self.expand("f", "good", "2026-01-05", "2026-01-05")
        bad = {**MINIMAL, "stops.txt": csv_text(["stop_id"], [["007"], ["007"]])}
        result = self.rejected("f", "bad", self.make_zip("bad.zip", bad))
        self.assertIn("duplicate_id", self.categories(result))
        self.assertEqual(self.sql(
            "SELECT COUNT(*) FROM departures WHERE feed_key='f' AND edition='good'")[0][0] > 0,
            True)
        with self.assertRaises(Exception):
            self.expand("f", "bad", "2026-01-05", "2026-01-05")

    def test_problems_carry_file_line_field(self):
        bad = {**MINIMAL,
               "calendar.txt": csv_text(
                   ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday", "start_date", "end_date"],
                   [["S", "2", "0", "0", "0", "0", "0", "0", "20260101", "20261231"]])}
        result = self.rejected("f", "e", self.make_zip("bad-weekday.zip", bad))
        problem = [p for p in result["problems"] if p["category"] == "invalid_weekday"][0]
        self.assertEqual(problem["file"], "calendar.txt")
        self.assertEqual(problem["line"], 2)
        self.assertEqual(problem["field"], "monday")
        self.assertIn("'2'", problem["message"])

        bad_date = {**MINIMAL,
                    "calendar.txt": csv_text(
                        ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                         "saturday", "sunday", "start_date", "end_date"],
                        [["S", "1", "0", "0", "0", "0", "0", "0", "20261301", "20261231"]])}
        result = self.rejected("f", "e2", self.make_zip("bad-date.zip", bad_date))
        self.assertIn("invalid_date", self.categories(result))

    def test_required_files_and_calendar_choice(self):
        members = dict(MINIMAL)
        del members["trips.txt"]
        result = self.rejected("f", "no-trips", self.make_zip("no-trips.zip", members))
        self.assertIn("missing_file", self.categories(result))

        members = {k: v for k, v in MINIMAL.items() if k != "calendar.txt"}
        result = self.rejected("f", "no-cal", self.make_zip("no-cal.zip", members))
        self.assertTrue(any(p["category"] == "missing_file" and "calendar" in p["message"]
                            for p in result["problems"]))

    def test_calendar_dates_alone_is_sufficient_for_a_service(self):
        members = {k: v for k, v in MINIMAL.items() if k != "calendar.txt"}
        members["calendar_dates.txt"] = csv_text(
            ["service_id", "date", "exception_type"], [["S", "20260105", "1"]])
        result = self.load("f", "only-dates", self.make_zip("only-dates.zip", members))
        self.assertEqual(result["status"], "ready", result)
        self.expand("f", "only-dates", "2026-01-05", "2026-01-05")
        self.assertEqual(len(self.rows("f", "only-dates", "2026-01-05", "007")), 2)

    def test_timezone_must_be_single_and_iana(self):
        bad = {**MINIMAL,
               "agency.txt": csv_text(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                                      [["A", "A", "https://x.invalid", "Europe/Berlin"],
                                       ["B", "B", "https://y.invalid", "Europe/Paris"]])}
        result = self.rejected("f", "tz", self.make_zip("tz.zip", bad))
        self.assertIn("timezone", self.categories(result))

        bad = {**MINIMAL,
               "agency.txt": csv_text(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                                      [["A", "A", "https://x.invalid", "Mars/Olympus"]])}
        result = self.rejected("f", "tz2", self.make_zip("tz2.zip", bad))
        self.assertIn("invalid_timezone", self.categories(result))

    def test_duplicate_ids_orphan_refs_and_sequences(self):
        bad = {**MINIMAL,
               "stop_times.txt": csv_text(
                   ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                   [["T", "08:00:00", "08:00:00", "007", "1"],
                    ["T", "08:10:00", "08:10:00", "MISSING", "2"],
                    ["T", "08:20:00", "08:20:00", "7", "1"]])}
        result = self.rejected("f", "fk", self.make_zip("fk.zip", bad))
        categories = self.categories(result)
        self.assertIn("foreign_key", categories)
        self.assertIn("duplicate_id", categories)

    def test_route_agency_id_must_resolve(self):
        bad = {**MINIMAL,
               "routes.txt": csv_text(["route_id", "agency_id", "route_type"],
                                      [["R", "NOPE", "3"]])}
        result = self.rejected("f", "agency-fk", self.make_zip("agency-fk.zip", bad))
        self.assertIn("foreign_key", self.categories(result))

    def test_time_and_integer_grammar_is_strict(self):
        for bad_time in ("8:00:00", "08:0:00", "08:00:60", " 08:00:00", "8:00"):
            stop_times = csv_text(
                ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                [["T", bad_time, "08:00:00", "007", "1"],
                 ["T", "08:10:00", "08:10:00", "7", "2"]])
            result = self.rejected("f", "t" + bad_time, self.make_zip(
                "badtime.zip", {**MINIMAL, "stop_times.txt": stop_times}))
            self.assertIn("invalid_time", self.categories(result), bad_time)
        stop_times = csv_text(
            ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            [["T", "08:00:00", "08:00:00", "007", "+1"],
             ["T", "08:10:00", "08:10:00", "7", "2"]])
        result = self.rejected("f", "plus", self.make_zip(
            "plus.zip", {**MINIMAL, "stop_times.txt": stop_times}))
        self.assertIn("invalid_value", self.categories(result))

    def test_calendar_dates_conflict_is_not_order_based(self):
        members = {**MINIMAL,
                   "calendar_dates.txt": csv_text(
                       ["service_id", "date", "exception_type"],
                       [["S", "20260105", "1"], ["S", "20260105", "2"]])}
        result = self.rejected("f", "conflict", self.make_zip("conflict.zip", members))
        conflict = [p for p in result["problems"] if p["category"] == "conflict"][0]
        self.assertEqual(conflict["file"], "calendar_dates.txt")
        self.assertEqual(conflict["line"], 3)
        # Even same-type duplicates are a conflict, not a silently deduplicated row.
        members = {**MINIMAL,
                   "calendar_dates.txt": csv_text(
                       ["service_id", "date", "exception_type"],
                       [["S", "20260105", "1"], ["S", "20260105", "1"]])}
        result = self.rejected("f", "conflict2", self.make_zip("conflict2.zip", members))
        self.assertIn("conflict", self.categories(result))


class UnsupportedSemanticsTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        import shutil

        self.dir = self.tmp / "demo"
        shutil.copytree(FIXTURES / "demo-v1", self.dir)
        self.texts = {p.name: p.read_text(encoding="utf-8").lstrip("\ufeff")
                      for p in self.dir.glob("*.txt")}

    def _pkg(self, **overrides: str) -> Path:
        members = dict(self.texts)
        members.update(overrides)
        return self.make_zip("unsupported.zip", members)

    def test_continuous_stopping_is_named_explicitly(self):
        header = "trip_id,arrival_time,departure_time,stop_id,stop_sequence,continuous_pickup"
        rows = ["T_WKDY,08:00:00,08:00:00,007,10,1",
                "T_WKDY,08:05:00,08:05:00,S2,20,0",
                "T_WKDY,08:10:00,08:10:00,S3,30,0"]
        body = header + "\r\n" + "\r\n".join(rows) + "\r\n"
        trips = "\r\n".join([self.texts["trips.txt"].splitlines()[0],
                             "R1,WKDY,T_WKDY,East,0"]) + "\r\n"
        result = self.rejected("f", "e", self._pkg(**{
            "stop_times.txt": body, "trips.txt": trips,
            "frequencies.txt": self.texts["frequencies.txt"].splitlines()[0] + "\r\n"}))
        self.assertEqual(self.categories(result), {"unsupported_semantics"})
        messages = " ".join(p["message"] for p in result["problems"])
        self.assertIn("continuous", messages)
        self.assertIn("does not model", messages)

    def test_pickup_type_1_is_not_a_boardable_departure(self):
        header = "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type"
        rows = ["T_WKDY,08:00:00,08:00:00,007,10,0",
                "T_WKDY,08:05:00,08:05:00,S2,20,1",
                "T_WKDY,08:10:00,08:10:00,S3,30,0"]
        body = header + "\r\n" + "\r\n".join(rows) + "\r\n"
        trips_lines = self.texts["trips.txt"].splitlines()
        trips = "\r\n".join([trips_lines[0]] +
                             [l for l in trips_lines[1:] if l.startswith("R1,WKDY,T_WKDY,")]) + "\r\n"
        result = self.load("f", "e", self._pkg(**{
            "stop_times.txt": body, "trips.txt": trips,
            "frequencies.txt": self.texts["frequencies.txt"].splitlines()[0] + "\r\n"}))
        self.assertEqual(result["status"], "ready", result)
        self.expand("f", "e", "2026-03-30", "2026-03-30")
        stop_ids = {r["stop_id"] for r in self.rows("f", "e", "2026-03-30")}
        self.assertIn("007", stop_ids)
        self.assertIn("S3", stop_ids)
        self.assertNotIn("S2", stop_ids)


class ArchiveTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = dict(MINIMAL)

    def _zip_with_raw_members(self, members: list[tuple[str, bytes]]) -> Path:
        path = self.tmp / "raw.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members:
                zf.writestr(zipfile.ZipInfo(name), data)
        return path

    def test_duplicate_member_name_is_refused(self):
        path = self.tmp / "dup.zip"
        import warnings

        with warnings.catch_warnings():
            # The writer itself warns about the duplicate it is creating.
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(path, "w") as zf:
                for name, text in self.base.items():
                    zf.writestr(name, text)
                zf.writestr("agency.txt", "second copy")
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("duplicate member", str(ctx.exception))

    def test_path_traversal_is_refused(self):
        path = self.tmp / "traverse.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in self.base.items():
                zf.writestr(name, text)
            zf.writestr("../evil.txt", "x")
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("escapes", str(ctx.exception))

    def test_subdirectory_and_unsupported_member_are_refused(self):
        path = self.tmp / "sub.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in self.base.items():
                zf.writestr("feed/" + name, text)
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("directory", str(ctx.exception))

        path = self.tmp / "extra.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in self.base.items():
                zf.writestr(name, text)
            zf.writestr("notes.txt", "not GTFS")
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("outside the accepted GTFS file set", str(ctx.exception))

    def test_size_limit_is_refused(self):
        from gtfs_store import archive

        path = self.tmp / "big.zip"
        members = dict(self.base)
        members["stops.txt"] = "stop_id\n" + "x" * (archive.MAX_MEMBER_BYTES + 1)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            for name, text in members.items():
                zf.writestr(name, text)
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("limit", str(ctx.exception))

    def test_not_a_zip(self):
        path = self.tmp / "notzip.zip"
        path.write_bytes(b"this is not a zip file")
        with self.assertRaises(Exception) as ctx:
            self.load("f", "e", path)
        self.assertIn("not a readable ZIP", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
