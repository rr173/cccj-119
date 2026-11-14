"""Build the synthetic-but-realistic GTFS fixtures used by the test suite.

Run from the repository root:  python3 tools/build_fixtures.py
Every fixture directory is created empty; the script never reads existing
files and only writes its own outputs.
"""

from __future__ import annotations

import csv
import io
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
DIST = ROOT / "dist"

AGENCY = [
    ["agency_id", "agency_name", "agency_url", "agency_timezone"],
    ["DEMO", "Demo Bus", "https://example.invalid/demo", "Europe/Berlin"],
]

STOPS = [
    ["stop_id", "stop_name", "stop_lat", "stop_lon"],
    ["007", "Station Seven", "52.5200", "13.4050"],
    ["7", "Station Seven (duplicate-looking id)", "52.5201", "13.4051"],
    ["S2", "Second Street", "52.5210", "13.4100"],
    ["S3", "Third Street, Platform 1", "52.5220", "13.4150"],
    ["S4", "Fourth Street", "52.5230", "13.4200"],
    ["S5", "Fifth Street", "52.5240", "13.4250"],
    ["S6", "Sixth Street", "52.5250", "13.4300"],
    ["S7", "Seventh Street", "52.5260", "13.4350"],
    ["S8", "Eighth Street", "52.5270", "13.4400"],
    ["S9", "Ninth Street", "52.5280", "13.4450"],
    ["SA", "Tenth Street", "52.5290", "13.4500"],
]

ROUTES = [
    ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
    ["R1", "DEMO", "7", "Seven Line", "3"],
    ["R2", "DEMO", "8", "Eight Line", "3"],
    ["R3", "DEMO", "9", "Nine Line", "3"],
    ["R4", "DEMO", "10", "Ten Line", "3"],
    ["R5", "DEMO", "11", "Eleven Line", "3"],
]

TRIPS = [
    ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"],
    ["R1", "WKDY", "T_WKDY", "East", "0"],
    ["R2", "WKDY", "T_FREQ_EXACT", "East", "0"],
    ["R3", "WKDY", "T_FREQ_WINDOW", "East", "0"],
    ["R1", "WKDY", "T_LATE", "East", "0"],
    ["R1", "WKND", "T_WKND", "East", "0"],
    ["R2", "WKND_ONLY_ADDED", "T_ADDED", "East", "0"],
    ["R3", "SAT_REMOVED", "T_SAT_REMOVED", "East", "0"],
    ["R5", "WKDY", "T_ADJACENT", "East", "0"],
    ["R4", "WKDY", "T_TWO_STOPS", "East", "0"],
    ["R1", "WKND", "T_WKND_LATE", "East", "0"],
    ["R4", "WKDY", "T_ID7", "East", "0"],
    ["R2", "WKDY", "T_MIDNIGHT", "East", "0"],
    ["R3", "WKDY", "T_FREQ_DEFAULT", "East", "0"],
]

# Non-consecutive stop_sequence: 10, 20, 30, 40.
# 07:00:00 departure at stop 007, arrival 07:05, etc.
STOP_TIMES = [
    ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    ["T_WKDY", "08:00:00", "08:00:00", "007", "10"],
    ["T_WKDY", "08:05:00", "08:05:30", "S2", "20"],
    ["T_WKDY", "08:10:00", "08:10:00", "S3", "30"],
    ["T_WKDY", "08:20:00", "08:20:00", "S4", "40"],
    # Frequency template: first departure 09:00:00, later stops shifted by
    # 00:10:00 and 00:20:00.
    ["T_FREQ_EXACT", "09:00:00", "09:00:00", "007", "1"],
    ["T_FREQ_EXACT", "09:10:00", "09:10:00", "S2", "2"],
    ["T_FREQ_EXACT", "09:20:00", "09:20:00", "S3", "3"],
    # Window-only template.
    ["T_FREQ_WINDOW", "06:00:00", "06:00:00", "007", "1"],
    ["T_FREQ_WINDOW", "06:06:00", "06:06:00", "S2", "2"],
    ["T_FREQ_WINDOW", "06:12:00", "06:12:00", "S3", "3"],
    # Past midnight.
    ["T_LATE", "25:10:00", "25:10:00", "007", "1"],
    ["T_LATE", "25:20:00", "25:20:00", "S2", "2"],
    # Weekend.
    ["T_WKND", "10:30:00", "10:30:00", "007", "1"],
    ["T_WKND", "10:40:00", "10:40:00", "S2", "2"],
    # Weekend late run: 26:30 is 02:30 on the following civil day, which does
    # not exist on 2026-03-29 (spring forward) and is ambiguous on 2026-10-25.
    ["T_WKND_LATE", "26:30:00", "26:30:00", "007", "1"],
    ["T_WKND_LATE", "26:40:00", "26:40:00", "S2", "2"],
    # Only via calendar_dates addition.
    ["T_ADDED", "11:15:00", "11:15:00", "007", "1"],
    ["T_ADDED", "11:25:00", "11:25:00", "S2", "2"],
    # Saturday service removed by exception.
    ["T_SAT_REMOVED", "12:00:00", "12:00:00", "007", "1"],
    ["T_SAT_REMOVED", "12:08:00", "12:08:00", "S2", "2"],
    # Two adjacent exact-frequency intervals, each with its own instance ids.
    ["T_ADJACENT", "13:00:00", "13:00:00", "007", "1"],
    ["T_ADJACENT", "13:04:00", "13:04:00", "S2", "2"],
    # Serves the separate stop id "7" (distinct from "007").
    ["T_ID7", "15:00:00", "15:00:00", "7", "1"],
    ["T_ID7", "15:05:00", "15:05:00", "S8", "2"],
    # Frequency template crossing midnight: stop offsets 0 and 0:20.
    ["T_MIDNIGHT", "23:00:00", "23:00:00", "007", "1"],
    ["T_MIDNIGHT", "23:20:00", "23:20:00", "S2", "2"],
    # exact_times column omitted -> GTFS default 0 (headway promise).
    ["T_FREQ_DEFAULT", "17:00:00", "17:00:00", "007", "1"],
    ["T_FREQ_DEFAULT", "17:07:00", "17:07:00", "S2", "2"],
    # Ordinary trip for right after midnight boundary checks.
    ["T_TWO_STOPS", "06:20:00", "06:20:00", "S5", "1"],
    ["T_TWO_STOPS", "06:30:00", "06:30:00", "S6", "2"],
]

CALENDAR = [
    ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
     "saturday", "sunday", "start_date", "end_date"],
    ["WKDY", "1", "1", "1", "1", "1", "1", "0", "20260301", "20261031"],
    ["WKND", "0", "0", "0", "0", "0", "1", "1", "20260301", "20261031"],
    ["SAT_REMOVED", "0", "0", "0", "0", "0", "1", "0", "20260301", "20261031"],
]

# Europe/Berlin spring forward 2026-03-29 (Sunday) and fall back 2026-10-25.
# WKND runs on Saturday and Sunday.  T_WKND at 10:30 local is already after
# the transition on the spring date; T_LATE (25:10 from WKND) crosses midnight
# into Monday and would demonstrate offset changes if WKDY applied it.
# Additionally a late-night weekend trip exercises 25:xx across the fall-back
# Sunday/Monday boundary.
CALENDAR_DATES = [
    ["service_id", "date", "exception_type", "holiday_name"],
    # Sunday 2026-03-29 service removed from WKND, added on Monday 2026-03-30.
    ["WKND", "20260329", "2", "spring gap day"],
    ["WKND", "20260330", "1", "spring makeup"],
    # Only calendar_dates service.
    ["WKND_ONLY_ADDED", "20260330", "1", "one-off service"],
    ["WKDY", "20260403", "2", "Good Friday"],
    ["WKND", "20260403", "1", "Good Friday weekend service"],
    ["WKDY", "20261025", "2", "fall back Sunday"],
    ["WKND", "20261025", "1", "fall back Sunday weekend service"],
]

FREQUENCIES = [
    ["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
    ["T_FREQ_EXACT", "09:30:00", "12:00:00", "600", "1"],
    ["T_FREQ_EXACT", "12:00:00", "14:00:00", "600", "1"],
    ["T_FREQ_WINDOW", "07:00:00", "09:00:00", "300", "0"],
    ["T_ADJACENT", "13:00:00", "14:00:00", "1800", "1"],
    ["T_ADJACENT", "14:00:00", "15:00:00", "1800", "1"],
    # [23:30, 24:45) every 30 min; the last departure at 24:30 still serves
    # its second stop at 24:50, beyond the window end.
    ["T_MIDNIGHT", "23:30:00", "24:45:00", "1800", "1"],
    ["T_FREQ_DEFAULT", "17:00:00", "18:00:00", "1200", ""],
]


def write_csv(path: Path, rows: list[list[str]], *, bom: bool = False) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow(row)
    data = buf.getvalue().encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


TRIPS_V2 = [row[:] for row in TRIPS]
TRIPS_V2.append(["R2", "WKDY", "T_WKDY_V2_EXTRA", "East", "0"])

STOP_TIMES_V2 = [row[:] for row in STOP_TIMES]
STOP_TIMES_V2 += [
    ["T_WKDY_V2_EXTRA", "09:00:00", "09:00:00", "007", "1"],
    ["T_WKDY_V2_EXTRA", "09:15:00", "09:15:00", "S2", "2"],
]

FREQUENCIES_V2 = [row[:] for row in FREQUENCIES]


def build_demo(dest: Path, *, v2: bool = False, extra_files: dict[str, bytes] | None = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob("*.txt"):
        stale.unlink()
    write_csv(dest / "agency.txt", AGENCY)
    write_csv(dest / "stops.txt", STOPS)          # 007 and 7 both present
    write_csv(dest / "routes.txt", ROUTES)
    write_csv(dest / "trips.txt", TRIPS_V2 if v2 else TRIPS)
    # UTF-8 BOM on the largest file, exercising BOM detection.
    write_csv(dest / "stop_times.txt", STOP_TIMES_V2 if v2 else STOP_TIMES, bom=True)
    write_csv(dest / "calendar.txt", CALENDAR)
    write_csv(dest / "calendar_dates.txt", CALENDAR_DATES)
    write_csv(dest / "frequencies.txt", FREQUENCIES_V2 if v2 else FREQUENCIES)
    for name, data in (extra_files or {}).items():
        (dest / name).write_bytes(data)


def zip_dir(src: Path, dest_zip: Path, *, bom_ok: bool = True) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.glob("*.txt")):
            zf.write(path, path.name)


def main() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    build_demo(FIXTURES / "demo-v1")
    build_demo(FIXTURES / "demo-v2", v2=True)
    zip_dir(FIXTURES / "demo-v1", DIST / "demo-v1.zip")
    zip_dir(FIXTURES / "demo-v2", DIST / "demo-v2.zip")

    # A separate operator whose internal ids deliberately collide with demo.
    other = FIXTURES / "other-operator"
    if other.exists():
        shutil.rmtree(other)
    other.mkdir(parents=True)
    write_csv(other / "agency.txt", [
        ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        ["DEMO", "Other Bus", "https://example.invalid/other", "Europe/Berlin"]])
    write_csv(other / "stops.txt", [
        ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        ["007", "Other Station Seven", "48.1370", "11.5750"],
        ["7", "Other Station Seven pseudo-dup", "48.1371", "11.5751"],
        ["S2", "Other Second Street", "48.1380", "11.5760"]])
    write_csv(other / "routes.txt", [
        ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
        ["R1", "DEMO", "7", "Other Seven", "3"]])
    write_csv(other / "trips.txt", [
        ["route_id", "service_id", "trip_id", "trip_headsign"],
        ["R1", "WKDY", "T_WKDY", "Other East"]])
    write_csv(other / "stop_times.txt", [
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        ["T_WKDY", "16:00:00", "16:00:00", "007", "1"],
        ["T_WKDY", "16:07:00", "16:07:00", "S2", "2"]])
    write_csv(other / "calendar.txt", [
        ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
         "saturday", "sunday", "start_date", "end_date"],
        ["WKDY", "1", "1", "1", "1", "1", "1", "0", "20260301", "20261031"]])
    zip_dir(other, DIST / "other-operator.zip")

    # Rejected packages -------------------------------------------------
    rej = FIXTURES / "reject-overlap"
    build_demo(rej)
    write_csv(rej / "frequencies.txt", FREQUENCIES + [
        ["T_FREQ_EXACT", "11:00:00", "13:00:00", "900", "1"]])
    zip_dir(rej, DIST / "reject-overlap.zip")

    rej = FIXTURES / "reject-missing-time"
    build_demo(rej)
    rows = [r[:] for r in STOP_TIMES]
    rows[2] = ["T_WKDY", "", "08:05:30", "S2", "20"]
    write_csv(rej / "stop_times.txt", rows, bom=True)
    zip_dir(rej, DIST / "reject-missing-time.zip")

    rej = FIXTURES / "reject-foreign-key"
    build_demo(rej)
    rows = [r[:] for r in STOP_TIMES]
    rows[1] = ["T_WKDY", "08:05:00", "08:05:30", "S_GONE", "20"]
    write_csv(rej / "stop_times.txt", rows, bom=True)
    zip_dir(rej, DIST / "reject-foreign-key.zip")

    # A trip whose times contradict each other: the package is accepted, the
    # trip is quarantined and never expanded.  (Reported as a trip issue.)
    q = FIXTURES / "quarantine-bad-timing"
    build_demo(q)
    rows = [r[:] for r in STOP_TIMES]
    # The S2 call (sequence 20) now arrives before the first stop departs;
    # the trip is internally contradictory and must never be expanded.
    rows[2] = ["T_WKDY", "07:55:00", "08:05:30", "S2", "20"]
    write_csv(q / "stop_times.txt", rows, bom=True)
    zip_dir(q, DIST / "quarantine-bad-timing.zip")

    print("fixtures built in", FIXTURES)
    print("zips built in", DIST)


if __name__ == "__main__":
    main()
