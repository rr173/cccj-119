"""Build synthetic fixtures for the offline GPS dwell pipeline.

Everything is generated, nothing is read back from existing fixture output.
Two kinds of artefacts land in dist/dwell/:

* static GTFS packages (dwell-v1.zip, dwell-v2.zip, dwell-v3-bad-coords.zip),
  loaded through the ordinary, unchanged static intake;
* sample CSVs + binding CSVs whose timelines were designed by hand below, with
  a side-car manifest (dwell-expected.json) recording every intended dwell,
  its state transitions and its evidence sample ids.

Run from the repository root:  python3 -I tools/build_dwell_fixtures.py
"""

from __future__ import annotations

import csv
import io
import json
import math
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "fixtures"
DIST = ROOT / "dist" / "dwell"

from gtfs_store.dwell import EARTH_RADIUS_M, haversine_m  # noqa: E402

# Pipeline parameters shared by the hand-written timelines.
ENTER_M = 120
EXIT_M = 250
MIN_DWELL_S = 60
MAX_GAP_S = 120

METERS_PER_DEG_LAT = math.radians(1.0) * EARTH_RADIUS_M


def north(stop_lat: float, stop_lon: float, meters: float) -> tuple[float, float]:
    return stop_lat + meters / METERS_PER_DEG_LAT, stop_lon


def south(stop_lat: float, stop_lon: float, meters: float) -> tuple[float, float]:
    return stop_lat - meters / METERS_PER_DEG_LAT, stop_lon


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def zip_dir(src: Path, dest_zip: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.glob("*.txt")):
            zf.write(path, path.name)


# ---------------------------------------------------------------------------
# Static stops.  ALPHA/BRAVO are 200 m apart on one meridian: with enter 120 m
# and exit 250 m their enter circles overlap while their exit circles do not,
# which is exactly the "competing candidates, then locked station" scenario.
# ---------------------------------------------------------------------------

ALPHA = (52.5200000, 13.4050000)
BRAVO = (ALPHA[0] + 200.0 / METERS_PER_DEG_LAT, ALPHA[1])
# Stops 007 and 7 keep separate identities and sit ~140 m apart.
ID_007 = (52.5300000, 13.4100000)
ID_7 = (ID_007[0] + 140.0 / METERS_PER_DEG_LAT, ID_007[1])
# Antimeridian: one stop just east of 180, candidates on both sides.
ANTI = (0.0000000, 179.9990000)
# Near the north pole: longitude bands converge there.
POLAR = (89.9900000, 45.0000000)

STOPS_V1 = [
    ("ALPHA", "Alpha Square", *ALPHA),
    ("BRAVO", "Bravo Gate", *BRAVO),
    ("CHARLIE", "Charlie Corner", 52.5100000, 13.4200000),
    ("007", "Station Seven", *ID_007),
    ("7", "Station Seven Pseudo Dup", *ID_7),
    ("ANTI", "Date Line Stop", *ANTI),
    ("POLAR", "Polar Platform", *POLAR),
]

TRIPS_V1 = {
    "TRIP_A": ["ALPHA"],
    "TRIP_AB": ["ALPHA", "BRAVO"],
    "TRIP_C": ["CHARLIE"],
    "TRIP_ID": ["007", "7"],
    "TRIP_ANTI": ["ANTI"],
    "TRIP_POLAR": ["POLAR"],
}


def build_static(dest: Path, stops: list[tuple], trips: dict[str, list[str]]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob("*.txt"):
        stale.unlink()
    write_csv(dest / "agency.txt",
              ["agency_id", "agency_name", "agency_url", "agency_timezone"],
              [["DEMO", "Dwell Demo", "https://example.invalid/dwell", "UTC"]])
    write_csv(dest / "stops.txt",
              ["stop_id", "stop_name", "stop_lat", "stop_lon"],
              [[sid, name,
                lat if isinstance(lat, str) else "%.7f" % lat,
                lon if isinstance(lon, str) else "%.7f" % lon]
               for sid, name, lat, lon in stops])
    write_csv(dest / "routes.txt",
              ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
              [["R1", "DEMO", "D1", "Dwell Line", "3"]])
    write_csv(dest / "trips.txt",
              ["route_id", "service_id", "trip_id", "trip_headsign"],
              [["R1", "WK", trip_id, "Loop"] for trip_id in trips])
    stop_rows = []
    for trip_id, seq_stops in trips.items():
        for seq, sid in enumerate(seq_stops, start=1):
            hh = 8 + (seq // 60)
            mm = seq % 60
            tm = "%02d:%02d:00" % (hh, mm)
            stop_rows.append([trip_id, tm, tm, sid, seq])
    write_csv(dest / "stop_times.txt",
              ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
              stop_rows)
    write_csv(dest / "calendar.txt",
              ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
               "saturday", "sunday", "start_date", "end_date"],
              [["WK", "1", "1", "1", "1", "1", "1", "1", "20260101", "20261231"]])


def sample_rows(records: list[tuple]) -> list[list[str]]:
    """records: (run_id, sample_id, t, lat, lon) in any physical order."""
    def coord(value) -> str:
        if isinstance(value, str):
            return value
        return repr(value) if not math.isfinite(value) else "%.7f" % value

    return [[run_id, sid, str(t), coord(lat), coord(lon)]
            for run_id, sid, t, lat, lon in records]


def build() -> dict:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # --- static packages -------------------------------------------------
    v1 = FIXTURES / "dwell-v1"
    build_static(v1, STOPS_V1, TRIPS_V1)
    zip_dir(v1, DIST / "dwell-v1.zip")

    trips_v2 = dict(TRIPS_V1)
    trips_v2["TRIP_EXTRA_V2"] = ["ALPHA", "CHARLIE"]
    v2 = FIXTURES / "dwell-v2"
    build_static(v2, STOPS_V1, trips_v2)
    zip_dir(v2, DIST / "dwell-v2.zip")

    # v3: CHARLIE has no coordinates -> any run bound to a trip using it is
    # unprocessable, and the diagnostic must name CHARLIE.
    bad_stops = [(sid, name, "" if sid == "CHARLIE" else "%.7f" % lat,
                  "" if sid == "CHARLIE" else "%.7f" % lon)
                 for sid, name, lat, lon in STOPS_V1]
    v3 = FIXTURES / "dwell-v3-bad-coords"
    build_static(v3, [tuple(x) for x in bad_stops], TRIPS_V1)
    zip_dir(v3, DIST / "dwell-v3-bad-coords.zip")

    # --- hand-written sample timeline ------------------------------------
    samples: list[tuple] = []
    expected: dict = {"params": {"enter_m": ENTER_M, "exit_m": EXIT_M,
                                 "min_dwell_s": MIN_DWELL_S,
                                 "max_gap_s": MAX_GAP_S},
                      "runs": {}, "files": {}}

    def add(run_id, sid, t, lat, lon):
        samples.append((run_id, sid, t, lat, lon))

    # R1 normal arrival -> confirmed -> observed departure at ALPHA.
    r1 = [
        ("R1-a", 1000, *south(*ALPHA, 300.0)),
        ("R1-b", 1020, *south(*ALPHA, 80.0)),
        ("R1-c", 1050, ALPHA[0], ALPHA[1]),
        ("R1-d", 1080, *north(*ALPHA, 60.0)),    # t-first = 60 -> confirms here
        ("R1-e", 1110, *north(*ALPHA, 150.0)),   # inside exit (150 <= 250)
        ("R1-f", 1140, *north(*ALPHA, 320.0)),   # > exit -> observed departure
    ]
    for sid, t, lat, lon in r1:
        add("R1", sid, t, lat, lon)
    expected["runs"]["R1"] = {
        "trip": "TRIP_A", "edition": "v1",
        "dwells": [{
            "stop_id": "ALPHA", "end_reason": "observed_exit",
            "arrival_utc": 1020, "confirmed_utc": 1080,
            "last_observed_utc": 1110, "departure_utc": 1140,
            "support_ids": ["R1-b", "R1-c", "R1-d", "R1-e"],
            "first_outside_id": "R1-f",
        }]}

    # R2 overlapping enter circles: nearest choice at entry, then lock.
    # ALPHA and BRAVO are 200 m apart on the same meridian.
    r2 = [
        ("R2-a", 2000, *south(*ALPHA, 300.0)),
        ("R2-b", 2020, *south(*ALPHA, 90.0)),     # ~90 m ALPHA, ~110 m BRAVO
        ("R2-c", 2050, *south(*ALPHA, 60.0)),     # ~60 m ALPHA, ~140 m BRAVO
        ("R2-d", 2080, *south(*ALPHA, 20.0)),     # t-first=60 confirms ALPHA
        ("R2-e", 2110, *north(*ALPHA, 180.0)),    # 180 m ALPHA in exit; BRAVO
                                                  # ~20 m nearer but station locked
        ("R2-f", 2140, *north(*ALPHA, 330.0)),    # 330 m > ALPHA exit; also >120 m
                                                  # from BRAVO so it starts no string
        ("R2-g", 2170, *south(*BRAVO, 40.0)),     # ~40 m BRAVO -> new pending
        ("R2-h", 2200, BRAVO[0], BRAVO[1]),             # BRAVO string t=30
        ("R2-i", 2240, *north(*BRAVO, 30.0)),     # t-first=70 confirms BRAVO
        ("R2-j", 2270, *north(*BRAVO, 200.0)),    # inside exit
        ("R2-k", 2300, *north(*BRAVO, 300.0)),    # observed departure BRAVO
    ]
    for sid, t, lat, lon in r2:
        add("R2", sid, t, lat, lon)
    expected["runs"]["R2"] = {
        "trip": "TRIP_AB", "edition": "v1",
        "dwells": [
            {"stop_id": "ALPHA", "end_reason": "observed_exit",
             "arrival_utc": 2020, "confirmed_utc": 2080,
             "last_observed_utc": 2110, "departure_utc": 2140,
             "support_ids": ["R2-b", "R2-c", "R2-d", "R2-e"],
             "first_outside_id": "R2-f"},
            {"stop_id": "BRAVO", "end_reason": "observed_exit",
             "arrival_utc": 2170, "confirmed_utc": 2240,
             "last_observed_utc": 2270, "departure_utc": 2300,
             "support_ids": ["R2-g", "R2-h", "R2-i", "R2-j"],
             "first_outside_id": "R2-k"},
        ]}

    # R3 brief fly-by: inside the enter circle for 45 s -> no dwell.
    for sid, t, lat, lon in [
        ("R3-a", 3000, *south(*ALPHA, 300.0)),
        ("R3-b", 3020, *south(*ALPHA, 70.0)),
        ("R3-c", 3065, *north(*ALPHA, 70.0)),     # 45 s span, still pending
        ("R3-d", 3100, *north(*ALPHA, 300.0)),    # leaves; string discarded
    ]:
        add("R3", sid, t, lat, lon)
    expected["runs"]["R3"] = {"trip": "TRIP_A", "edition": "v1", "dwells": []}

    # R4 exact boundaries: == min_dwell confirms; == max_gap stays continuous;
    # a gap of max_gap+1 truncates, and the two sides become separate dwells.
    for sid, t, lat, lon in [
        ("R4-a", 4000, *south(*ALPHA, 60.0)),     # pending start
        ("R4-b", 4060, ALPHA[0], ALPHA[1]),       # exactly 60 s -> confirms
        ("R4-c", 4120, *north(*ALPHA, 330.0)),    # > exit: observed departure (and
                                                  # >120 m from BRAVO: no new string)
        ("R4-d", 4240, *south(*ALPHA, 60.0)),     # gap 120 == max_gap; idle -> pending
        ("R4-e", 4300, ALPHA[0], ALPHA[1]),       # new pending start at 4240, confirms
        ("R4-f", 4420, *north(*ALPHA, 60.0)),     # gap 120 == max_gap on an ACTIVE
                                                  # process: still continuous, stays locked
        ("R4-g", 4541, *south(*BRAVO, 40.0)),     # gap 121 > max_gap -> truncate
    ]:
        add("R4", sid, t, lat, lon)
    expected["runs"]["R4"] = {
        "trip": "TRIP_AB", "edition": "v1",
        "dwells": [
            {"stop_id": "ALPHA", "end_reason": "observed_exit",
             "arrival_utc": 4000, "confirmed_utc": 4060,
             "last_observed_utc": 4060, "departure_utc": 4120,
             "support_ids": ["R4-a", "R4-b"], "first_outside_id": "R4-c"},
            {"stop_id": "ALPHA", "end_reason": "gap_truncated",
             "arrival_utc": 4240, "confirmed_utc": 4300,
             "last_observed_utc": 4420, "departure_utc": None,
             "support_ids": ["R4-d", "R4-e", "R4-f"],
             "first_outside_id": None},
        ]}
    # R4-c sits 330 m north of ALPHA and 130 m from BRAVO, so it starts no
    # competing string; R4-g is only the truncation edge (single BRAVO point).

    # R5 end of file while still inside -> open_ended, no departure.
    for sid, t, lat, lon in [
        ("R5-a", 5000, *south(*ALPHA, 80.0)),
        ("R5-b", 5060, ALPHA[0], ALPHA[1]),             # confirms
        ("R5-c", 5090, *north(*ALPHA, 50.0)),     # EOF inside exit
    ]:
        add("R5", sid, t, lat, lon)
    expected["runs"]["R5"] = {
        "trip": "TRIP_A", "edition": "v1",
        "dwells": [{"stop_id": "ALPHA", "end_reason": "open_ended",
                    "arrival_utc": 5000, "confirmed_utc": 5060,
                    "last_observed_utc": 5090, "departure_utc": None,
                    "support_ids": ["R5-a", "R5-b", "R5-c"],
                    "first_outside_id": None}]}

    # R6 antimeridian: points hop across the 180 meridian; the index and the
    # exact Haversine path must both survive the longitude wraparound.
    anti_lat, anti_lon = ANTI
    anti_points = [
        ("R6-a", 6000, anti_lat, anti_lon - 0.0009),   # west, ~100 m -> idle lookup
        ("R6-b", 6060, anti_lat, -179.99995),          # other side of 180, ~117 m
        ("R6-c", 6120, anti_lat, -179.99999),          # ~112 m from the stop
        ("R6-d", 6180, anti_lat, anti_lon),            # on the stop line
    ]
    for sid, t, lat, lon in anti_points:
        add("R6", sid, t, lat, lon)
    expected["runs"]["R6"] = {
        "trip": "TRIP_ANTI", "edition": "v1",
        "dwells": [{"stop_id": "ANTI", "end_reason": "open_ended",
                    "arrival_utc": 6000, "confirmed_utc": 6060,
                    "last_observed_utc": 6180, "departure_utc": None,
                    "support_ids": ["R6-a", "R6-b", "R6-c", "R6-d"],
                    "first_outside_id": None}]}

    # R7 near the pole: a small longitude jump is a small physical distance.
    polar_lat, polar_lon = POLAR
    polar_points = [
        ("R7-a", 7000, polar_lat, polar_lon),
        ("R7-b", 7060, polar_lat, polar_lon + 5.0),    # ~87 m at this latitude
        ("R7-c", 7120, polar_lat, polar_lon),          # back, confirms timeline
    ]
    for sid, t, lat, lon in polar_points:
        add("R7", sid, t, lat, lon)
    expected["runs"]["R7"] = {
        "trip": "TRIP_POLAR", "edition": "v1",
        "dwells": [{"stop_id": "POLAR", "end_reason": "open_ended",
                    "arrival_utc": 7000, "confirmed_utc": 7060,
                    "last_observed_utc": 7120, "departure_utc": None,
                    "support_ids": ["R7-a", "R7-b", "R7-c"],
                    "first_outside_id": None}]}

    # R8 stops 007 and 7 as separate identities: dwell at each in one run,
    # with the first dwell explicitly closed by an observed exit in between.
    for sid, t, lat, lon in [
        ("R8-a", 8000, *south(*ID_007, 40.0)),
        ("R8-b", 8060, ID_007[0], ID_007[1]),
        ("R8-c", 8100, *south(*ID_007, 330.0)),   # > exit of 007; 470 m from 7:
                                                  # no competing string starts
        ("R8-d", 8160, *south(*ID_7, 40.0)),
        ("R8-e", 8220, ID_7[0], ID_7[1]),
        ("R8-f", 8260, *north(*ID_7, 50.0)),
    ]:
        add("R8", sid, t, lat, lon)
    expected["runs"]["R8"] = {
        "trip": "TRIP_ID", "edition": "v1",
        "dwells": [
            {"stop_id": "007", "end_reason": "observed_exit",
             "arrival_utc": 8000, "confirmed_utc": 8060,
             "last_observed_utc": 8060, "departure_utc": 8100,
             "support_ids": ["R8-a", "R8-b"], "first_outside_id": "R8-c"},
            {"stop_id": "7", "end_reason": "open_ended",
             "arrival_utc": 8160, "confirmed_utc": 8220,
             "last_observed_utc": 8260, "departure_utc": None,
             "support_ids": ["R8-d", "R8-e", "R8-f"], "first_outside_id": None},
        ]}

    # R15 gap splitting one physical stay into two dwells: the first dwell is
    # truncated by a >max_gap_s hole with no departure; points afterwards
    # re-enter from idle and produce a separate, complete dwell with its own
    # arrival/confirm instants (the two sides are never stitched together).
    for sid, t, lat, lon in [
        ("R15-a", 10000, *south(*ALPHA, 60.0)),
        ("R15-b", 10060, ALPHA[0], ALPHA[1]),           # confirms first dwell
        ("R15-c", 10100, *north(*ALPHA, 40.0)),         # last point before the hole
        ("R15-d", 10500, *south(*ALPHA, 60.0)),         # gap 400 > 120: idle again
        ("R15-e", 10560, ALPHA[0], ALPHA[1]),           # confirms second dwell
        ("R15-f", 10600, *north(*ALPHA, 50.0)),
        ("R15-g", 10640, *north(*ALPHA, 330.0)),        # observed departure
    ]:
        add("R15", sid, t, lat, lon)
    expected["runs"]["R15"] = {
        "trip": "TRIP_A", "edition": "v1",
        "dwells": [
            {"stop_id": "ALPHA", "end_reason": "gap_truncated",
             "arrival_utc": 10000, "confirmed_utc": 10060,
             "last_observed_utc": 10100, "departure_utc": None,
             "support_ids": ["R15-a", "R15-b", "R15-c"], "first_outside_id": None},
            {"stop_id": "ALPHA", "end_reason": "observed_exit",
             "arrival_utc": 10500, "confirmed_utc": 10560,
             "last_observed_utc": 10600, "departure_utc": 10640,
             "support_ids": ["R15-d", "R15-e", "R15-f"], "first_outside_id": "R15-g"},
        ]}

    # R9 is R1's timeline written in shuffled physical order; result must be
    # identical (same run, same ids, chronological restoration).
    shuffle_order = [3, 0, 5, 1, 4, 2]
    for i in shuffle_order:
        sid, t, lat, lon = r1[i]
        add("R9", sid.replace("R1-", "R9-"), t, lat, lon)
    expected["runs"]["R9"] = {
        "trip": "TRIP_A", "edition": "v1",
        "dwells": [{
            "stop_id": "ALPHA", "end_reason": "observed_exit",
            "arrival_utc": 1020, "confirmed_utc": 1080,
            "last_observed_utc": 1110, "departure_utc": 1140,
            "support_ids": ["R9-b", "R9-c", "R9-d", "R9-e"],
            "first_outside_id": "R9-f"}],
        "shuffled_source_lines": True}

    # R10 isolated bad runs (each fails; sibling good run R10-OK survives).
    add("R10-BADCOORD", "X1", 9000, *south(*ALPHA, 60.0))
    add("R10-BADCOORD", "X2", 9060, float("nan"), 13.0)     # non-finite lat
    add("R10-BADCOORD", "X3", 9120, ALPHA[0], ALPHA[1])
    add("R10-DUPID", "D1", 9000, *south(*ALPHA, 60.0))
    add("R10-DUPID", "D1", 9060, ALPHA[0], ALPHA[1])              # duplicate sample_id
    add("R10-DUPTS", "P1", 9000, *south(*ALPHA, 60.0))
    add("R10-DUPTS", "P2", 9000, ALPHA[0], ALPHA[1])              # same timestamp
    add("R10-OK", "G1", 9000, *south(*ALPHA, 80.0))
    add("R10-OK", "G2", 9060, ALPHA[0], ALPHA[1])
    add("R10-OK", "G3", 9090, *north(*ALPHA, 40.0))
    expected["runs"]["R10-BADCOORD"] = {"trip": "TRIP_A", "edition": "v1",
                                        "fails": ["bad_coordinate"]}
    expected["runs"]["R10-DUPID"] = {"trip": "TRIP_A", "edition": "v1",
                                     "fails": ["duplicate_sample_id"]}
    expected["runs"]["R10-DUPTS"] = {"trip": "TRIP_A", "edition": "v1",
                                     "fails": ["duplicate_timestamp"]}
    expected["runs"]["R10-OK"] = {
        "trip": "TRIP_A", "edition": "v1",
        "dwells": [{"stop_id": "ALPHA", "end_reason": "open_ended",
                    "arrival_utc": 9000, "confirmed_utc": 9060,
                    "last_observed_utc": 9090, "departure_utc": None,
                    "support_ids": ["G1", "G2", "G3"], "first_outside_id": None}]}

    # R11 is bound to v2 (different static edition) -> edition isolation.
    add("R11-V2", "V1", 9500, *south(*ALPHA, 80.0))
    add("R11-V2", "V2", 9560, ALPHA[0], ALPHA[1])
    add("R11-V2", "V3", 9590, *north(*ALPHA, 40.0))
    expected["runs"]["R11-V2"] = {
        "trip": "TRIP_EXTRA_V2", "edition": "v2",
        "dwells": [{"stop_id": "ALPHA", "end_reason": "open_ended",
                    "arrival_utc": 9500, "confirmed_utc": 9560,
                    "last_observed_utc": 9590, "departure_utc": None,
                    "support_ids": ["V1", "V2", "V3"], "first_outside_id": None}]}

    # R12 bound to v3 where CHARLIE has no coordinates -> run-level failure.
    add("R12-BADSTOP", "B1", 9600, 52.5100000, 13.4200000)
    expected["runs"]["R12-BADSTOP"] = {"trip": "TRIP_C", "edition": "v3-bad-coords",
                                       "fails": ["candidate_stops"],
                                       "bad_stop": "CHARLIE"}

    write_csv(DIST / "samples.csv",
              ["run_id", "sample_id", "observed_utc", "lat", "lon"],
              sample_rows(samples))

    # R13 appears in samples but has no binding row -> unregistered run.
    extra = DIST / "samples-unregistered.csv"
    write_csv(extra, ["run_id", "sample_id", "observed_utc", "lat", "lon"],
              sample_rows(samples) + [["R13-NOBIND", "N1", "9700",
                                       "%.7f" % ALPHA[0], "%.7f" % ALPHA[1]]])
    expected["runs"]["R13-NOBIND"] = {"fails": ["unregistered_run"],
                                      "trip": None, "edition": None}

    # R14 is bound but has no sample rows at all.
    bindings = []
    for run_id, spec in expected["runs"].items():
        if spec.get("fails") == ["unregistered_run"]:
            continue
        bindings.append([run_id, "dwell", spec["edition"], spec["trip"]])
    bindings.append(["R14-EMPTY", "dwell", "v1", "TRIP_A"])
    expected["runs"]["R14-EMPTY"] = {"trip": "TRIP_A", "edition": "v1",
                                     "fails": ["no_samples"]}
    write_csv(DIST / "bindings.csv",
              ["run_id", "feed_key", "edition", "trip_id"], bindings)

    # File-level failures: a row with no run_id, and an unparseable binding.
    write_csv(DIST / "samples-missing-run.csv",
              ["run_id", "sample_id", "observed_utc", "lat", "lon"],
              [["", "Z1", "9800", "52.52", "13.40"]])
    write_csv(DIST / "bindings-broken.csv",
              ["run_id", "feed_key", "edition", "trip_id"],
              [["RX", "dwell", "v1", ""]])

    # --- large fixture: many candidates, samples near a handful ------------
    large_stops = []
    n_large = 400
    for i in range(n_large):
        large_stops.append(("L%04d" % i, "Large Stop %d" % i,
                            40.0 + i * 0.001, -74.0))
    large_trips = {"TRIP_LARGE": [s[0] for s in large_stops]}
    lv1 = FIXTURES / "dwell-large"
    build_static(lv1, large_stops, large_trips)
    zip_dir(lv1, DIST / "dwell-large.zip")
    target = large_stops[200]
    # Candidates are ~111 m apart along a meridian.  Every sample stays within
    # 25 m of L0200 and at least ~86 m from its neighbours, so L0200 is always
    # the unique nearest candidate inside the 120 m enter circle; the grid
    # index still only opens the few cells around each point.
    large_samples = []
    for k in range(40):
        lat = target[2] + (0.0 if k % 2 else -0.0002)  # 0 m / ~22 m hops
        large_samples.append(["LRUN", "S%03d" % k, 10000 + 30 * k,
                               "%.7f" % lat, "%.7f" % target[3]])
    write_csv(DIST / "samples-large.csv",
              ["run_id", "sample_id", "observed_utc", "lat", "lon"],
              large_samples)
    expected["files"]["large"] = {
        "candidate_stops": n_large, "samples": 40,
        "dwell": {"stop_id": "L0200", "end_reason": "open_ended",
                  "arrival_utc": 10000, "confirmed_utc": 10060,
                  "support_points": 40},
        # 40 points x at most the few cells of a ~111 m grid vs a 400-stop
        # Cartesian scan (16000 exact checks).
        "brute_force_checks": n_large * 40}
    write_csv(DIST / "bindings-large.csv",
              ["run_id", "feed_key", "edition", "trip_id"],
              [["LRUN", "dwell", "large", "TRIP_LARGE"]])

    # Record the actual designed distances so the walkthrough/tests can show
    # real numbers instead of rounded metre claims.
    distance_audit = {}
    for run_id, spec in expected["runs"].items():
        if "dwells" not in spec:
            continue
        pts = {s[1]: (s[3], s[4]) for s in samples if s[0] == run_id}
        audit = {}
        for dwell in spec["dwells"]:
            stop = next(s for s in STOPS_V1 if s[0] == dwell["stop_id"])
            for sid in dwell["support_ids"] + (
                    [dwell["first_outside_id"]] if dwell["first_outside_id"] else []):
                lat, lon = pts[sid]
                if not math.isfinite(lat) or not math.isfinite(lon):
                    continue
                audit[sid] = round(haversine_m(lat, lon, stop[2], stop[3]), 4)
        distance_audit[run_id] = audit
    expected["distance_audit"] = distance_audit

    (DIST / "dwell-expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return expected


if __name__ == "__main__":
    exp = build()
    print("dwell fixtures built in", DIST)
    print("runs designed:", ", ".join(sorted(exp["runs"])))
