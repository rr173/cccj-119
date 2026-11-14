"""Offline GPS dwell processing: sample files -> stop dwell facts with evidence.

The module is deliberately narrow.  It never matches roads, never infers a
route, never computes expected arrivals and never counts passengers or fares.
Given, for each ``run_id``, a binding to one exact static
``(feed_key, edition, trip_id)``, it recognises *spatial* dwells at the
distinct stops the trip actually visits:

* candidates are the distinct ``stop_id`` values the bound trip passes (a
  stop visited twice stays one spatial candidate; no visit ordinal is guessed);
* every point is only matched against that run's own candidates through a
  queryable bounding-grid spatial index, never a point x stop Cartesian product;
* dwells are produced by an explicit, explainable state machine with separate
  enter/exit radii, minimum dwell duration and maximum observation gap.

Every produced fact keeps the raw samples (with source file lines) that
support it, so each dwell can be traced back to the original points.  A job is
written in one transaction: a crash before commit exposes no half-written
dwell and a rerun simply creates a new, independent job id.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import GtfsError, RejectError
from .model import read_csv_bytes

EARTH_RADIUS_M = 6371008.8

DWELL_SCHEMA_VERSION = "1"

SAMPLE_COLUMNS = ("run_id", "sample_id", "observed_utc", "lat", "lon")
BINDING_COLUMNS = ("run_id", "feed_key", "edition", "trip_id")

# Dwell end reasons.  A gap and end-of-file are never treated as an observed
# departure: both leave departure_utc NULL.
END_OBSERVED_EXIT = "observed_exit"
END_GAP_TRUNCATED = "gap_truncated"
END_OPEN_ENDED = "open_ended"


class DwellFileError(GtfsError):
    """File-level failure: the input cannot be parsed reliably at all."""

    exit_code = 1


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres on a sphere of radius EARTH_RADIUS_M.

    The comparison contract uses full float precision: results are never
    rounded to whole metres before being compared with a radius.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2)
    # Clamp for safety against tiny negative values from floating point.
    a = min(1.0, max(0.0, a))
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def finite_coord(lat: Any, lon: Any) -> bool:
    return (isinstance(lat, float) and isinstance(lon, float)
            and math.isfinite(lat) and math.isfinite(lon)
            and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)


class SpatialIndex:
    """Bounding-grid index over one run's candidate stops.

    The grid uses uniform angular cells.  A query returns the stops whose
    cell overlaps the *conservative* bounding box of the enter circle, after
    which the caller computes exact Haversine distances.  The coarse filter is
    guaranteed not to drop a stop that is truly inside the circle, including
    across the 180 degree meridian and near the poles:

    * longitude wraps on a normalised 0..360 grid, so a box spanning the
      antimeridian scans the cells on both edges;
    * when the latitude box reaches a pole, every longitude band is scanned,
      because longitude lines converge and a longitude box is meaningless
      there.
    """

    def __init__(self, stops: list[dict[str, Any]], radius_m: float) -> None:
        self.stops = stops
        self.radius_m = radius_m
        # Angular size of the radius, taken at the most generous meridian so
        # the box never under-covers; cos(lat) shrinks east-west spacing, so
        # the longitude half-width is recomputed per query at the point's
        # latitude.  The cell size derives from the meridian spacing, which is
        # the largest angular radius needed anywhere.
        self.cell_deg = math.degrees(radius_m / EARTH_RADIUS_M)
        if self.cell_deg <= 0:
            raise GtfsError("spatial index radius must be positive")
        # cells[(lat_cell, lon_cell)] -> list of stop indexes.  Longitude cell
        # indexes live in [0, grid_cells); query cells are folded with the same
        # modulo, so the two seams of the normalised 0..360 grid agree.
        self._grid_cells = max(1, int(math.ceil(360.0 / self.cell_deg)))
        self.cells: dict[tuple[int, int], list[int]] = {}
        for idx, stop in enumerate(stops):
            lon_norm = stop["stop_lon"] % 360.0
            key = (int(math.floor(stop["stop_lat"] / self.cell_deg)),
                   int(math.floor(lon_norm / self.cell_deg)))
            self.cells.setdefault(key, []).append(idx)
        self.occupied_lon_bands = sorted({k[1] for k in self.cells})
        self.lon_bands = len(self.occupied_lon_bands)
        self.lat_bands = len({k[0] for k in self.cells})
        # Number of longitude bands tiling the full 0..360 sphere.
        self.lon_grid_size = self._grid_cells
        self.candidate_checks = 0
        self.cell_scans = 0

    def _covering_cells(self, lat: float, lon: float) -> Iterable[tuple[int, int]]:
        dlat = self.cell_deg
        # East-west angular spacing at the *highest* latitude the box reaches,
        # so the box stays conservative for stops slightly nearer the pole.
        extreme_lat = min(90.0, abs(lat) + dlat)
        cos_lat = max(math.cos(math.radians(extreme_lat)), 1e-12)
        dlon = min(dlat / cos_lat, 360.0)
        lat_lo = lat - dlat
        lat_hi = lat + dlat
        lon_norm = lon % 360.0
        lon_lo = lon_norm - dlon
        lon_hi = lon_norm + dlon
        lat_cells = range(int(math.floor(lat_lo / dlat)),
                          int(math.floor(lat_hi / dlat)) + 1)
        # Pole case: the latitude box touches +/-90, longitude bands converge;
        # scan every *occupied* longitude band for each touched latitude band.
        all_lon = lat_lo <= -90.0 or lat_hi >= 90.0 or dlon >= 180.0
        if all_lon:
            lon_cells = self.occupied_lon_bands
        else:
            # Enumerate the box on the normalised 0..360 axis.  When the box
            # spills past a seam, split it into the in-range pieces instead of
            # folding cell indexes (the last cell may be narrower): a box
            # crossing 0 yields [0, hi] plus [360+lo, 360), and a box crossing
            # 360 yields [lo, 360) plus [0, hi-360].
            ranges: list[tuple[float, float]] = []
            if lon_lo < 0.0:
                ranges.append((0.0, lon_hi))
                ranges.append((360.0 + lon_lo, 360.0))
            elif lon_hi > 360.0:
                ranges.append((lon_lo, 360.0))
                ranges.append((0.0, lon_hi - 360.0))
            else:
                ranges.append((lon_lo, lon_hi))
            lon_cells = set()
            for lo_edge, hi_edge in ranges:
                c0 = int(math.floor(lo_edge / dlat))
                c1 = int(math.floor(hi_edge / dlat))
                lon_cells.update(range(c0, c1 + 1))
        for la in lat_cells:
            for lo in lon_cells:
                yield la, lo

    def candidates(self, lat: float, lon: float) -> list[int]:
        """Indexes of stops whose grid cell may cover the point's enter box."""
        seen: set[int] = set()
        for key in self._covering_cells(lat, lon):
            self.cell_scans += 1
            for idx in self.cells.get(key, ()):
                seen.add(idx)
        self.candidate_checks += len(seen)
        return sorted(seen)

    def explain(self) -> dict[str, Any]:
        return {
            "index": "uniform_latlon_grid",
            "cell_size_degrees": self.cell_deg,
            "occupied_lat_bands": self.lat_bands,
            "occupied_lon_bands": self.lon_bands,
            "occupied_cells": len(self.cells),
            "indexed_stops": len(self.stops),
            "candidate_checks": self.candidate_checks,
            "cell_scans": self.cell_scans,
            "antimeridian_safe": True,
            "pole_safe": True,
        }


def nearest_candidate(index: SpatialIndex, lat: float, lon: float,
                      radius_m: float) -> tuple[dict[str, Any], float] | None:
    """Pick the nearest indexed stop within radius_m, or None.

    Only the run's own candidate stops participate; exact ties resolve by the
    Unicode code point order of stop_id (Python's default string ordering),
    never by input order.
    """
    best: tuple[float, str, dict] | None = None
    for idx in index.candidates(lat, lon):
        stop = index.stops[idx]
        distance = haversine_m(lat, lon, stop["stop_lat"], stop["stop_lon"])
        if distance <= radius_m and (best is None or (distance, stop["stop_id"]) < best[:2]):
            best = (distance, stop["stop_id"], stop)
    if best is None:
        return None
    return best[2], best[0]


def brute_force_nearest(stops: list[dict[str, Any]], lat: float, lon: float,
                        radius_m: float) -> tuple[dict[str, Any], float] | None:
    """Independent reference path: every candidate stop is checked exactly.

    Used by the small-fixture tests and the walkthrough to prove the spatial
    index returns the same candidate as an exhaustive scan; production
    processing never calls this.
    """
    best: tuple[float, str, dict] | None = None
    for stop in stops:
        distance = haversine_m(lat, lon, stop["stop_lat"], stop["stop_lon"])
        if distance <= radius_m and (best is None or (distance, stop["stop_id"]) < best[:2]):
            best = (distance, stop["stop_id"], stop)
    if best is None:
        return None
    return best[2], best[0]


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    run_id: str
    sample_id: str
    observed_utc: int
    lat: float
    lon: float
    source_line: int


@dataclass
class _RawRow:
    run_id: str | None
    sample_id: str | None
    observed_raw: str
    lat_raw: str
    lon_raw: str
    source_line: int


def _parse_csv(data: bytes, expected: tuple[str, ...],
               kind: str) -> tuple[list[str], list[tuple[dict[str, str], int, int]]]:
    try:
        header, rows = read_csv_bytes(data)
    except RejectError as exc:
        raise DwellFileError("%s file cannot be parsed: %s" % (kind, exc.message)) from exc
    if tuple(header) != expected:
        # Header order/content is part of the contract; do not guess columns.
        raise DwellFileError(
            "%s file header must be exactly %s, got %s"
            % (kind, ",".join(expected), ",".join(header) if header else "(empty)"))
    return header, rows


def parse_bindings(data: bytes) -> dict[str, dict[str, str]]:
    """Parse the run -> (feed_key, edition, trip_id) binding CSV."""
    _header, rows = _parse_csv(data, BINDING_COLUMNS, "binding")
    bindings: dict[str, dict[str, str]] = {}
    for raw, line, _field_count in rows:
        run_id = raw["run_id"]
        if not run_id:
            raise DwellFileError("binding line %d: run_id must not be empty" % line)
        for col in ("feed_key", "edition", "trip_id"):
            if not raw[col]:
                raise DwellFileError(
                    "binding line %d: %s must not be empty for run_id=%s"
                    % (line, col, run_id))
        if run_id in bindings:
            raise DwellFileError(
                "binding line %d: run_id=%s is bound more than once" % (line, run_id))
        bindings[run_id] = {
            "feed_key": raw["feed_key"], "edition": raw["edition"],
            "trip_id": raw["trip_id"], "source_line": line,
        }
    return bindings


def parse_samples(data: bytes) -> tuple[list[_RawRow], list[tuple[int, str]]]:
    """Parse the sample CSV.

    Returns (rows, file_problems).  A missing run_id is a file-level failure
    (the row cannot be attributed to any run); every other malformed row is
    kept raw and charged to its run later, so sibling runs still get results.
    """
    _header, rows = _parse_csv(data, SAMPLE_COLUMNS, "sample")
    parsed: list[_RawRow] = []
    file_problems: list[tuple[int, str]] = []
    for raw, line, _field_count in rows:
        run_id = raw["run_id"] or None
        if run_id is None:
            file_problems.append((line, "run_id is empty; the row cannot be attributed to a run"))
            continue
        parsed.append(_RawRow(run_id, raw["sample_id"] or None, raw["observed_utc"],
                              raw["lat"], raw["lon"], line))
    return parsed, file_problems


def _integer(value: str) -> int | None:
    """Strict integer: signs and digits only; no floats, whitespace or 1e3."""
    if value is None or value == "":
        return None
    body = value[1:] if value[0] in "+-" else value
    if not body.isdigit():
        return None
    return int(value)


def _finite_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


# ---------------------------------------------------------------------------
# Per-run state machine
# ---------------------------------------------------------------------------

@dataclass
class _Pending:
    stop: dict[str, Any]
    first: Sample
    samples: list[Sample] = field(default_factory=list)


@dataclass
class _Confirmed:
    stop: dict[str, Any]
    arrival: Sample
    confirm: Sample
    samples: list[Sample]
    last_inside: Sample
    first_outside: Sample | None = None


@dataclass
class DwellFact:
    stop: dict[str, Any]
    arrival_sample: Sample
    confirm_sample: Sample
    last_observed_sample: Sample
    departure_sample: Sample | None          # first point outside exit_m
    end_reason: str
    samples: list[Sample]


def _close_confirmed(state: _Confirmed, reason: str) -> DwellFact:
    return DwellFact(
        stop=state.stop,
        arrival_sample=state.arrival,
        confirm_sample=state.confirm,
        last_observed_sample=state.last_inside,
        departure_sample=state.first_outside,
        end_reason=reason,
        samples=list(state.samples),
    )


def detect_dwells(samples: list[Sample], index: SpatialIndex,
                  params: "DwellParams") -> list[DwellFact]:
    """Run the explainable dwell state machine over one run's sorted samples.

    States: idle -> pending (a candidate is seen) -> confirmed (the same
    candidate has been seen continuously for min_dwell_s) -> closed.  Every
    adjacent pair with a gap larger than max_gap_s cuts the current process
    first; a gap or end-of-file closes a confirmed dwell without a departure
    time rather than inventing one.
    """
    dwells: list[DwellFact] = []
    pending: _Pending | None = None
    confirmed: _Confirmed | None = None
    previous: Sample | None = None

    def gap_from(prev: Sample | None, point: Sample) -> bool:
        return prev is not None and point.observed_utc - prev.observed_utc > params.max_gap_s

    for point in samples:
        # 1) A gap always cuts whatever was in progress before anything else.
        if gap_from(previous, point):
            if confirmed is not None:
                dwells.append(_close_confirmed(confirmed, END_GAP_TRUNCATED))
                confirmed = None
            pending = None

        if confirmed is not None:
            # 2) Locked station: only the exit radius matters now.  Another
            #    stop being closer does not change the station or the arrival.
            distance = haversine_m(point.lat, point.lon,
                                   confirmed.stop["stop_lat"], confirmed.stop["stop_lon"])
            if distance <= params.exit_m:
                confirmed.samples.append(point)
                confirmed.last_inside = point
            else:
                confirmed.first_outside = point
                dwells.append(_close_confirmed(confirmed, END_OBSERVED_EXIT))
                confirmed = None
                pending = None
                # This outside point may itself start a new candidate string,
                # but it never supports the dwell that just ended.
                picked = nearest_candidate(index, point.lat, point.lon, params.enter_m)
                if picked is not None:
                    stop, _dist = picked
                    pending = _Pending(stop=stop, first=point, samples=[point])
        elif pending is not None:
            picked = nearest_candidate(index, point.lat, point.lon, params.enter_m)
            if picked is not None and picked[0]["stop_id"] == pending.stop["stop_id"]:
                pending.samples.append(point)
                if point.observed_utc - pending.first.observed_utc >= params.min_dwell_s:
                    confirmed = _Confirmed(
                        stop=pending.stop, arrival=pending.first, confirm=point,
                        samples=list(pending.samples), last_inside=point)
                    pending = None
            else:
                # No candidate, or a different candidate: the old string is
                # discarded and the new point may start its own string.
                pending = None
                if picked is not None:
                    stop, _dist = picked
                    pending = _Pending(stop=stop, first=point, samples=[point])
        else:
            picked = nearest_candidate(index, point.lat, point.lon, params.enter_m)
            if picked is not None:
                stop, _dist = picked
                pending = _Pending(stop=stop, first=point, samples=[point])
        previous = point

    # 3) End of file is never an observed departure.
    if confirmed is not None:
        dwells.append(_close_confirmed(confirmed, END_OPEN_ENDED))
    # A still-pending string that never reached min_dwell_s outputs nothing.
    return dwells


# ---------------------------------------------------------------------------
# Parameters and persistence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DwellParams:
    enter_m: int
    exit_m: int
    min_dwell_s: int
    max_gap_s: int

    def validate(self) -> None:
        for name, value in (("enter_m", self.enter_m), ("exit_m", self.exit_m),
                            ("min_dwell_s", self.min_dwell_s),
                            ("max_gap_s", self.max_gap_s)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise GtfsError("%s must be a positive integer" % name)
        if not self.enter_m < self.exit_m:
            raise GtfsError("require 0 < enter_m < exit_m (got enter_m=%d exit_m=%d)"
                            % (self.enter_m, self.exit_m))


DWELL_DDL = """
CREATE TABLE IF NOT EXISTS dwell_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    binding_file TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    enter_m INTEGER NOT NULL,
    exit_m INTEGER NOT NULL,
    min_dwell_s INTEGER NOT NULL,
    max_gap_s INTEGER NOT NULL,
    total_runs INTEGER NOT NULL,
    processed_runs INTEGER NOT NULL,
    failed_runs INTEGER NOT NULL,
    dwell_facts INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dwell_bindings (
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    PRIMARY KEY (job_id, run_id)
);

CREATE TABLE IF NOT EXISTS dwell_job_runs (
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    feed_key TEXT,
    edition TEXT,
    trip_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('processed', 'failed')),
    sample_rows INTEGER NOT NULL,
    dwell_count INTEGER NOT NULL,
    index_json TEXT,
    diagnostics_json TEXT NOT NULL,
    PRIMARY KEY (job_id, run_id)
);

CREATE TABLE IF NOT EXISTS dwell_samples (
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    sample_id TEXT,
    observed_utc INTEGER,
    lat REAL,
    lon REAL,
    source_line INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (job_id, run_id, seq)
);

CREATE TABLE IF NOT EXISTS dwell_facts (
    job_id INTEGER NOT NULL,
    dwell_seq INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_name TEXT,
    arrival_utc INTEGER NOT NULL,
    confirmed_utc INTEGER NOT NULL,
    last_observed_utc INTEGER NOT NULL,
    departure_utc INTEGER,
    end_reason TEXT NOT NULL CHECK (end_reason IN
        ('observed_exit', 'gap_truncated', 'open_ended')),
    support_points INTEGER NOT NULL,
    first_sample_id TEXT NOT NULL,
    last_inside_sample_id TEXT NOT NULL,
    first_outside_sample_id TEXT,
    PRIMARY KEY (job_id, dwell_seq)
);

CREATE TABLE IF NOT EXISTS dwell_evidence (
    job_id INTEGER NOT NULL,
    dwell_seq INTEGER NOT NULL,
    evidence_seq INTEGER NOT NULL,
    sample_id TEXT NOT NULL,
    observed_utc INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    source_line INTEGER NOT NULL,
    distance_m REAL NOT NULL,
    point_role TEXT NOT NULL CHECK (point_role IN
        ('arrival', 'confirm', 'inside', 'last_inside', 'first_outside')),
    PRIMARY KEY (job_id, dwell_seq, evidence_seq)
);

CREATE INDEX IF NOT EXISTS idx_dwell_facts_run
    ON dwell_facts (job_id, run_id, arrival_utc);
CREATE INDEX IF NOT EXISTS idx_dwell_facts_stop
    ON dwell_facts (job_id, feed_key, edition, stop_id);
CREATE INDEX IF NOT EXISTS idx_dwell_samples_lookup
    ON dwell_samples (job_id, run_id, source_line);
"""


def _fault_after_runs() -> int | None:
    raw = os.environ.get("GTFS_FAULT_DWELL_AFTER_RUNS")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise GtfsError("GTFS_FAULT_DWELL_AFTER_RUNS must be an integer") from exc
    return value if value > 0 else None


class DwellStore:
    """Offline GPS dwell pipeline on top of an existing static Store."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn
        self.conn.executescript(DWELL_DDL)
        stored = self.conn.execute(
            "SELECT value FROM meta WHERE key='dwell_schema_version'").fetchone()
        if stored is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('dwell_schema_version', ?)",
                (DWELL_SCHEMA_VERSION,))
        elif stored[0] != DWELL_SCHEMA_VERSION:
            raise GtfsError(
                "dwell schema version %s is stored but this code expects %s"
                % (stored[0], DWELL_SCHEMA_VERSION))

    # ------------------------------------------------------------------
    # Static candidate resolution
    # ------------------------------------------------------------------

    def candidate_stops(self, feed_key: str, edition: str,
                        trip_id: str) -> list[dict[str, Any]]:
        """Distinct stops the trip actually visits, with coordinates and names.

        Repeated visits to one stop_id collapse to one spatial candidate.  The
        whole run is unprocessable if any candidate coordinate is missing,
        non-finite or out of range; the offending stop is named.
        """
        edition_row = self.conn.execute(
            "SELECT status FROM editions WHERE feed_key=? AND edition=?",
            (feed_key, edition)).fetchone()
        if edition_row is None:
            raise GtfsError("no edition %r for feed_key %r" % (edition, feed_key))
        if edition_row["status"] != "ready":
            raise GtfsError("edition %r of feed_key %r is %s and cannot supply stops"
                            % (edition, feed_key, edition_row["status"]))
        trip_row = self.conn.execute(
            "SELECT 1 FROM trips WHERE feed_key=? AND edition=? AND trip_id=?",
            (feed_key, edition, trip_id)).fetchone()
        if trip_row is None:
            raise GtfsError("trip_id=%r does not exist in %s/%s"
                            % (trip_id, feed_key, edition))
        rows = self.conn.execute(
            "SELECT DISTINCT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, "
            "       MIN(st.stop_sequence) AS first_sequence "
            "FROM stop_times st JOIN stops s "
            "  ON s.feed_key=st.feed_key AND s.edition=st.edition AND s.stop_id=st.stop_id "
            "WHERE st.feed_key=? AND st.edition=? AND st.trip_id=? "
            "GROUP BY s.stop_id, s.stop_name, s.stop_lat, s.stop_lon "
            "ORDER BY MIN(st.stop_sequence), s.stop_id",
            (feed_key, edition, trip_id)).fetchall()
        if not rows:
            raise GtfsError("trip_id=%r in %s/%s has no stop_times rows"
                            % (trip_id, feed_key, edition))
        stops: list[dict[str, Any]] = []
        for row in rows:
            lat, lon = row["stop_lat"], row["stop_lon"]
            if not finite_coord(lat, lon):
                raise GtfsError(
                    "candidate stop %r of trip %r (%s/%s) has missing, non-finite or "
                    "out-of-range coordinates (lat=%r, lon=%r); the run cannot be processed"
                    % (row["stop_id"], trip_id, feed_key, edition, lat, lon))
            stops.append({"stop_id": row["stop_id"], "stop_name": row["stop_name"],
                          "stop_lat": float(lat), "stop_lon": float(lon),
                          "first_sequence": row["first_sequence"]})
        return stops

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    def process(self, sample_path: str, binding_path: str,
                params: DwellParams, *,
                sample_bytes: bytes | None = None,
                binding_bytes: bytes | None = None) -> dict[str, Any]:
        """Run one complete, independent job and commit it atomically."""
        params.validate()
        if sample_bytes is None:
            sample_bytes = self._read_input(sample_path)
        if binding_bytes is None:
            binding_bytes = self._read_input(binding_path)
        sample_sha = hashlib.sha256(sample_bytes).hexdigest()
        binding_sha = hashlib.sha256(binding_bytes).hexdigest()

        bindings = parse_bindings(binding_bytes)
        raw_rows, file_problems = parse_samples(sample_bytes)
        if file_problems:
            details = "; ".join("line %d: %s" % (line, msg) for line, msg in file_problems)
            raise DwellFileError("sample file has unattributable row(s): " + details)

        # Group raw rows by run without losing source lines.
        by_run: dict[str, list[_RawRow]] = {}
        for row in raw_rows:
            by_run.setdefault(row.run_id, []).append(row)

        run_ids = sorted(set(by_run) | set(bindings))
        results = []
        for run_id in run_ids:
            results.append(self._process_run(run_id, by_run.get(run_id, []),
                                             bindings.get(run_id), params))

        total = len(results)
        failed = sum(1 for r in results if r["status"] == "failed")
        dwell_total = sum(len(r["dwells"]) for r in results)
        created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fault_after = _fault_after_runs()

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO dwell_jobs(source_file, source_sha256, binding_file, "
                "binding_sha256, enter_m, exit_m, min_dwell_s, max_gap_s, total_runs, "
                "processed_runs, failed_runs, dwell_facts, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (os.path.basename(str(sample_path)), sample_sha,
                 os.path.basename(str(binding_path)), binding_sha,
                 params.enter_m, params.exit_m, params.min_dwell_s, params.max_gap_s,
                 total, total - failed, failed, dwell_total, created))
            job_id = cur.lastrowid
            for run_id, binding in bindings.items():
                self.conn.execute(
                    "INSERT INTO dwell_bindings VALUES(?,?,?,?,?,?)",
                    (job_id, run_id, binding["feed_key"], binding["edition"],
                     binding["trip_id"], binding["source_line"]))
            written_runs = 0
            for result in results:
                self._write_run(job_id, result, params)
                written_runs += 1
                if fault_after is not None and written_runs >= fault_after:
                    raise GtfsError(
                        "fault injection: dwell job interrupted after %d run(s)"
                        % written_runs)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        return {
            "job_id": job_id,
            "source_file": os.path.basename(str(sample_path)),
            "source_sha256": sample_sha,
            "binding_sha256": binding_sha,
            "params": params.__dict__.copy(),
            "total_runs": total,
            "processed_runs": total - failed,
            "failed_runs": failed,
            "dwell_facts": dwell_total,
            "runs": results,
        }

    @staticmethod
    def _read_input(path) -> bytes:
        try:
            return open(path, "rb").read()
        except OSError as exc:
            raise DwellFileError("cannot read input file %s: %s" % (path, exc)) from exc

    def _process_run(self, run_id: str, rows: list[_RawRow],
                     binding: dict[str, str] | None,
                     params: DwellParams) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        if binding is None:
            diagnostics.append({
                "code": "unregistered_run", "source_line": None,
                "message": "run_id=%s has no binding row" % run_id})
        if not rows:
            diagnostics.append({
                "code": "no_samples", "source_line": None,
                "message": "run_id=%s is bound but has no sample rows" % run_id})

        samples: list[Sample] = []
        seen_ids: dict[str, int] = {}
        seen_times: dict[int, int] = {}
        for row in rows:
            if row.sample_id is None:
                diagnostics.append({
                    "code": "empty_sample_id", "source_line": row.source_line,
                    "message": "sample_id is empty"})
                continue
            if row.sample_id in seen_ids:
                diagnostics.append({
                    "code": "duplicate_sample_id", "source_line": row.source_line,
                    "message": "sample_id=%s repeats line %d"
                               % (row.sample_id, seen_ids[row.sample_id])})
                continue
            ts = _integer(row.observed_raw)
            if ts is None:
                diagnostics.append({
                    "code": "bad_timestamp", "source_line": row.source_line,
                    "message": "observed_utc=%r is not an integer UTC second"
                               % row.observed_raw})
                continue
            lat = _finite_float(row.lat_raw)
            lon = _finite_float(row.lon_raw)
            if lat is None or lon is None or not finite_coord(lat, lon):
                diagnostics.append({
                    "code": "bad_coordinate", "source_line": row.source_line,
                    "message": "lat=%r lon=%r is not a finite WGS84 coordinate in range"
                               % (row.lat_raw, row.lon_raw)})
                continue
            if ts in seen_times:
                diagnostics.append({
                    "code": "duplicate_timestamp", "source_line": row.source_line,
                    "message": "observed_utc=%d repeats line %d; input order must not "
                               "break the conflict" % (ts, seen_times[ts])})
                continue
            seen_ids[row.sample_id] = row.source_line
            seen_times[ts] = row.source_line
            samples.append(Sample(run_id, row.sample_id, ts, lat, lon, row.source_line))

        # Stop candidate problems make the whole run unprocessable; the bad
        # stop is named.  Resolve before sorting so a broken binding is still
        # reported alongside row-level problems.
        stops: list[dict[str, Any]] = []
        index_explain: dict[str, Any] | None = None
        if binding is not None and not diagnostics:
            try:
                stops = self.candidate_stops(binding["feed_key"], binding["edition"],
                                             binding["trip_id"])
            except GtfsError as exc:
                diagnostics.append({"code": "candidate_stops", "source_line":
                                    binding["source_line"], "message": str(exc)})

        if diagnostics:
            return {"run_id": run_id, "status": "failed",
                    "binding": binding, "rows": rows, "samples": samples,
                    "dwells": [], "index": None,
                    "diagnostics": diagnostics}

        # Restore chronological order; ties were already rejected above.
        samples.sort(key=lambda s: s.observed_utc)
        index = SpatialIndex(stops, params.enter_m)
        dwells = detect_dwells(samples, index, params)
        index_explain = index.explain()
        return {"run_id": run_id, "status": "processed", "binding": binding,
                "rows": rows, "samples": samples, "dwells": dwells,
                "index": index_explain, "diagnostics": []}

    def _write_run(self, job_id: int, result: dict[str, Any],
                   params: DwellParams) -> None:
        binding = result["binding"]
        feed_key = binding["feed_key"] if binding else None
        edition = binding["edition"] if binding else None
        trip_id = binding["trip_id"] if binding else None
        # Every imported raw row is kept, in source order, with its line.
        for seq, row in enumerate(result["rows"]):
            ts = _integer(row.observed_raw)
            lat = _finite_float(row.lat_raw)
            lon = _finite_float(row.lon_raw)
            ok_coord = lat is not None and lon is not None and finite_coord(lat, lon)
            self.conn.execute(
                "INSERT INTO dwell_samples VALUES(?,?,?,?,?,?,?,?)",
                (job_id, result["run_id"], row.sample_id, ts,
                 lat if ok_coord else None, lon if ok_coord else None,
                 row.source_line, seq))
        self.conn.execute(
            "INSERT INTO dwell_job_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, result["run_id"], feed_key, edition, trip_id, result["status"],
             len(result["rows"]), len(result["dwells"]),
             json.dumps(result["index"], ensure_ascii=False, sort_keys=True)
             if result["index"] else None,
             json.dumps(result["diagnostics"], ensure_ascii=False, sort_keys=True)))
        if result["status"] != "processed":
            return
        for dwell_seq, fact in enumerate(result["dwells"], start=1):
            global_seq = self._next_dwell_seq(job_id)
            self._write_fact(job_id, global_seq, result, fact, params)

    def _next_dwell_seq(self, job_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(dwell_seq), 0) + 1 AS next_seq FROM dwell_facts "
            "WHERE job_id=?", (job_id,)).fetchone()
        return row["next_seq"]

    def _write_fact(self, job_id: int, dwell_seq: int, result: dict[str, Any],
                    fact: DwellFact, params: DwellParams) -> None:
        binding = result["binding"]
        departure = fact.departure_sample
        confirm_sample = fact.confirm_sample
        self.conn.execute(
            "INSERT INTO dwell_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, dwell_seq, result["run_id"], binding["feed_key"],
             binding["edition"], binding["trip_id"], fact.stop["stop_id"],
             fact.stop["stop_name"], fact.arrival_sample.observed_utc,
             confirm_sample.observed_utc, fact.last_observed_sample.observed_utc,
             departure.observed_utc if departure else None, fact.end_reason,
             len(fact.samples), fact.arrival_sample.sample_id,
             fact.last_observed_sample.sample_id,
             departure.sample_id if departure else None))
        # Expand every supporting sample with its raw value, source line and
        # exact distance; the first outside point (if any) is evidence too but
        # is explicitly marked and never counted as a support point.  A sample
        # that holds several roles (arrival == confirm, for instance) gets one
        # evidence row per role, so every role is always queryable.
        evidence_seq = 0
        for sample in fact.samples:
            distance = haversine_m(sample.lat, sample.lon,
                                   fact.stop["stop_lat"], fact.stop["stop_lon"])
            roles: list[str] = []
            if sample is fact.arrival_sample:
                roles.append("arrival")
            if sample is confirm_sample:
                roles.append("confirm")
            if sample is fact.last_observed_sample:
                roles.append("last_inside")
            if not roles:
                roles.append("inside")
            for role in roles:
                evidence_seq += 1
                self.conn.execute(
                    "INSERT INTO dwell_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (job_id, dwell_seq, evidence_seq, sample.sample_id,
                     sample.observed_utc, sample.lat, sample.lon, sample.source_line,
                     distance, role))
        if departure is not None:
            evidence_seq += 1
            distance = haversine_m(departure.lat, departure.lon,
                                   fact.stop["stop_lat"], fact.stop["stop_lon"])
            self.conn.execute(
                "INSERT INTO dwell_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, dwell_seq, evidence_seq, departure.sample_id,
                 departure.observed_utc, departure.lat, departure.lon,
                 departure.source_line, distance, "first_outside"))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def jobs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM dwell_jobs ORDER BY job_id").fetchall()
        return [dict(r) for r in rows]

    def job(self, job_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM dwell_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise GtfsError("no dwell job %d" % job_id)
        result = dict(row)
        result["runs"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM dwell_job_runs WHERE job_id=? ORDER BY run_id", (job_id,))]
        result["bindings"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM dwell_bindings WHERE job_id=? ORDER BY run_id", (job_id,))]
        return result

    def run_detail(self, job_id: int, run_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM dwell_job_runs WHERE job_id=? AND run_id=?",
            (job_id, run_id)).fetchone()
        if row is None:
            raise GtfsError("job %d has no run %r" % (job_id, run_id))
        detail = dict(row)
        detail["diagnostics"] = json.loads(detail.pop("diagnostics_json"))
        detail["index"] = json.loads(detail.pop("index_json")) if detail["index_json"] else None
        detail["samples"] = [dict(r) for r in self.conn.execute(
            "SELECT sample_id, observed_utc, lat, lon, source_line, seq "
            "FROM dwell_samples WHERE job_id=? AND run_id=? ORDER BY seq",
            (job_id, run_id))]
        detail["dwells"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM dwell_facts WHERE job_id=? AND run_id=? ORDER BY dwell_seq",
            (job_id, run_id))]
        return detail

    def dwells(self, job_id: int, run_id: str | None = None,
               stop_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dwell_facts WHERE job_id=?"
        args: list[Any] = [job_id]
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        if stop_id is not None:
            sql += " AND stop_id=?"
            args.append(stop_id)
        sql += " ORDER BY dwell_seq"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def evidence(self, job_id: int, dwell_seq: int) -> dict[str, Any]:
        fact = self.conn.execute(
            "SELECT * FROM dwell_facts WHERE job_id=? AND dwell_seq=?",
            (job_id, dwell_seq)).fetchone()
        if fact is None:
            raise GtfsError("job %d has no dwell %d" % (job_id, dwell_seq))
        points = [dict(r) for r in self.conn.execute(
            "SELECT * FROM dwell_evidence WHERE job_id=? AND dwell_seq=? ORDER BY evidence_seq",
            (job_id, dwell_seq))]
        return {"fact": dict(fact), "evidence": points}
