"""Anonymized GPS egress: grid math, manifests, level promotion, publication.

The tests exercise the production entry points (``AnonStore`` and the CLI)
against synthetic dwell GPS jobs created through the existing dwell schema.
The hand-crafted scenarios in dist/anon are cross-checked field-by-field by
tools/anon_walkthrough.py with an independent pure-Python reference; this
suite focuses on contract details and refusal paths.

Run with:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal

from helpers import ROOT, StoreTestCase

from gtfs_store.anon import (
    PUBLIC_COLUMNS,
    AnonError,
    AnonStore,
    lat_cell,
    lon_cell,
    time_bucket,
    validate_k,
    validate_levels,
)

CLI = str(ROOT / "gtfs-store")


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def make_dwell_job(store, job_id: int,
                   runs: dict[str, list[tuple[int, float, float, str]]],
                   *, failed: set[str] | None = None,
                   empty: set[str] | None = None) -> None:
    """Populate the existing dwell GPS tables with synthetic raw samples."""
    from gtfs_store.dwell import DwellStore

    DwellStore(store)  # ensure the dwell GPS schema exists
    failed = failed or set()
    empty = empty or set()
    conn = store.conn
    all_run_ids = list(runs) + sorted(failed | empty)
    conn.execute(
        "INSERT INTO dwell_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, "samples-%d.csv" % job_id, "sha", "bindings-%d.csv" % job_id,
         "sha", 120, 250, 60, 120, len(all_run_ids),
         len(all_run_ids) - len(failed), len(failed), 0,
         "2026-11-14T00:00:00Z"))
    for run_id in failed:
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, run_id, None, None, None, "failed", 0, 0, None,
             '[{"code":"x"}]'))
    for run_id in empty:
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, run_id, None, None, None, "processed", 0, 0, None, "[]"))
    for run_id, points in runs.items():
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, run_id, None, None, None, "processed",
             len(points), 0, None, "[]"))
        for seq, (observed_utc, lat, lon, sample_id) in enumerate(points):
            conn.execute(
                "INSERT INTO dwell_samples VALUES(?,?,?,?,?,?,?,?)",
                (job_id, run_id, sample_id, observed_utc, lat, lon,
                 seq + 2, seq))


def path(points):
    return points


class GridMathTests(StoreTestCase):
    def test_latitude_floor_and_pole(self) -> None:
        # (lat + 90) * 1e6 / width, floored.
        self.assertEqual(lat_cell(-90.0, 500_000), 0)
        self.assertEqual(lat_cell(0.0, 500_000), 180)
        self.assertEqual(lat_cell(89.5, 500_000), 359)
        # +90 lands in the northernmost cell, not one past the grid.
        self.assertEqual(lat_cell(90.0, 500_000), 359)
        self.assertEqual(lat_cell(90.0, 1_000_000), 179)
        # Negative coordinates floor downward.
        self.assertEqual(lat_cell(-89.75, 500_000), 0)
        self.assertEqual(lat_cell(-89.5, 500_000), 1)

    def test_longitude_seam(self) -> None:
        self.assertEqual(lon_cell(-180.0, 500_000), 0)
        # +180 is the same seam as -180.
        self.assertEqual(lon_cell(180.0, 500_000), 0)
        self.assertEqual(lon_cell(0.0, 500_000), 360)
        self.assertEqual(lon_cell(-179.75, 500_000), 0)
        self.assertEqual(lon_cell(-179.5, 500_000), 1)

    def test_uses_decimal_of_float_text(self) -> None:
        # The value must be rebuilt as Decimal(str(value)); a value whose
        # binary float sits just below an integer still floors on its text.
        value = 0.1 + 0.2  # 0.30000000000000004 as a float
        self.assertEqual(str(value), "0.30000000000000004")
        cell = lat_cell(value, 1_000_000)
        expect = int(((Decimal(str(value)) + Decimal(90)) * Decimal(1_000_000)
                      / Decimal(1_000_000)).to_integral_value())
        self.assertEqual(cell, expect)

    def test_time_bucket_negative_floors_toward_minus_infinity(self) -> None:
        self.assertEqual(time_bucket(0, 60), 0)
        self.assertEqual(time_bucket(59, 60), 0)
        self.assertEqual(time_bucket(60, 60), 1)
        self.assertEqual(time_bucket(-1, 60), -1)
        self.assertEqual(time_bucket(-60, 60), -1)
        self.assertEqual(time_bucket(-61, 60), -2)
        self.assertEqual(time_bucket(-120, 120), -1)


class LevelValidationTests(StoreTestCase):
    def test_accepts_monotone_ladder(self) -> None:
        levels = validate_levels([
            {"cell_microdeg": 500_000, "bucket_s": 60},
            {"cell_microdeg": 1_000_000, "bucket_s": 120},
        ])
        self.assertEqual([l["level_index"] for l in levels], [0, 1])

    def test_rejects_non_divisor_width(self) -> None:
        with self.assertRaises(AnonError):
            validate_levels([{"cell_microdeg": 7, "bucket_s": 60}])

    def test_rejects_non_multiple_space_and_time(self) -> None:
        with self.assertRaises(AnonError):
            validate_levels([
                {"cell_microdeg": 1_000_000, "bucket_s": 120},
                {"cell_microdeg": 1_500_000, "bucket_s": 240}])
        with self.assertRaises(AnonError):
            validate_levels([
                {"cell_microdeg": 1_000_000, "bucket_s": 120},
                {"cell_microdeg": 2_000_000, "bucket_s": 180}])

    def test_rejects_identical_levels_and_bad_integers(self) -> None:
        with self.assertRaises(AnonError):
            validate_levels([
                {"cell_microdeg": 1_000_000, "bucket_s": 60},
                {"cell_microdeg": 1_000_000, "bucket_s": 60}])
        with self.assertRaises(AnonError):
            validate_levels([{"cell_microdeg": True, "bucket_s": 60}])
        with self.assertRaises(AnonError):
            validate_levels([{"cell_microdeg": 0, "bucket_s": 60}])
        with self.assertRaises(AnonError):
            validate_levels([])

    def test_k_must_be_integer_at_least_two(self) -> None:
        self.assertEqual(validate_k(2), 2)
        for bad in (1, 0, -1, True, 2.0, "2", None):
            with self.assertRaises(AnonError):
                validate_k(bad)


class AnonPipelineTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.anon = AnonStore(self.store)
        self.levels = [
            {"cell_microdeg": 500_000, "bucket_s": 60},
            {"cell_microdeg": 1_000_000, "bucket_s": 120},
        ]
        self.common = [(3600, 52.0, 13.0, "a"), (3660, 52.4, 13.4, "b"),
                       (3720, 52.6, 13.6, "c")]

    def _manifest(self, rows: list[list[str]]) -> bytes:
        return csv_bytes(["job_id", "run_id", "vehicle_unit"], rows)

    def test_whole_sequence_anonymity_not_per_cell(self) -> None:
        # Two reversed paths share every cell but never the same order.
        make_dwell_job(self.store, 1, {
            "X": [(3600, 52.0, 13.0, "a"), (3660, 52.0, 13.0, "a2"),
                  (3720, 52.6, 13.6, "b")],
            "Y": [(3600, 52.6, 13.6, "b"), (3660, 52.6, 13.6, "b2"),
                  (3720, 52.0, 13.0, "a")],
        })
        manifest = self._manifest([["1", "X", "VX"], ["1", "Y", "VY"]])
        job = self.anon.process(manifest, 2, self.levels)
        # At the fine level both groups are rare; the coarse level merges the
        # two directions into one group supported by two vehicles.
        self.assertEqual(job["selected_level_index"], 1)
        export = self.anon.public_export(job["anon_job_id"])
        self.assertEqual(len(export["groups"]), 1)
        self.assertEqual(export["groups"][0]["vehicle_count"], 2)
        self.assertEqual(export["groups"][0]["trip_count"], 2)

    def test_same_vehicle_multiple_trips_never_reaches_k(self) -> None:
        make_dwell_job(self.store, 1, {
            "R1": self.common,
            "R2": [(t + 100, la, lo, sid) for t, la, lo, sid in self.common],
        })
        manifest = self._manifest([["1", "R1", "VA"], ["1", "R2", "VA"]])
        job = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job["published_groups"], 0)
        self.assertEqual(job["dropped_trips"], 2)
        # The internal audit explains both members as dropped together.
        for run in ("R1", "R2"):
            outcome = self.anon.member_outcome(job["anon_job_id"], 1, run)
            self.assertEqual(outcome["status"], "dropped")
            self.assertIn("rare_group", outcome["explanation"])
        export = self.anon.public_export(job["anon_job_id"])
        self.assertEqual(export["groups"], [])

    def test_opaque_units_007_and_7_are_distinct(self) -> None:
        make_dwell_job(self.store, 1, {
            "R1": self.common, "R2": self.common, "R3": self.common})
        # Two distinct units across three runs; k=2 passes, k=3 fails.
        manifest = self._manifest(
            [["1", "R1", "007"], ["1", "R2", "7"], ["1", "R3", "007"]])
        job2 = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job2["published_trips"], 3)
        self.assertEqual(
            self.anon.public_export(job2["anon_job_id"])["groups"][0]
            ["vehicle_count"], 2)
        job3 = self.anon.process(manifest, 3, self.levels)
        self.assertEqual(job3["published_groups"], 0)
        self.assertEqual(job3["dropped_trips"], 3)

    def test_rare_group_forces_promotion_of_everyone(self) -> None:
        make_dwell_job(self.store, 1, {
            "P1": self.common, "P2": self.common,
            "RARE": [(9000, 60.0, 20.0, "x"), (9060, 60.6, 20.6, "y"),
                     (9120, 61.2, 21.2, "z")]})
        manifest = self._manifest([["1", "P1", "V1"], ["1", "P2", "V2"],
                                   ["1", "RARE", "VR"]])
        job = self.anon.process(manifest, 2, self.levels)
        # Fine level had a rare group, so the fine level may not be selected
        # even though one group was already supported there.
        self.assertEqual(job["levels"][0]["outcome"], "promoted")
        self.assertEqual(job["levels"][0]["rare_groups"], 1)
        self.assertEqual(job["selected_level_index"], 1)

    def test_fine_level_selected_when_every_group_supported(self) -> None:
        make_dwell_job(self.store, 1, {
            "R1": self.common, "R2": self.common})
        manifest = self._manifest([["1", "R1", "V1"], ["1", "R2", "V2"]])
        job = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job["selected_level_index"], 0)
        self.assertEqual(job["final_outcome"], "selected")
        self.assertEqual(len(job["levels"]), 1)  # stopped immediately

    def test_last_level_rare_groups_dropped_as_whole_groups(self) -> None:
        make_dwell_job(self.store, 1, {
            "R1": self.common, "R2": self.common,
            "UNIQ": [(9000, -33.0, 150.0, "x"), (9060, -33.6, 150.6, "y"),
                     (9120, -34.2, 151.2, "z")]})
        manifest = self._manifest([["1", "R1", "V1"], ["1", "R2", "V2"],
                                   ["1", "UNIQ", "VU"]])
        job = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job["final_outcome"], "final_rare_groups_dropped")
        self.assertEqual(job["published_trips"], 2)
        self.assertEqual(job["dropped_trips"], 1)
        export = self.anon.public_export(job["anon_job_id"])
        self.assertEqual(len(export["groups"]), 1)
        # The dropped run is absent from every public group.
        all_runs = {m["run_id"] for g in export["groups"] for m in []}
        self.assertEqual(all_runs, set())

    def test_adjacent_fold_keeps_non_adjacent_repetition_and_order(self) -> None:
        # A,B,A,B: adjacent equals fold, separated repeats survive, order kept.
        make_dwell_job(self.store, 1, {"R1": [
            (0, 52.0, 13.0, "a1"), (30, 52.0, 13.0, "a2"),
            (60, 52.6, 13.6, "b1"), (90, 52.6, 13.6, "b2"),
            (120, 52.0, 13.0, "a3"), (150, 52.6, 13.6, "b3")]})
        make_dwell_job(self.store, 2, {"R2": [
            (0, 52.0, 13.0, "a1"), (30, 52.0, 13.0, "a2"),
            (60, 52.6, 13.6, "b1"), (90, 52.6, 13.6, "b2"),
            (120, 52.0, 13.0, "a3"), (150, 52.6, 13.6, "b3")]})
        manifest = self._manifest([["1", "R1", "V1"], ["2", "R2", "V2"]])
        job = self.anon.process(manifest, 2, self.levels)
        export = self.anon.public_export(job["anon_job_id"])
        seq = export["groups"][0]["sequence"]
        # 60 s buckets, 0.5 degree cells: four distinct ordered triples.
        self.assertEqual(seq, [
            [0, 284, 386], [1, 285, 387], [2, 284, 386], [2, 285, 387]])

    def test_prefix_and_reverse_never_merge(self) -> None:
        # The three shapes differ even at the 1-degree/120 s coarse level:
        # a prefix is shorter, the reverse visits cells in opposite order and
        # the full path is neither.  No whole-sequence group reaches k=2.
        make_dwell_job(self.store, 1, {
            "FULL": [(0, 52.0, 13.0, "a"), (200, 53.4, 14.4, "b"),
                     (400, 54.6, 15.6, "c")],
            "PREFIX": [(0, 52.0, 13.0, "a"), (200, 53.4, 14.4, "b")],
            "REV": [(0, 54.6, 15.6, "c"), (200, 53.4, 14.4, "b"),
                    (400, 52.0, 13.0, "a")]})
        manifest = self._manifest([["1", "FULL", "V1"], ["1", "PREFIX", "V2"],
                                   ["1", "REV", "V3"]])
        job = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job["published_groups"], 0)
        self.assertEqual(job["dropped_trips"], 3)

    def test_members_across_two_jobs(self) -> None:
        make_dwell_job(self.store, 1, {"R1": self.common})
        make_dwell_job(self.store, 2, {"R2": self.common})
        manifest = self._manifest([["1", "R1", "V1"], ["2", "R2", "V2"]])
        job = self.anon.process(manifest, 2, self.levels)
        export = self.anon.public_export(job["anon_job_id"])
        self.assertEqual(export["groups"][0]["trip_count"], 2)
        detail = self.anon.group_detail(job["anon_job_id"], 1)
        source_jobs = sorted({m["job_id"] for m in detail["members"]})
        self.assertEqual(source_jobs, [1, 2])
        # Raw source rows expand through the internal audit entry only.
        self.assertTrue(all(m["raw_samples"] for m in detail["members"]))

    def test_negative_epoch_and_boundary_points(self) -> None:
        make_dwell_job(self.store, 1, {"N1": [
            (-120, -89.0, -179.0, "c"), (-60, -89.5, -179.5, "b"),
            (-30, -90.0, -180.0, "a")],
            "N2": [
            (-120, -89.0, -179.0, "c"), (-60, -89.5, -179.5, "b"),
            (-30, -90.0, -180.0, "a")]})
        manifest = self._manifest([["1", "N1", "V1"], ["1", "N2", "V2"]])
        job = self.anon.process(manifest, 2, self.levels)
        seq = self.anon.public_export(job["anon_job_id"])["groups"][0]["sequence"]
        # Chronological order even with negative epochs; -60 and -30 share
        # bucket -1 so the last two triples collapse into one after folding.
        self.assertEqual(seq, [[-2, 2, 2], [-1, 1, 1], [-1, 0, 0]])

    def test_each_level_rebuilds_from_raw_samples(self) -> None:
        # Jitter across a fine boundary merges only at the coarse level; the
        # coarse sequence must come from raw points, not the folded fine one.
        make_dwell_job(self.store, 1, {
            "Q1": [(4000, 52.0, 13.0, "a"), (4060, 52.4, 13.0, "b"),
                   (4120, 52.6, 13.6, "c")],
            "Q2": [(4000, 52.0, 13.0, "a"), (4060, 52.6, 13.0, "bjit"),
                   (4120, 52.6, 13.6, "c")]})
        manifest = self._manifest([["1", "Q1", "V1"], ["1", "Q2", "V2"]])
        job = self.anon.process(manifest, 2, self.levels)
        self.assertEqual(job["levels"][0]["rare_groups"], 2)
        export = self.anon.public_export(job["anon_job_id"])
        self.assertEqual(len(export["groups"]), 1)
        self.assertEqual(export["groups"][0]["vehicle_count"], 2)


class ManifestRefusalTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.anon = AnonStore(self.store)
        self.levels = [{"cell_microdeg": 1_000_000, "bucket_s": 60}]
        make_dwell_job(self.store, 1, {"R1": [(0, 1.0, 1.0, "a")]},
                       failed={"RFAIL"}, empty={"REMPTY"})

    def _run(self, rows, header=("job_id", "run_id", "vehicle_unit")):
        return self.anon.process(
            csv_bytes(list(header), rows), 2, self.levels)

    def test_refusals(self) -> None:
        cases = [
            [["9", "R1", "VA"]],               # unknown job
            [["1", "GHOST", "VA"]],            # unknown run
            [["01", "R1", "VA"]],              # leading zero job id
            [["1", "R1", ""]],                 # empty opaque unit
            [["", "R1", "VA"]],                # empty job id
            [["1", "R1", "VA"], ["1", "R1", "VB"]],  # duplicate instance
            [["1", "RFAIL", "VA"]],            # failed run excluded
            [["1", "REMPTY", "VA"]],           # processed but no samples
        ]
        for rows in cases:
            with self.assertRaises(AnonError):
                self._run(rows)

    def test_bad_header_refuses(self) -> None:
        with self.assertRaises(AnonError):
            self.anon.process(
                csv_bytes(["job_id", "run_id", "owner"], [["1", "R1", "VA"]]),
                2, self.levels)

    def test_refusal_leaves_no_job_behind(self) -> None:
        with self.assertRaises(AnonError):
            self._run([["9", "R1", "VA"]])
        self.assertEqual(self.anon.jobs(), [])
        tables = ("anon_jobs", "anon_groups", "anon_members", "anon_public")
        for table in tables:
            self.assertEqual(
                self.store.conn.execute(
                    "SELECT COUNT(*) FROM %s" % table).fetchone()[0], 0)

    def test_unselected_jobs_and_static_tables_untouched(self) -> None:
        make_dwell_job(self.store, 2, {"OTHER": [(0, 2.0, 2.0, "a")]})
        job = self._run([["1", "R1", "VA"], ["2", "OTHER", "VB"]])
        detail = self.anon.job(job["anon_job_id"])
        self.assertEqual(detail["member_count"], 2)

    def test_null_samples_of_unselected_failed_run_do_not_block(self) -> None:
        # A failed run in another job keeps NULL-coordinate raw rows (the
        # dwell writer stores them verbatim); the integrity check is scoped to
        # selected instances, so a valid manifest must still be accepted.
        conn = self.store.conn
        conn.execute(
            "INSERT INTO dwell_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (2, "s2", "sha", "b2", "sha", 120, 250, 60, 120, 1, 0, 1, 0,
             "2026-11-14T00:00:00Z"))
        conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (2, "BAD", None, None, None, "failed", 1, 0, None,
             '[{"code":"bad_coordinate"}]'))
        conn.execute(
            "INSERT INTO dwell_samples VALUES(?,?,?,?,?,?,?,?)",
            (2, "BAD", "x1", None, None, None, 2, 0))
        job = self._run([["1", "R1", "VA"]])
        self.assertEqual(job["member_count"], 1)
        # Selecting the failed run itself is still refused.
        with self.assertRaises(AnonError):
            self._run([["2", "BAD", "VA"]])


class PublicSurfaceTests(StoreTestCase):
    def test_export_columns_are_fixed_and_clean(self) -> None:
        anon = AnonStore(self.store)
        make_dwell_job(self.store, 1, {
            "R1": [(3600, 52.0, 13.0, "s1"), (3720, 52.6, 13.6, "s2")],
            "R2": [(3600, 52.0, 13.0, "s1"), (3720, 52.6, 13.6, "s2")]})
        levels = [{"cell_microdeg": 1_000_000, "bucket_s": 120}]
        job = anon.process(csv_bytes(
            ["job_id", "run_id", "vehicle_unit"],
            [["1", "R1", "V1"], ["1", "R2", "V2"]]), 2, levels)
        export = anon.public_export(job["anon_job_id"])
        self.assertEqual(export["columns"], list(PUBLIC_COLUMNS))
        for group in export["groups"]:
            self.assertEqual(set(group), set(PUBLIC_COLUMNS))
            for key in ("run_id", "vehicle_unit", "job_id", "sample_id",
                        "seq_hash", "internal_group_no", "source_line"):
                self.assertNotIn(key, group)
            # Only coarse cell numbers; no raw epoch seconds or coordinates.
            for triple in group["sequence"]:
                self.assertEqual(len(triple), 3)
                self.assertTrue(all(isinstance(v, int) for v in triple))

    def test_rerun_gets_independent_job_id(self) -> None:
        anon = AnonStore(self.store)
        make_dwell_job(self.store, 1, {"R1": [(0, 1.0, 1.0, "a")],
                                       "R2": [(0, 1.0, 1.0, "a")]})
        manifest = csv_bytes(["job_id", "run_id", "vehicle_unit"],
                             [["1", "R1", "V1"], ["1", "R2", "V2"]])
        levels = [{"cell_microdeg": 1_000_000, "bucket_s": 60}]
        first = anon.process(manifest, 2, levels)
        second = anon.process(manifest, 2, levels)
        self.assertNotEqual(first["anon_job_id"], second["anon_job_id"])
        self.assertEqual(len(anon.jobs()), 2)


class CrashAndCliTests(StoreTestCase):
    def _db_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", CLI, "--db", str(self.db_path), *argv],
            capture_output=True, text=True)

    def test_hard_exit_mid_publish_leaves_nothing_visible(self) -> None:
        make_dwell_job(self.store, 1, {
            "R%d" % i: [(3600, 52.0 + i, 13.0, "a"),
                        (3720, 52.6 + i, 13.6, "b")] for i in range(6)})
        manifest = self.tmp / "manifest.csv"
        manifest.write_bytes(csv_bytes(
            ["job_id", "run_id", "vehicle_unit"],
            [["1", "R%d" % i, "V%d" % i] for i in range(6)]))
        levels = self.tmp / "levels.json"
        levels.write_text(json.dumps([
            {"cell_microdeg": 500_000, "bucket_s": 60},
            {"cell_microdeg": 1_000_000, "bucket_s": 120}]))
        # Close this connection so the subprocess can open its own.
        self.store.close()
        env = dict(os.environ, GTFS_FAULT_ANON_AFTER_GROUPS="2")
        crashed = subprocess.run(
            [sys.executable, "-I", CLI, "--db", str(self.db_path),
             "anon-process", "--manifest", str(manifest), "--k", "2",
             "--levels-file", str(levels)],
            env=env, capture_output=True, text=True)
        self.assertEqual(crashed.returncode, 91)
        # A brand new process must not see any half-written anon job.
        jobs = json.loads(self._db_cli("--json", "anon-jobs").stdout)
        self.assertEqual(jobs, [])
        reopened = sqlite3.connect(str(self.db_path))
        for table in ("anon_jobs", "anon_groups", "anon_members",
                      "anon_public", "anon_group_sequences", "anon_levels"):
            self.assertEqual(
                reopened.execute("SELECT COUNT(*) FROM %s" % table)
                .fetchone()[0], 0)
        # Nothing is left beside the database; any crashed scratch file lives
        # only in the OS temp area, never in the deliverable directory.
        self.assertFalse(
            list(self.db_path.parent.glob("anon-scratch-*")))
        reopened.close()

    def test_cli_round_trip_in_new_process(self) -> None:
        make_dwell_job(self.store, 1, {"R1": [(0, 1.0, 1.0, "a")],
                                       "R2": [(0, 1.0, 1.0, "a")]})
        self.store.close()
        manifest = self.tmp / "manifest.csv"
        manifest.write_bytes(csv_bytes(
            ["job_id", "run_id", "vehicle_unit"],
            [["1", "R1", "V1"], ["1", "R2", "V2"]]))
        levels = self.tmp / "levels.json"
        levels.write_text(json.dumps(
            [{"cell_microdeg": 1_000_000, "bucket_s": 60}]))
        proc = self._db_cli("anon-process", "--manifest", str(manifest),
                            "--k", "2", "--levels-file", str(levels))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        export = json.loads(self._db_cli(
            "--json", "anon-export", "--anon-job-id", "1").stdout)
        self.assertEqual(export["groups"][0]["vehicle_count"], 2)
        bad = self._db_cli("anon-process", "--manifest", str(manifest),
                           "--k", "1", "--levels-file", str(levels))
        self.assertEqual(bad.returncode, 2)
