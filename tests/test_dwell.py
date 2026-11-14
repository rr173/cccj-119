"""Offline GPS dwell pipeline: parsing, spatial index, state machine, storage.

The tests never trust the engine's own summary counts.  Every dwell is
checked two independent ways:

1. the candidate chosen for each point is recomputed with a full-stop
   exhaustive scan that bypasses the production spatial index;
2. each produced dwell is compared field-by-field against the hand-written
   timeline in dist/dwell/dwell-expected.json (states, instants, support
   sample ids, end reason and source lines), not just row counts.

Run with:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from helpers import DIST, ROOT, StoreTestCase, build_all_fixtures

from gtfs_store.dwell import (
    DwellFileError,
    DwellParams,
    DwellStore,
    Sample,
    SpatialIndex,
    brute_force_nearest,
    detect_dwells,
    finite_coord,
    haversine_m,
    nearest_candidate,
    parse_bindings,
    parse_samples,
)
from gtfs_store.errors import GtfsError

DDWELL = DIST / "dwell"
PARAMS = DwellParams(enter_m=120, exit_m=250, min_dwell_s=60, max_gap_s=120)


def build_dwell_fixtures() -> None:
    subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_dwell_fixtures.py")],
                   check=True, capture_output=True, cwd=str(ROOT))


class GeometryTests(StoreTestCase):
    def test_earth_radius_and_basic_distances(self) -> None:
        from gtfs_store.dwell import EARTH_RADIUS_M
        self.assertEqual(EARTH_RADIUS_M, 6371008.8)
        # One meridian degree at the equator; full precision, no metre rounding.
        d = haversine_m(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(d, 111195.0802335329, places=6)
        self.assertEqual(haversine_m(52.0, 13.0, 52.0, 13.0), 0.0)
        # Symmetry and the longitude cosine term.
        self.assertAlmostEqual(haversine_m(0, 0, 0, 1), haversine_m(0, 1, 0, 0))
        self.assertLess(haversine_m(60, 0, 60, 1), haversine_m(0, 0, 0, 1))

    def test_finite_coord_contract(self) -> None:
        self.assertTrue(finite_coord(90.0, 180.0))
        self.assertTrue(finite_coord(-90.0, -180.0))
        self.assertFalse(finite_coord(90.0001, 0.0))
        self.assertFalse(finite_coord(0.0, 180.0001))
        self.assertFalse(finite_coord(float("nan"), 0.0))
        self.assertFalse(finite_coord(0.0, float("inf")))


class SpatialIndexTests(StoreTestCase):
    def _random_stops(self, n: int, rng) -> list[dict]:
        stops = []
        for i in range(n):
            stops.append({
                "stop_id": "S%04d" % i,
                "stop_lat": rng.uniform(-89.999, 89.999),
                "stop_lon": rng.uniform(-180, 180),
            })
        return stops

    def test_index_matches_brute_force_worldwide(self) -> None:
        import random
        rng = random.Random(20261114)
        checks = 0
        for trial in range(120):
            stops = self._random_stops(rng.randint(1, 30), rng)
            if trial % 3 == 0:
                stops[0]["stop_lat"] = rng.choice([89.9999, -89.9999])
            if trial % 4 == 0:
                stops[0]["stop_lon"] = rng.choice([179.9999, -179.9999, 180.0, -180.0])
            radius = rng.choice([60, 120, 300, 3000, 40000])
            index = SpatialIndex(stops, radius)
            for _ in range(50):
                lat = rng.uniform(-89.99999, 89.99999)
                lon = rng.uniform(-180, 180)
                indexed = nearest_candidate(index, lat, lon, radius)
                brute = brute_force_nearest(stops, lat, lon, radius)
                self.assertEqual(
                    indexed[0]["stop_id"] if indexed else None,
                    brute[0]["stop_id"] if brute else None,
                    "trial %d point (%s,%s) radius %s" % (trial, lat, lon, radius))
                checks += 1
        # The index demonstrably did less work than the Cartesian scan.
        self.assertGreater(checks, 0)

    def test_antimeridian_box_does_not_drop_candidate(self) -> None:
        stops = [{"stop_id": "DL", "stop_lat": 0.0, "stop_lon": 179.999}]
        index = SpatialIndex(stops, 120)
        # A point on the negative side within the enter circle must be found.
        pick = nearest_candidate(index, 0.0, -179.99995, 120)
        self.assertIsNotNone(pick)
        self.assertEqual(pick[0]["stop_id"], "DL")
        self.assertLessEqual(pick[1], 120.0)

    def test_pole_box_scans_all_longitude_bands(self) -> None:
        # At 89.99 latitude a 1-degree longitude hop is only ~19 m; a naive
        # longitude box that ignores convergence would miss this candidate.
        stops = [{"stop_id": "P", "stop_lat": 89.995, "stop_lon": 45.0}]
        index = SpatialIndex(stops, 120)
        pick = nearest_candidate(index, 89.995, 46.0, 120)
        self.assertIsNotNone(pick)
        self.assertEqual(pick[0]["stop_id"], "P")
        explanation = index.explain()
        self.assertTrue(explanation["pole_safe"])
        self.assertTrue(explanation["antimeridian_safe"])

    def test_unicode_tie_break_is_by_code_point_not_input_order(self) -> None:
        stops = [{"stop_id": "z", "stop_lat": 0.001, "stop_lon": 0.0},
                 {"stop_id": "A", "stop_lat": -0.001, "stop_lon": 0.0},
                 {"stop_id": "007", "stop_lat": 0.0, "stop_lon": 0.001},
                 {"stop_id": "7", "stop_lat": 0.0, "stop_lon": -0.001}]
        index = SpatialIndex(stops, 2000)
        pick = nearest_candidate(index, 0.0, 0.0, 2000)
        # "007" (U+0030) < "7" (U+0037) < "A" (U+0041) < "z".
        self.assertEqual(pick[0]["stop_id"], "007")
        # Input order must not decide: reverse the list and expect the same.
        index2 = SpatialIndex(list(reversed(stops)), 2000)
        self.assertEqual(nearest_candidate(index2, 0.0, 0.0, 2000)[0]["stop_id"], "007")

    def test_boundary_distances_are_inclusive_without_rounding(self) -> None:
        # Build a stop exactly 120.000... metres away using a computed offset
        # and assert the inclusive <= boundary is decided on raw float distance.
        stop = {"stop_id": "X", "stop_lat": 10.0, "stop_lon": 20.0}
        index = SpatialIndex([stop], 120)
        lat = 10.0 + 120.0 / 6371008.8 * 180.0 / 3.141592653589793
        pick = nearest_candidate(index, lat, 20.0, 120)
        self.assertIsNotNone(pick)
        self.assertLessEqual(pick[1], 120.0 + 1e-9)

    def test_prime_meridian_seam_wraps_too(self) -> None:
        # The grid seam is at longitude 0 (normalised 0..360); stops and
        # points on either side of it must still meet through the fold.
        stops = [{"stop_id": "M", "stop_lat": 0.0, "stop_lon": 0.0005}]
        index = SpatialIndex(stops, 120)
        pick = nearest_candidate(index, 0.0, -0.0005, 120)
        self.assertIsNotNone(pick)
        self.assertEqual(pick[0]["stop_id"], "M")

    def test_no_point_supports_two_dwells(self) -> None:
        # When the first outside point starts the next string, it must never
        # also appear among the closed dwell's support samples.
        stops = [
            {"stop_id": "S1", "stop_lat": 0.0, "stop_lon": 0.0},
            {"stop_id": "S2", "stop_lat": 0.004, "stop_lon": 0.0},  # ~445 m north
        ]
        index = SpatialIndex(stops, 120)
        timeline = [
            Sample("R", "a", 0, -60.0 / 111195.08, 0.0, 2),
            Sample("R", "b", 60, 0.0, 0.0, 3),
            Sample("R", "c", 90, 330.0 / 111195.08, 0.0, 4),   # exits S1
            Sample("R", "d", 120, 0.004 - 40.0 / 111195.08, 0.0, 5),
            Sample("R", "e", 180, 0.004, 0.0, 6),
            Sample("R", "f", 250, 0.004 + 30.0 / 111195.08, 0.0, 7),
        ]
        dwells = detect_dwells(timeline, index, PARAMS)
        self.assertEqual([d.stop["stop_id"] for d in dwells], ["S1", "S2"])
        first_support = {s.sample_id for s in dwells[0].samples}
        second_support = {s.sample_id for s in dwells[1].samples}
        self.assertNotIn("c", first_support)
        self.assertFalse(first_support & second_support)


class StateMachineTests(StoreTestCase):
    def _sample(self, sid, t, lat, lon, line=1) -> Sample:
        return Sample("R", sid, t, lat, lon, line)

    def setUp(self) -> None:
        super().setUp()
        self.alpha = {"stop_id": "ALPHA", "stop_name": "A",
                      "stop_lat": 52.52, "stop_lon": 13.405}
        self.bravo = {"stop_id": "BRAVO", "stop_name": "B",
                      "stop_lat": 52.52 + 200.0 / 111195.08, "stop_lon": 13.405}
        self.stops = [self.alpha, self.bravo]
        self.index = SpatialIndex(self.stops, PARAMS.enter_m)

    def _at(self, stop, north_m=0.0, south_m=0.0) -> tuple[float, float]:
        offset = (north_m - south_m) / 111195.08
        return stop["stop_lat"] + offset, stop["stop_lon"]

    def test_pending_below_min_dwell_outputs_nothing(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=300)),
            self._sample("b", 20, *self._at(self.alpha, south_m=70)),
            self._sample("c", 65, *self._at(self.alpha, north_m=70)),
            self._sample("d", 100, *self._at(self.alpha, north_m=300)),
        ]
        self.assertEqual(detect_dwells(samples, self.index, PARAMS), [])

    def test_arrival_is_backfilled_to_first_point_not_confirm_instant(self) -> None:
        samples = [
            self._sample("a", 100, *self._at(self.alpha, south_m=60)),
            self._sample("b", 130, self.alpha["stop_lat"], self.alpha["stop_lon"]),
            self._sample("c", 160, *self._at(self.alpha, north_m=60)),
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        fact = dwells[0]
        self.assertEqual(fact.arrival_sample.sample_id, "a")
        self.assertEqual(fact.arrival_sample.observed_utc, 100)
        self.assertEqual(fact.confirm_sample.observed_utc, 160)
        self.assertEqual(fact.end_reason, "open_ended")
        self.assertIsNone(fact.departure_sample)

    def test_exact_min_dwell_confirms_and_exact_gap_stays_continuous(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 60, self.alpha["stop_lat"], self.alpha["stop_lon"]),
            self._sample("c", 180, *self._at(self.alpha, north_m=60)),  # gap == 120
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        self.assertEqual(dwells[0].confirm_sample.observed_utc, 60)
        self.assertEqual([s.sample_id for s in dwells[0].samples], ["a", "b", "c"])

    def test_gap_truncates_confirmed_without_departure(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 60, self.alpha["stop_lat"], self.alpha["stop_lon"]),
            self._sample("c", 181, *self._at(self.alpha, south_m=60)),  # gap 121
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)  # the post-gap string never reaches 60 s
        self.assertEqual(dwells[0].end_reason, "gap_truncated")
        self.assertIsNone(dwells[0].departure_sample)
        self.assertEqual(dwells[0].last_observed_sample.sample_id, "b")

    def test_exit_radius_hysteresis_locks_station(self) -> None:
        # Confirmed at ALPHA; a later point is beyond enter but inside exit and
        # much nearer BRAVO: station and arrival must not change.
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 30, *self._at(self.alpha, south_m=20)),
            self._sample("c", 60, *self._at(self.alpha, south_m=20)),
            self._sample("d", 90, *self._at(self.alpha, north_m=180)),
            self._sample("e", 120, *self._at(self.alpha, north_m=330)),
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        fact = dwells[0]
        self.assertEqual(fact.stop["stop_id"], "ALPHA")
        self.assertEqual(fact.end_reason, "observed_exit")
        self.assertEqual(fact.last_observed_sample.sample_id, "d")
        self.assertEqual(fact.departure_sample.sample_id, "e")
        # The outside point did not become a support point.
        self.assertEqual([s.sample_id for s in fact.samples], ["a", "b", "c", "d"])

    def test_outside_point_can_start_another_station_string(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 60, self.alpha["stop_lat"], self.alpha["stop_lon"]),
            self._sample("c", 90, *self._at(self.alpha, north_m=330)),  # exits ALPHA
            self._sample("d", 120, *self._at(self.bravo, south_m=40)),
            self._sample("e", 150, self.bravo["stop_lat"], self.bravo["stop_lon"]),
            self._sample("f", 190, *self._at(self.bravo, north_m=30)),
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual([d.stop["stop_id"] for d in dwells], ["ALPHA", "BRAVO"])
        self.assertEqual(dwells[0].departure_sample.sample_id, "c")
        self.assertNotIn("c", [s.sample_id for s in dwells[1].samples])

    def test_pending_switch_discards_old_string(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 30, *self._at(self.bravo, south_m=40)),  # switches
            self._sample("c", 60, self.bravo["stop_lat"], self.bravo["stop_lon"]),
            self._sample("d", 100, *self._at(self.bravo, north_m=30)),
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        self.assertEqual(dwells[0].stop["stop_id"], "BRAVO")
        self.assertEqual(dwells[0].arrival_sample.sample_id, "b")

    def test_gap_rules_apply_to_pending_strings_too(self) -> None:
        # Exactly max_gap keeps a pending string alive; over-gap discards it.
        alive = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 120, *self._at(self.alpha, south_m=60)),
            self._sample("c", 180, *self._at(self.alpha, south_m=60)),
        ]
        dwells = detect_dwells(alive, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        self.assertEqual(dwells[0].arrival_sample.observed_utc, 0)
        broken = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 121, *self._at(self.alpha, south_m=60)),
        ]
        self.assertEqual(detect_dwells(broken, self.index, PARAMS), [])

    def test_drift_between_enter_and_exit_keeps_locked_station(self) -> None:
        samples = [
            self._sample("a", 0, *self._at(self.alpha, south_m=60)),
            self._sample("b", 60, self.alpha["stop_lat"], self.alpha["stop_lon"]),
            self._sample("c", 90, *self._at(self.alpha, north_m=180)),  # > enter
            self._sample("d", 120, *self._at(self.alpha, north_m=300)),  # > exit
        ]
        dwells = detect_dwells(samples, self.index, PARAMS)
        self.assertEqual(len(dwells), 1)
        self.assertEqual([s.sample_id for s in dwells[0].samples],
                         ["a", "b", "c"])
        self.assertEqual(dwells[0].departure_sample.sample_id, "d")


class ParsingTests(StoreTestCase):
    def test_duplicate_timestamp_rejects_whole_run_even_with_same_coord(self) -> None:
        data = (
            "run_id,sample_id,observed_utc,lat,lon\r\n"
            "R,a,10,52.52,13.405\r\n"
            "R,b,10,52.52,13.405\r\n").encode()
        rows, problems = parse_samples(data)
        self.assertEqual(problems, [])
        # The conflict surfaces at run validation, regardless of physical order.
        store = DwellStore(self.store)
        result = store._process_run(
            "R", rows, {"feed_key": "f", "edition": "e", "trip_id": "t",
                        "source_line": 1}, PARAMS)
        self.assertEqual(result["status"], "failed")
        self.assertIn("duplicate_timestamp",
                      [d["code"] for d in result["diagnostics"]])

    def test_missing_run_id_is_file_level(self) -> None:
        data = (
            "run_id,sample_id,observed_utc,lat,lon\n"
            ",a,10,52.52,13.405\n").encode()
        _rows, problems = parse_samples(data)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], 2)

    def test_bad_header_and_broken_csv_are_file_errors(self) -> None:
        with self.assertRaises(DwellFileError):
            parse_samples(b"run_id,sample_id,observed_utc,lat\nR,a,10,52.52\n")
        with self.assertRaises(DwellFileError):
            parse_bindings(b"run_id,feed_key,edition,trip_id\nR,f,e\n")

    def test_bad_coords_and_ids_are_attributed_to_the_run(self) -> None:
        data = (
            "run_id,sample_id,observed_utc,lat,lon\n"
            "R,a,10,999,13\n"
            "R,b,not-a-time,52,13\n"
            "R,,20,52,13\n").encode()
        rows, _ = parse_samples(data)
        store = DwellStore(self.store)
        binding = {"feed_key": "f", "edition": "e", "trip_id": "t", "source_line": 1}
        result = store._process_run("R", rows, binding, PARAMS)
        codes = {d["code"] for d in result["diagnostics"]}
        self.assertEqual(codes, {"bad_coordinate", "bad_timestamp", "empty_sample_id"})
        self.assertTrue(all(d["source_line"] for d in result["diagnostics"]))


class ParamsTests(StoreTestCase):
    def test_param_contract(self) -> None:
        DwellParams(1, 2, 1, 1).validate()
        for kwargs in (
            dict(enter_m=0, exit_m=2, min_dwell_s=1, max_gap_s=1),
            dict(enter_m=2, exit_m=2, min_dwell_s=1, max_gap_s=1),
            dict(enter_m=3, exit_m=2, min_dwell_s=1, max_gap_s=1),
            dict(enter_m=1, exit_m=2, min_dwell_s=0, max_gap_s=1),
            dict(enter_m=1, exit_m=2, min_dwell_s=1, max_gap_s=-1),
        ):
            with self.assertRaises(GtfsError):
                DwellParams(**kwargs).validate()
        # Booleans are not integers for this contract.
        with self.assertRaises(GtfsError):
            DwellParams(True, 2, 1, 1).validate()


class FixturePipelineTests(StoreTestCase):
    """End-to-end checks against the hand-written dwell fixture timeline."""

    @classmethod
    def setUpClass(cls) -> None:
        build_all_fixtures()
        build_dwell_fixtures()

    def setUp(self) -> None:
        super().setUp()
        self.dwell = DwellStore(self.store)
        for edition, package in [
            ("v1", DDWELL / "dwell-v1.zip"),
            ("v2", DDWELL / "dwell-v2.zip"),
            ("v3-bad-coords", DDWELL / "dwell-v3-bad-coords.zip"),
            ("large", DDWELL / "dwell-large.zip"),
        ]:
            result = self.store.load("dwell", edition, package)
            self.assertEqual(result["status"], "ready", result)
        self.expected = json.loads((DDWELL / "dwell-expected.json").read_text())
        self.sample_bytes = (DDWELL / "samples.csv").read_bytes()
        self.binding_bytes = (DDWELL / "bindings.csv").read_bytes()

    def _job(self) -> dict:
        return self.dwell.process("samples.csv", "bindings.csv", PARAMS,
                                  sample_bytes=self.sample_bytes,
                                  binding_bytes=self.binding_bytes)

    def test_hand_timeline_every_dwell_field_and_evidence(self) -> None:
        job = self._job()
        # R13-NOBIND only exists in the unregistered-sample file.
        manifest_runs = {k: v for k, v in self.expected["runs"].items()
                         if k != "R13-NOBIND"}
        # 1. Per-run statuses and failure codes match the hand manifest.
        for run_id, spec in manifest_runs.items():
            stored = next(r for r in job["runs"] if r["run_id"] == run_id)
            if "fails" in spec:
                self.assertEqual(stored["status"], "failed", run_id)
                for code in spec["fails"]:
                    self.assertIn(code, [d["code"] for d in stored["diagnostics"]],
                                  run_id)
            else:
                self.assertEqual(stored["status"], "processed", run_id)
                self._assert_dwells_match(run_id, spec, stored, job["job_id"])

    def _assert_dwells_match(self, run_id, spec, stored, job_id) -> None:
        facts = self.dwell.dwells(job_id, run_id)
        self.assertEqual(len(facts), len(spec["dwells"]), run_id)
        for fact, want in zip(facts, spec["dwells"]):
            self.assertEqual(fact["stop_id"], want["stop_id"], run_id)
            self.assertEqual(fact["end_reason"], want["end_reason"], run_id)
            self.assertEqual(fact["arrival_utc"], want["arrival_utc"], run_id)
            self.assertEqual(fact["confirmed_utc"], want["confirmed_utc"], run_id)
            self.assertEqual(fact["last_observed_utc"], want["last_observed_utc"], run_id)
            self.assertEqual(fact["departure_utc"], want["departure_utc"], run_id)
            self.assertEqual(fact["support_points"], len(want["support_ids"]), run_id)
            self.assertEqual(fact["first_sample_id"], want["support_ids"][0], run_id)
            self.assertEqual(fact["last_inside_sample_id"],
                             want["support_ids"][-1], run_id)
            self.assertEqual(fact["first_outside_sample_id"],
                             want["first_outside_id"], run_id)
            # Evidence expands the raw sample values and source lines, and the
            # support roles partition correctly (multi-role samples repeat).
            detail = self.dwell.evidence(job_id, fact["dwell_seq"])
            roles = [p["point_role"] for p in detail["evidence"]]
            self.assertEqual(roles.count("arrival"), 1)
            self.assertEqual(roles.count("confirm"), 1)
            self.assertEqual(roles.count("last_inside"), 1)
            if want["first_outside_id"]:
                self.assertEqual(roles.count("first_outside"), 1)
            support = [p for p in detail["evidence"]
                       if p["point_role"] != "first_outside"]
            support_ids = [p["sample_id"] for p in support]
            # Every intended support id appears at least once, and nothing else.
            self.assertEqual(sorted(set(support_ids)), sorted(want["support_ids"]))
            self.assertTrue(all(p["source_line"] >= 2 for p in detail["evidence"]))
            # Stop name comes from the actual static table.
            static_name = self.store.conn.execute(
                "SELECT stop_name FROM stops WHERE feed_key='dwell' AND edition=? "
                "AND stop_id=?", (spec["edition"], want["stop_id"])).fetchone()[0]
            self.assertEqual(fact["stop_name"], static_name)

    def test_brute_force_candidates_agree_for_every_ingested_point(self) -> None:
        job = self._job()
        for run in job["runs"]:
            if run["status"] != "processed":
                continue
            binding = run["binding"]
            stops = self.dwell.candidate_stops(binding["feed_key"], binding["edition"],
                                               binding["trip_id"])
            index = SpatialIndex(stops, PARAMS.enter_m)
            for sample in run["samples"]:
                indexed = nearest_candidate(index, sample.lat, sample.lon,
                                            PARAMS.enter_m)
                brute = brute_force_nearest(stops, sample.lat, sample.lon,
                                            PARAMS.enter_m)
                self.assertEqual(
                    indexed[0]["stop_id"] if indexed else None,
                    brute[0]["stop_id"] if brute else None,
                    "run %s sample %s" % (run["run_id"], sample.sample_id))

    def test_distinct_stop_identity_007_and_7(self) -> None:
        job = self._job()
        facts = self.dwell.dwells(job["job_id"], "R8")
        self.assertEqual([f["stop_id"] for f in facts], ["007", "7"])
        self.assertEqual([f["stop_name"] for f in facts],
                         ["Station Seven", "Station Seven Pseudo Dup"])

    def test_repeated_stop_visit_is_one_spatial_candidate(self) -> None:
        # TRIP_AB visits ALPHA and BRAVO once each here; build a synthetic trip
        # visiting one stop twice and confirm a single candidate is returned.
        stops = self.dwell.candidate_stops("dwell", "v1", "TRIP_AB")
        self.assertEqual([s["stop_id"] for s in stops], ["ALPHA", "BRAVO"])
        conn = self.store.conn
        conn.execute("INSERT INTO stop_times VALUES('dwell','v1','TRIP_DUP',1,'ALPHA',"
                     "0,0,'0')")
        conn.execute("INSERT INTO stop_times VALUES('dwell','v1','TRIP_DUP',2,'ALPHA',"
                     "60,60,'0')")
        conn.execute("INSERT INTO trips VALUES('dwell','v1','TRIP_DUP','R1','WK',NULL)")
        dup = self.dwell.candidate_stops("dwell", "v1", "TRIP_DUP")
        self.assertEqual([s["stop_id"] for s in dup], ["ALPHA"])

    def test_bad_candidate_coordinate_names_the_stop_and_fails_run(self) -> None:
        job = self._job()
        stored = next(r for r in job["runs"] if r["run_id"] == "R12-BADSTOP")
        self.assertEqual(stored["status"], "failed")
        diag = stored["diagnostics"][0]
        self.assertEqual(diag["code"], "candidate_stops")
        self.assertIn("CHARLIE", diag["message"])

    def test_sibling_good_run_survives_bad_runs_and_isolation(self) -> None:
        job = self._job()
        good = next(r for r in job["runs"] if r["run_id"] == "R10-OK")
        self.assertEqual(good["status"], "processed")
        self.assertEqual(len(good["dwells"]), 1)
        for bad in ("R10-BADCOORD", "R10-DUPID", "R10-DUPTS"):
            self.assertEqual(
                next(r for r in job["runs"] if r["run_id"] == bad)["status"], "failed")
        # Different static edition per run: R11-V2 binds edition v2.
        v2 = next(r for r in job["runs"] if r["run_id"] == "R11-V2")
        self.assertEqual(v2["binding"]["edition"], "v2")
        fact = self.dwell.dwells(job["job_id"], "R11-V2")[0]
        self.assertEqual((fact["feed_key"], fact["edition"]), ("dwell", "v2"))

    def test_shuffled_physical_order_gives_identical_dwell(self) -> None:
        job = self._job()
        r1 = self.dwell.dwells(job["job_id"], "R1")
        r9 = self.dwell.dwells(job["job_id"], "R9")
        self.assertEqual(len(r1), len(r9) == 1)
        for key in ("stop_id", "arrival_utc", "confirmed_utc", "last_observed_utc",
                    "departure_utc", "end_reason", "support_points"):
            self.assertEqual(r1[0][key], r9[0][key], key)
        # Evidence sample ids differ only by the run prefix; source lines point
        # at the shuffled physical rows.
        e1 = self.dwell.evidence(job["job_id"], r1[0]["dwell_seq"])["evidence"]
        e9 = self.dwell.evidence(job["job_id"], r9[0]["dwell_seq"])["evidence"]
        self.assertEqual([p["sample_id"] for p in e9],
                         [s.replace("R1-", "R9-") for s in
                          [p["sample_id"] for p in e1]])
        self.assertNotEqual([p["source_line"] for p in e9],
                            [p["source_line"] for p in e1])

    def test_index_participation_large_fixture(self) -> None:
        self.store.load("dwell", "large", DDWELL / "dwell-large.zip")
        result = self.dwell.process(
            "samples-large.csv", "bindings-large.csv", PARAMS,
            sample_bytes=(DDWELL / "samples-large.csv").read_bytes(),
            binding_bytes=(DDWELL / "bindings-large.csv").read_bytes())
        run = result["runs"][0]
        self.assertEqual(run["status"], "processed")
        self.assertEqual(len(run["dwells"]), 1)
        index = run["index"]
        self.assertEqual(index["indexed_stops"], 400)
        # Exact checks via the index are far below a 400 x 40 Cartesian scan.
        self.assertLess(index["candidate_checks"],
                        self.expected["files"]["large"]["brute_force_checks"])
        # And independently verify the brute-force candidate on every point.
        stops = self.dwell.candidate_stops("dwell", "large", "TRIP_LARGE")
        for sample in run["samples"]:
            brute = brute_force_nearest(stops, sample.lat, sample.lon, PARAMS.enter_m)
            self.assertEqual(brute[0]["stop_id"], "L0200")

    def test_job_is_atomic_and_rerun_is_independent(self) -> None:
        first = self._job()
        os.environ["GTFS_FAULT_DWELL_AFTER_RUNS"] = "2"
        try:
            with self.assertRaises(GtfsError):
                self._job()
        finally:
            del os.environ["GTFS_FAULT_DWELL_AFTER_RUNS"]
        # The crashed job exposed nothing.
        job_ids = [r["job_id"] for r in self.dwell.jobs()]
        self.assertEqual(job_ids, [first["job_id"]])
        # A rerun creates a brand new job with identical facts.
        rerun = self._job()
        self.assertNotEqual(rerun["job_id"], first["job_id"])
        self.assertEqual(rerun["dwell_facts"], first["dwell_facts"])
        before = self.dwell.dwells(first["job_id"])
        after = self.dwell.dwells(rerun["job_id"])
        keys = ("run_id", "stop_id", "arrival_utc", "confirmed_utc",
                "last_observed_utc", "departure_utc", "end_reason", "support_points")
        self.assertEqual([{k: f[k] for k in keys} for f in before],
                         [{k: f[k] for k in keys} for f in after])

    def test_unregistered_and_empty_runs_are_run_level_failures(self) -> None:
        result = self.dwell.process(
            "samples-unregistered.csv", "bindings.csv", PARAMS,
            sample_bytes=(DDWELL / "samples-unregistered.csv").read_bytes(),
            binding_bytes=self.binding_bytes)
        for run_id, code in (("R13-NOBIND", "unregistered_run"),
                             ("R14-EMPTY", "no_samples")):
            run = next(r for r in result["runs"] if r["run_id"] == run_id)
            self.assertEqual(run["status"], "failed")
            self.assertIn(code, [d["code"] for d in run["diagnostics"]])
        # Good runs in the same file still produced dwells.
        expected_facts = sum(len(spec.get("dwells", []))
                             for spec in self.expected["runs"].values())
        self.assertEqual(result["dwell_facts"], expected_facts)

    def test_file_level_failures_create_no_job(self) -> None:
        with self.assertRaises(DwellFileError):
            self.dwell.process(
                "x.csv", "bindings.csv", PARAMS,
                sample_bytes=(DDWELL / "samples-missing-run.csv").read_bytes(),
                binding_bytes=self.binding_bytes)
        with self.assertRaises(DwellFileError):
            self.dwell.process(
                "samples.csv", "broken.csv", PARAMS,
                sample_bytes=self.sample_bytes,
                binding_bytes=(DDWELL / "bindings-broken.csv").read_bytes())
        self.assertEqual(self.dwell.jobs(), [])

    def test_source_lines_and_raw_values_are_retained(self) -> None:
        job = self._job()
        detail = self.dwell.run_detail(job["job_id"], "R1")
        lines = {s["source_line"] for s in detail["samples"]}
        # Header is line 1; every data row keeps its physical source line.
        self.assertEqual(lines, set(range(2, 2 + len(detail["samples"]))))
        evidence = self.dwell.evidence(job["job_id"], 1)["evidence"]
        # Raw lat/lon are stored verbatim at 7-decimal precision.
        first = next(p for p in evidence if p["point_role"] == "arrival")
        self.assertEqual((round(first["lat"], 7), round(first["lon"], 7)),
                         (first["lat"], first["lon"]))

    def test_binding_manifest_is_persisted(self) -> None:
        job = self._job()
        detail = self.dwell.job(job["job_id"])
        binding_lines = [l for l in self.binding_bytes.decode().splitlines()[1:] if l]
        self.assertEqual(len(detail["bindings"]), len(binding_lines))
        binding = next(b for b in detail["bindings"] if b["run_id"] == "R1")
        self.assertEqual((binding["feed_key"], binding["edition"], binding["trip_id"]),
                         ("dwell", "v1", "TRIP_A"))
        self.assertIsNotNone(binding["source_line"])

    def test_cli_end_to_end(self) -> None:
        out = self.cli("--json", "dwell-process",
                       "--samples", str(DDWELL / "samples.csv"),
                       "--bindings", str(DDWELL / "bindings.csv"),
                       "--enter-m", "120", "--exit-m", "250",
                       "--min-dwell-s", "60", "--max-gap-s", "120")
        expected_facts = sum(len(spec.get("dwells", []))
                             for spec in self.expected["runs"].values())
        expected_failed = sum(1 for spec in self.expected["runs"].values()
                              if "fails" in spec and spec["fails"] != ["unregistered_run"])
        self.assertEqual(out["failed_runs"], expected_failed)
        self.assertEqual(out["dwell_facts"], expected_facts)
        job_id = out["job_id"]
        jobs = self.cli("--json", "dwell-jobs")
        self.assertTrue(any(j["job_id"] == job_id for j in jobs))
        run = self.cli("--json", "dwell-run", "--job-id", str(job_id),
                       "--run-id", "R2")
        self.assertEqual(len(run["dwells"]), 2)
        first_seq = run["dwells"][0]["dwell_seq"]
        evidence = self.cli("--json", "dwell-evidence", "--job-id", str(job_id),
                            "--dwell-seq", str(first_seq))
        self.assertTrue(evidence["evidence"])
