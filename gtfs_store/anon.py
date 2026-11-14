"""Anonymized egress of GPS tracks as coarse spatiotemporal cell sequences.

The pipeline answers one question only: *which full, ordered, coarsened cell
sequences are supported by at least ``k`` distinct vehicle units among the
run instances the caller explicitly registered?*  It is whole-sequence
anonymity, not per-cell head counting: a sequence whose every cell is common
can still be unique as an ordered whole, and uniqueness is what the rare-group
rule protects against.

Data source contract
---------------------
Raw observations are read exclusively from the existing dwell GPS job tables
(``dwell_job_runs`` + ``dwell_samples``).  No location files are re-imported,
nothing is derived from dwell facts, and the static/realtime tables never
participate.  A request registers concrete run instances through a manifest
(``job_id,run_id,vehicle_unit``); every reference and every field must be
valid or the whole request is refused.  Nothing is silently skipped.

Grid contract (no boundary is ever learned from the data)
---------------------------------------------------------
For each level with cell width ``cell_microdeg`` and time width ``bucket_s``::

    lat_cell = floor((lat + 90)  * 1_000_000 / cell_microdeg)
    lon_cell = floor((lon + 180) * 1_000_000 / cell_microdeg)
    bucket   = floor(observed_utc / bucket_s)

Coordinates are taken from SQLite as REAL and rebuilt with
``Decimal(str(value))`` so float text, not binary noise, drives the floor.
Longitude +180 is treated identically to -180 (cell 0); latitude +90 lands
in the northernmost cell rather than one past it.  Time floors toward negative
infinity, so negative epochs bucket correctly.  Each run's samples are
time-sorted, mapped to ``(bucket, lat_cell, lon_cell)`` tuples and *adjacent*
equal tuples are folded; a tuple that reappears after a different one is kept,
and the order/length/repetition of the path are never normalized away.

Level promotion
---------------
Every level rebuilds each run's complete sequence from the raw samples; the
previous level's already-folded sequence is never reused as an input.  Runs
with byte-identical sequence text form one group.  A group's support is the
number of **distinct** ``vehicle_unit`` strings among its members (``"007"``
and ``"7"`` are different units; runs never count as vehicles).  If any group
has support below ``k`` the whole membership is promoted together to the next
level and regrouped.  At the last level, groups still rare are dropped as
whole groups; every run of a dropped group is excluded together.

Memory contract
---------------
No call ever materializes all samples, all full sequences or the vehicle set
of any group inside Python.  All sorting, grouping and distinct counting is
delegated to an on-disk scratch SQLite database; Python holds at most the
working state of one run (while its sequence is built) or one output group
(while it is written).  See ``docs/ANON_WALKTHROUGH.md`` and the module
constants below for the concrete bounds.

Atomicity
---------
Validation and scratch processing happen before the main database write
transaction opens.  The publish step writes the job, its level ledger, its
groups, memberships and the fixed-column public rows in one
``BEGIN IMMEDIATE`` transaction; a crash (including the injected
``GTFS_FAULT_ANON_AFTER_GROUPS`` hard exit) rolls everything back, so a reader
in a new process can never see a half group or a group missing its audit
membership.  A rerun gets a brand new independent job id; there is no cursor
to resume.

Threat model
------------
The only adversary is a partner holding the public export.  This pipeline
guarantees that each published whole sequence has at least ``k`` distinct
vehicle subjects supporting it *within this run*.  It is not differential
privacy, it adds no noise, and repeated or intersecting deliveries can be
cross-analysed; nothing here claims resistance to that.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import tempfile
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from .errors import GtfsError
from .model import read_csv_bytes

ANON_SCHEMA_VERSION = "1"

MANIFEST_COLUMNS = ("job_id", "run_id", "vehicle_unit")

# Fixed columns of the public export.  Public payloads are freshly built with
# exactly these keys; internal row objects are never handed out and trimmed.
PUBLIC_COLUMNS = ("public_group_no", "level_index", "cell_microdeg",
                  "bucket_s", "trip_count", "vehicle_count", "sequence")

# The full latitude/longitude span in microdegrees; a legal cell width must
# divide this exactly.
MICRODEG_SPAN = 180_000_000

# Scratch database caps.  The scratch file lives on disk and SQLite is told to
# keep its page cache tiny and to spill temporary sorts to file, so grouping a
# large delivery never grows Python memory or the SQLite cache with the input.
SCRATCH_CACHE_SIZE_PAGES = 2000          # ~2 MiB with the default 1 KiB pages
SCRATCH_PAGE_BYTES = 1024
# SQL inserts are batched at this many rows; the batch is the only transient
# Python list held while writing.
INSERT_BATCH = 500


class AnonError(GtfsError):
    """A request-level refusal: manifest or level parameters are unusable."""

    exit_code = 2


# ---------------------------------------------------------------------------
# Grid arithmetic
# ---------------------------------------------------------------------------

def lat_cell(lat: float, cell_microdeg: int) -> int:
    """Floor the latitude grid index, pinning the +90 pole to its own cell."""
    value = (Decimal(str(lat)) + Decimal(90)) * Decimal(1_000_000)
    index = int((value / Decimal(cell_microdeg)).to_integral_value(
        rounding=ROUND_FLOOR))
    northernmost = MICRODEG_SPAN // cell_microdeg - 1
    if index > northernmost:
        return northernmost
    return index


def lon_cell(lon: float, cell_microdeg: int) -> int:
    """Floor the longitude grid index; +180 is the same seam as -180."""
    if lon == 180.0:
        return 0
    value = (Decimal(str(lon)) + Decimal(180)) * Decimal(1_000_000)
    return int((value / Decimal(cell_microdeg)).to_integral_value(
        rounding=ROUND_FLOOR))


def time_bucket(observed_utc: int, bucket_s: int) -> int:
    """Floor toward negative infinity, so negative epochs bucket correctly."""
    return math.floor(observed_utc / bucket_s)


def validate_levels(levels: list[dict[str, int]]) -> list[dict[str, int]]:
    """Validate the fine-to-coarse level ladder described in the module docstring."""
    if not isinstance(levels, list) or not levels:
        raise AnonError("at least one grid level is required")
    normalized: list[dict[str, int]] = []
    for idx, level in enumerate(levels):
        if not isinstance(level, dict):
            raise AnonError("level %d must be an object with cell_microdeg and bucket_s"
                            % idx)
        cell = level.get("cell_microdeg")
        bucket = level.get("bucket_s")
        # bool is an int subclass; reject it explicitly along with non-ints.
        if not isinstance(cell, int) or isinstance(cell, bool) or cell <= 0:
            raise AnonError("level %d cell_microdeg must be a positive integer" % idx)
        if not isinstance(bucket, int) or isinstance(bucket, bool) or bucket <= 0:
            raise AnonError("level %d bucket_s must be a positive integer" % idx)
        if MICRODEG_SPAN % cell != 0:
            raise AnonError(
                "level %d cell_microdeg=%d must divide %d evenly"
                % (idx, cell, MICRODEG_SPAN))
        normalized.append({"level_index": idx, "cell_microdeg": cell,
                           "bucket_s": bucket})
    for prev, nxt in zip(normalized, normalized[1:]):
        if nxt["cell_microdeg"] % prev["cell_microdeg"] != 0:
            raise AnonError(
                "cell_microdeg must be non-decreasing by integer multiples: "
                "%d does not divide %d"
                % (prev["cell_microdeg"], nxt["cell_microdeg"]))
        if nxt["bucket_s"] % prev["bucket_s"] != 0:
            raise AnonError(
                "bucket_s must be non-decreasing by integer multiples: "
                "%d does not divide %d" % (prev["bucket_s"], nxt["bucket_s"]))
        if (nxt["cell_microdeg"] == prev["cell_microdeg"]
                and nxt["bucket_s"] == prev["bucket_s"]):
            raise AnonError(
                "each level must coarsen at least one dimension; level %d is "
                "identical to level %d" % (nxt["level_index"], prev["level_index"]))
    return normalized


def validate_k(k: Any) -> int:
    if not isinstance(k, int) or isinstance(k, bool) or k < 2:
        raise AnonError("k must be an integer >= 2")
    return k


# ---------------------------------------------------------------------------
# Manifest parsing and validation
# ---------------------------------------------------------------------------

def _strict_job_id(raw: str, line: int) -> int:
    if not raw or not raw.isdigit():
        raise AnonError(
            "manifest line %d: job_id=%r must be a non-negative integer written "
            "without a sign or leading zeros" % (line, raw))
    if len(raw) > 1 and raw[0] == "0":
        raise AnonError(
            "manifest line %d: job_id=%r must not have leading zeros" % (line, raw))
    return int(raw)


def parse_manifest(data: bytes) -> list[dict[str, Any]]:
    """Parse the member manifest; header and every field are part of the contract."""
    try:
        header, rows = read_csv_bytes(data)
    except GtfsError as exc:
        raise AnonError("manifest file cannot be parsed: %s" % exc) from exc
    if tuple(header) != MANIFEST_COLUMNS:
        raise AnonError(
            "manifest header must be exactly %s, got %s"
            % (",".join(MANIFEST_COLUMNS),
               ",".join(header) if header else "(empty)"))
    members: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for raw, line, _field_count in rows:
        job_raw, run_id, vehicle_unit = (raw["job_id"], raw["run_id"],
                                         raw["vehicle_unit"])
        if not job_raw or not run_id or not vehicle_unit:
            raise AnonError(
                "manifest line %d: job_id, run_id and vehicle_unit must all be "
                "non-empty (got job_id=%r run_id=%r vehicle_unit=%r)"
                % (line, job_raw, run_id, vehicle_unit))
        job_id = _strict_job_id(job_raw, line)
        key = (job_id, run_id)
        if key in seen:
            raise AnonError(
                "manifest line %d: instance job_id=%d run_id=%r is registered "
                "more than once; each instance may appear exactly once"
                % (line, job_id, run_id))
        seen.add(key)
        # run_id and vehicle_unit stay opaque strings: no trim, no zero
        # padding, no numeric coercion.  "007" and "7" stay distinct.
        members.append({"manifest_line": line, "job_id": job_id,
                        "run_id": run_id, "vehicle_unit": vehicle_unit})
    if not members:
        raise AnonError("manifest registers no run instances")
    return members


# ---------------------------------------------------------------------------
# On-disk scratch processing
# ---------------------------------------------------------------------------

_SCRATCH_DDL = """
CREATE TABLE member (
    member_seq INTEGER PRIMARY KEY,
    manifest_line INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    vehicle_unit TEXT NOT NULL
);

CREATE TABLE level_run (
    member_seq INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    vehicle_unit TEXT NOT NULL,
    seq_hash TEXT NOT NULL,
    seq_text TEXT NOT NULL
);

CREATE TABLE level_group (
    group_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    seq_hash TEXT NOT NULL,
    seq_text TEXT NOT NULL,
    run_count INTEGER NOT NULL,
    vehicle_count INTEGER NOT NULL
);
"""


SCRATCH_PREFIX = "anon-scratch-"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_stale_scratch() -> None:
    """Best-effort removal of scratch files left by a previous hard exit.

    Normal runs delete their scratch database in ``finally``; only an injected
    ``os._exit`` (or SIGKILL) can leave one behind, and it always lives in the
    process temp directory, never next to a deliverable.  Scratch file names
    carry the owning PID, so files belonging to live processes (concurrent
    deliveries) are never touched.
    """
    tmpdir = tempfile.gettempdir()
    for name in os.listdir(tmpdir):
        if not name.startswith(SCRATCH_PREFIX):
            continue
        marker = name[len(SCRATCH_PREFIX):].split("-", 1)[0]
        if marker.isdigit() and _pid_alive(int(marker)):
            continue
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(tmpdir, name))


class _Scratch:
    """File-backed scratch database; all heavy data stays on disk."""

    def __init__(self, scratch_path: Path) -> None:
        import sqlite3

        self.path = scratch_path
        self.conn = sqlite3.connect(str(scratch_path), isolation_level=None)
        # Tiny page cache plus file-based temp store: even a multi-hundred-
        # thousand-sample delivery cannot inflate the cache to fit the input.
        self.conn.execute("PRAGMA page_size=%d" % SCRATCH_PAGE_BYTES)
        self.conn.execute("PRAGMA cache_size=-%d"
                          % (SCRATCH_CACHE_SIZE_PAGES * SCRATCH_PAGE_BYTES // 1024))
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCRATCH_DDL)

    def open_reader(self) -> "sqlite3.Connection":
        """Second connection so an open group cursor and per-group queries coexist.

        Both connections point at the same on-disk scratch file; the second
        shares the same tiny page cache policy and never buffers the input in
        Python.  The caller closes it before the scratch file is removed.
        """
        import sqlite3

        reader = sqlite3.connect(str(self.path), isolation_level=None)
        reader.execute("PRAGMA cache_size=-%d"
                       % (SCRATCH_CACHE_SIZE_PAGES * SCRATCH_PAGE_BYTES // 1024))
        reader.execute("PRAGMA temp_store=FILE")
        return reader

    def close(self) -> None:
        self.conn.close()


class AnonStore:
    """Whole-sequence k-anonymity egress built on top of a dwell-enabled Store."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        self.conn.executescript(ANON_DDL)
        stored = self.conn.execute(
            "SELECT value FROM meta WHERE key='anon_schema_version'").fetchone()
        if stored is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('anon_schema_version', ?)",
                (ANON_SCHEMA_VERSION,))
        elif stored[0] != ANON_SCHEMA_VERSION:
            raise AnonError(
                "anon schema version %s is stored but this code expects %s"
                % (stored[0], ANON_SCHEMA_VERSION))

    # ------------------------------------------------------------------
    # Manifest validation against the existing GPS job tables
    # ------------------------------------------------------------------

    def _validate_members(self, members: list[dict[str, Any]]) -> None:
        """Refuse the whole request if any reference or field is unusable."""
        job_ids = sorted({m["job_id"] for m in members})
        placeholders = ",".join("?" for _ in job_ids)
        known_jobs = {
            row[0]: row[1] for row in self.conn.execute(
                "SELECT job_id, total_runs FROM dwell_jobs "
                "WHERE job_id IN (%s)" % placeholders, job_ids)
        }
        for member in members:
            line = member["manifest_line"]
            if member["job_id"] not in known_jobs:
                raise AnonError(
                    "manifest line %d: job_id=%d does not exist among the GPS "
                    "jobs; unselected jobs never participate"
                    % (line, member["job_id"]))
        # Per-instance existence/status queries against indexed primary keys:
        # validation stays bounded by the manifest size instead of pulling all
        # runs (including unselected ones) of a job into Python memory.
        for member in members:
            line = member["manifest_line"]
            row = self.conn.execute(
                "SELECT status, sample_rows FROM dwell_job_runs "
                "WHERE job_id=? AND run_id=?",
                (member["job_id"], member["run_id"])).fetchone()
            if row is None:
                raise AnonError(
                    "manifest line %d: run_id=%r is not part of GPS job %d"
                    % (line, member["run_id"], member["job_id"]))
            status, sample_rows = row
            if status != "processed":
                raise AnonError(
                    "manifest line %d: job_id=%d run_id=%r has status %r; only "
                    "processed runs are admissible and failed runs never "
                    "participate" % (line, member["job_id"], member["run_id"], status))
            if not sample_rows:
                raise AnonError(
                    "manifest line %d: job_id=%d run_id=%r is processed but has "
                    "no samples" % (line, member["job_id"], member["run_id"]))
            # The raw sample table is the only data source; coordinates and
            # times of the *selected* instances must be intact.  The check is
            # scoped to the manifest: failed runs of other jobs legitimately
            # keep NULL rows in dwell_samples and must not block this request,
            # because unselected jobs never participate.
            bad = self.conn.execute(
                "SELECT COUNT(*) FROM dwell_samples "
                "WHERE job_id=? AND run_id=? AND ("
                "observed_utc IS NULL OR lat IS NULL OR lon IS NULL)",
                (member["job_id"], member["run_id"])).fetchone()[0]
            if bad:
                raise AnonError(
                    "manifest line %d: job_id=%d run_id=%r has %d raw sample "
                    "row(s) with missing timestamp or coordinates; the "
                    "anonymization pipeline only reads intact raw samples"
                    % (line, member["job_id"], member["run_id"], bad))

    # ------------------------------------------------------------------
    # Sequence building (one run in memory at a time)
    # ------------------------------------------------------------------

    def _build_run_sequence(self, scratch: _Scratch, member: dict[str, Any],
                            level: dict[str, int]) -> tuple[str, str]:
        """Stream one run's raw samples into a folded cell-sequence text.

        Only this one run's working state is held: a rolling ``last_triple``
        and one ``StringIO`` buffer for its sequence; nothing of sibling runs
        is resident.  The buffer is the single allowed per-trip workspace and
        is handed to SQLite as soon as the run finishes.
        """
        cell = level["cell_microdeg"]
        bucket = level["bucket_s"]
        buffer = io.StringIO()
        buffer.write("[")
        last_triple: tuple[int, int, int] | None = None
        length = 0
        cursor = self.conn.execute(
            "SELECT observed_utc, lat, lon FROM dwell_samples "
            "WHERE job_id=? AND run_id=? ORDER BY observed_utc, seq",
            (member["job_id"], member["run_id"]))
        for observed_utc, lat, lon in cursor:
            triple = (time_bucket(int(observed_utc), bucket),
                      lat_cell(float(lat), cell),
                      lon_cell(float(lon), cell))
            if triple != last_triple:
                if length:
                    buffer.write(",")
                buffer.write("[%d,%d,%d]" % triple)
                length += 1
                last_triple = triple
        if length == 0:
            # Manifest validation already required sample_rows > 0; keep the
            # guard explicit so an empty sequence can never be published.
            raise AnonError(
                "job_id=%d run_id=%r produced an empty sequence"
                % (member["job_id"], member["run_id"]))
        buffer.write("]")
        seq_text = buffer.getvalue()
        buffer.close()
        seq_hash = hashlib.sha256(seq_text.encode("utf-8")).hexdigest()
        return seq_hash, seq_text

    def _process_level(self, scratch: _Scratch, members: list[dict[str, Any]],
                       level: dict[str, int]) -> dict[str, Any]:
        """Rebuild every sequence from raw samples and regroup on disk."""
        scratch.conn.execute("DROP TABLE IF EXISTS level_run")
        scratch.conn.execute("DROP TABLE IF EXISTS level_group")
        scratch.conn.execute(
            "CREATE TABLE level_run ("
            "member_seq INTEGER NOT NULL, job_id INTEGER NOT NULL, run_id TEXT NOT NULL, "
            "vehicle_unit TEXT NOT NULL, seq_hash TEXT NOT NULL, seq_text TEXT NOT NULL)")
        run_batch: list[tuple[Any, ...]] = []
        for member_seq, member in enumerate(members):
            seq_hash, seq_text = self._build_run_sequence(scratch, member, level)
            run_batch.append((member_seq, member["job_id"], member["run_id"],
                              member["vehicle_unit"], seq_hash, seq_text))
            if len(run_batch) >= INSERT_BATCH:
                scratch.conn.executemany(
                    "INSERT INTO level_run VALUES(?,?,?,?,?,?)", run_batch)
                run_batch.clear()
        if run_batch:
            scratch.conn.executemany(
                "INSERT INTO level_run VALUES(?,?,?,?,?,?)", run_batch)
            run_batch.clear()
        scratch.conn.execute(
            "CREATE INDEX idx_level_run_hash ON level_run(seq_hash)")
        # Exact grouping by the full sequence text; hashes are only a key, and
        # a defensive guard proves no two texts share one hash.
        scratch.conn.execute(
            "CREATE TABLE level_group AS "
            "SELECT seq_hash, seq_text, COUNT(*) AS run_count, "
            "       COUNT(DISTINCT vehicle_unit) AS vehicle_count "
            "FROM level_run GROUP BY seq_hash, seq_text")
        collision = scratch.conn.execute(
            "SELECT COUNT(*) FROM (SELECT seq_hash FROM level_group "
            "GROUP BY seq_hash HAVING COUNT(DISTINCT seq_text) > 1)").fetchone()[0]
        if collision:
            raise AnonError("internal error: sequence hash collision; refusing to merge")
        stats = self._level_stats(scratch, level)
        return stats

    def _level_stats(self, scratch: _Scratch,
                     level: dict[str, int]) -> dict[str, Any]:
        total_groups = scratch.conn.execute(
            "SELECT COUNT(*) FROM level_group").fetchone()[0]
        rare = scratch.conn.execute(
            "SELECT COUNT(*) FROM level_group WHERE vehicle_count < ?",
            (self._k,)).fetchone()[0]
        retained_runs = scratch.conn.execute(
            "SELECT COUNT(*) FROM level_run WHERE seq_hash IN "
            "(SELECT seq_hash FROM level_group WHERE vehicle_count >= ?)",
            (self._k,)).fetchone()[0]
        rare_runs = scratch.conn.execute(
            "SELECT COUNT(*) FROM level_run WHERE seq_hash IN "
            "(SELECT seq_hash FROM level_group WHERE vehicle_count < ?)",
            (self._k,)).fetchone()[0]
        retained_vehicles = scratch.conn.execute(
            "SELECT COUNT(DISTINCT vehicle_unit) FROM level_run WHERE seq_hash IN "
            "(SELECT seq_hash FROM level_group WHERE vehicle_count >= ?)",
            (self._k,)).fetchone()[0]
        rare_vehicles = scratch.conn.execute(
            "SELECT COUNT(DISTINCT vehicle_unit) FROM level_run WHERE seq_hash IN "
            "(SELECT seq_hash FROM level_group WHERE vehicle_count < ?)",
            (self._k,)).fetchone()[0]
        return {
            "level_index": level["level_index"],
            "cell_microdeg": level["cell_microdeg"],
            "bucket_s": level["bucket_s"],
            "total_groups": total_groups,
            "rare_groups": rare,
            "retained_trips": retained_runs,
            "dropped_trips": rare_runs,
            "retained_vehicles": retained_vehicles,
            "rare_vehicles": rare_vehicles,
            "total_trips": retained_runs + rare_runs,
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process(self, manifest_bytes: bytes, k: Any,
                raw_levels: list[dict[str, int]]) -> dict[str, Any]:
        """Validate, coarsen, regroup by level and publish one anon job atomically."""
        k_value = validate_k(k)
        levels = validate_levels(raw_levels)
        members = parse_manifest(manifest_bytes)
        # All references must resolve before any scratch work or write begins.
        self._validate_members(members)

        self._k = k_value
        _cleanup_stale_scratch()
        # The scratch database lives in the process temp directory, never next
        # to the main database: a hard process exit (os._exit fault injection)
        # therefore cannot leave an externally readable scratch file beside
        # the deliverable, and the OS temp area is cleaned on reboot anyway.
        fd, scratch_name = tempfile.mkstemp(
            prefix="%s%d-" % (SCRATCH_PREFIX, os.getpid()), suffix=".db")
        # mkstemp keeps the handle open; close and remove it so SQLite creates
        # and owns a fresh scratch file.
        os.close(fd)
        scratch_path = Path(scratch_name)
        scratch_path.unlink()
        scratch = _Scratch(scratch_path)
        try:
            scratch.conn.executemany(
                "INSERT INTO member(member_seq, manifest_line, job_id, run_id, "
                "vehicle_unit) VALUES(?,?,?,?,?)",
                [(idx, m["manifest_line"], m["job_id"], m["run_id"],
                  m["vehicle_unit"]) for idx, m in enumerate(members)])
            level_ledgers: list[dict[str, Any]] = []
            selected: dict[str, Any] | None = None
            for level in levels:
                stats = self._process_level(scratch, members, level)
                if stats["rare_groups"] == 0:
                    stats["outcome"] = "selected"
                    level_ledgers.append(stats)
                    selected = stats
                    break
                if level["level_index"] == levels[-1]["level_index"]:
                    stats["outcome"] = "final_rare_groups_dropped"
                    level_ledgers.append(stats)
                    selected = stats
                    break
                stats["outcome"] = "promoted"
                level_ledgers.append(stats)
            assert selected is not None
            job_id = self._publish(scratch, members, levels, k_value,
                                   selected, level_ledgers,
                                   hashlib.sha256(manifest_bytes).hexdigest())
        finally:
            scratch.close()
            # The scratch database and any journal it left behind are private
            # working files and must never remain readable after the run.
            for suffix in ("", "-journal", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    scratch_path.with_name(scratch_path.name + suffix).unlink()

        return self.job(job_id)

    # ------------------------------------------------------------------
    # Atomic publish into the main database
    # ------------------------------------------------------------------

    def _fault_after_groups(self) -> int | None:
        raw = os.environ.get("GTFS_FAULT_ANON_AFTER_GROUPS")
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise AnonError("GTFS_FAULT_ANON_AFTER_GROUPS must be an integer") from exc
        return value if value > 0 else None

    def _publish(self, scratch: _Scratch, members: list[dict[str, Any]],
                 levels: list[dict[str, int]], k_value: int,
                 selected: dict[str, Any],
                 level_ledgers: list[dict[str, Any]],
                 manifest_sha256: str) -> int:
        fault_after = self._fault_after_groups()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO anon_jobs(k, selected_level_index, "
                "cell_microdeg, bucket_s, member_count, published_groups, "
                "published_trips, dropped_trips, distinct_vehicles, "
                "final_outcome, manifest_sha256, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (k_value, selected["level_index"], selected["cell_microdeg"],
                 selected["bucket_s"], len(members),
                 selected["total_groups"] - selected["rare_groups"],
                 selected["retained_trips"], selected["dropped_trips"],
                 selected["retained_vehicles"], selected["outcome"],
                 manifest_sha256,
                 _utc_now()))
            job_id = cur.lastrowid

            for stats in level_ledgers:
                self.conn.execute(
                    "INSERT INTO anon_levels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, stats["level_index"], stats["cell_microdeg"],
                     stats["bucket_s"], stats["total_groups"], stats["rare_groups"],
                     stats["retained_trips"], stats["dropped_trips"],
                     stats["retained_vehicles"], stats["rare_vehicles"],
                     stats["total_trips"], stats["outcome"]))

            # Deterministic internal ordering; public numbers are assigned only
            # to retained groups in sequence order.  The groups are streamed
            # from a second scratch connection in bounded chunks: the main
            # connection may hold an open cursor while per-group member
            # queries run on this one, and Python never holds all groups.
            reader = scratch.open_reader()
            try:
                internal_no = 0
                public_no = 0
                written_groups = 0
                group_batch: list[tuple[Any, ...]] = []
                seq_batch: list[tuple[Any, ...]] = []
                member_batch: list[tuple[Any, ...]] = []
                public_batch: list[tuple[Any, ...]] = []
                while True:
                    chunk = reader.execute(
                        "SELECT g.seq_hash, g.seq_text, g.run_count, "
                        "g.vehicle_count FROM level_group g "
                        "ORDER BY g.seq_text, g.seq_hash LIMIT ? OFFSET ?",
                        (INSERT_BATCH, internal_no)).fetchall()
                    if not chunk:
                        break
                    for seq_hash, seq_text, run_count, vehicle_count in chunk:
                        internal_no += 1
                        retained = vehicle_count >= k_value
                        if retained:
                            public_no += 1
                            group_no = public_no
                        else:
                            group_no = None
                        drop_reason = None if retained else (
                            "rare_group_at_level_%d: distinct vehicle support "
                            "%d < k=%d" % (selected["level_index"],
                                           vehicle_count, k_value))
                        group_batch.append(
                            (job_id, internal_no, group_no,
                             selected["level_index"], seq_hash, run_count,
                             vehicle_count,
                             "retained" if retained else "dropped",
                             drop_reason))
                        # One group's sequence at a time is parsed, then its
                        # rows leave Python through the bounded batch.
                        for ordinal, triple in enumerate(json.loads(seq_text),
                                                         start=1):
                            seq_batch.append(
                                (job_id, internal_no, ordinal, triple[0],
                                 triple[1], triple[2]))
                            if len(seq_batch) >= INSERT_BATCH:
                                self._insert_batch(
                                    "anon_group_sequences", 6, seq_batch)
                        # One group's membership at a time; the distinct
                        # vehicle counting itself never happens in Python.
                        for m_row in reader.execute(
                                "SELECT m.member_seq, m.manifest_line, "
                                "m.job_id, m.run_id, m.vehicle_unit "
                                "FROM level_run l JOIN member m "
                                "ON m.member_seq = l.member_seq "
                                "WHERE l.seq_hash=? "
                                "ORDER BY m.job_id, m.run_id, m.member_seq",
                                (seq_hash,)):
                            member_batch.append(
                                (job_id, internal_no, m_row[0], m_row[1],
                                 m_row[2], m_row[3], m_row[4],
                                 "retained" if retained else "dropped",
                                 drop_reason))
                            if len(member_batch) >= INSERT_BATCH:
                                self._insert_batch(
                                    "anon_members", 9, member_batch)
                        if retained:
                            public_batch.append(
                                (job_id, public_no, selected["level_index"],
                                 selected["cell_microdeg"],
                                 selected["bucket_s"], run_count,
                                 vehicle_count, seq_text))
                        written_groups += 1
                        if len(group_batch) >= INSERT_BATCH:
                            self._insert_batch("anon_groups", 9, group_batch)
                        if len(public_batch) >= INSERT_BATCH:
                            self._insert_batch("anon_public", 8, public_batch)
                        if fault_after is not None and written_groups >= fault_after:
                            # A real process death: no commit, no cleanup, no
                            # exception handling.  WAL recovery on reopen rolls
                            # the whole transaction back, so no half-written
                            # job is ever externally visible.
                            self.conn.execute("ROLLBACK")
                            os._exit(91)
                self._insert_batch("anon_groups", 9, group_batch)
                self._insert_batch("anon_group_sequences", 6, seq_batch)
                self._insert_batch("anon_members", 9, member_batch)
                self._insert_batch("anon_public", 8, public_batch)
            finally:
                reader.close()
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return job_id

    def _insert_batch(self, table: str, columns: int,
                      batch: list[tuple[Any, ...]]) -> None:
        """Flush one bounded insert batch into the open write transaction."""
        if not batch:
            return
        placeholders = ",".join("?" * columns)
        self.conn.executemany(
            "INSERT INTO %s VALUES(%s)" % (table, placeholders), batch)
        batch.clear()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def jobs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM anon_jobs ORDER BY anon_job_id").fetchall()
        return [dict(r) for r in rows]

    def job(self, job_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM anon_jobs WHERE anon_job_id=?", (job_id,)).fetchone()
        if row is None:
            raise AnonError("no anonymization job %d" % job_id)
        result = dict(row)
        result["levels"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM anon_levels WHERE anon_job_id=? ORDER BY level_index",
            (job_id,))]
        result["groups"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM anon_groups WHERE anon_job_id=? ORDER BY internal_group_no",
            (job_id,))]
        return result

    def public_export(self, job_id: int) -> dict[str, Any]:
        """The only external reader; fixed columns, freshly built payloads."""
        header = self.conn.execute(
            "SELECT k, selected_level_index FROM anon_jobs WHERE anon_job_id=?",
            (job_id,)).fetchone()
        if header is None:
            raise AnonError("no anonymization job %d" % job_id)
        rows: list[dict[str, Any]] = []
        for r in self.conn.execute(
                "SELECT public_group_no, level_index, cell_microdeg, bucket_s, "
                "trip_count, vehicle_count, sequence_text "
                "FROM anon_public WHERE anon_job_id=? ORDER BY public_group_no",
                (job_id,)):
            rows.append({
                "public_group_no": r["public_group_no"],
                "level_index": r["level_index"],
                "cell_microdeg": r["cell_microdeg"],
                "bucket_s": r["bucket_s"],
                "trip_count": r["trip_count"],
                "vehicle_count": r["vehicle_count"],
                # Parsed into plain nested int lists; never an internal object.
                "sequence": json.loads(r["sequence_text"]),
            })
        return {"anon_job_id": job_id, "k": header["k"],
                "selected_level_index": header["selected_level_index"],
                "columns": list(PUBLIC_COLUMNS), "groups": rows}

    def group_detail(self, job_id: int, public_group_no: int) -> dict[str, Any]:
        """Internal audit view: expand one retained group to members and raw rows."""
        group = self.conn.execute(
            "SELECT * FROM anon_groups WHERE anon_job_id=? AND public_group_no=?",
            (job_id, public_group_no)).fetchone()
        if group is None:
            raise AnonError(
                "anonymization job %d has no retained public group %d"
                % (job_id, public_group_no))
        internal_no = group["internal_group_no"]
        sequence = [list(r) for r in self.conn.execute(
            "SELECT bucket, lat_cell, lon_cell FROM anon_group_sequences "
            "WHERE anon_job_id=? AND internal_group_no=? ORDER BY ordinal",
            (job_id, internal_no))]
        members: list[dict[str, Any]] = []
        for m in self.conn.execute(
                "SELECT * FROM anon_members WHERE anon_job_id=? AND internal_group_no=? "
                "ORDER BY member_seq", (job_id, internal_no)):
            item = dict(m)
            item["raw_samples"] = [dict(r) for r in self.conn.execute(
                "SELECT sample_id, observed_utc, lat, lon, source_line, seq "
                "FROM dwell_samples WHERE job_id=? AND run_id=? "
                "ORDER BY observed_utc, seq",
                (m["job_id"], m["run_id"]))]
            members.append(item)
        return {"job_id": job_id, "public_group_no": public_group_no,
                "internal_group_no": internal_no,
                "group": dict(group), "sequence": sequence, "members": members}

    def member_outcome(self, job_id: int, member_job_id: int,
                       run_id: str) -> dict[str, Any]:
        """Internal audit view: why a registered member was retained or dropped."""
        member = self.conn.execute(
            "SELECT * FROM anon_members WHERE anon_job_id=? AND job_id=? AND run_id=?",
            (job_id, member_job_id, run_id)).fetchone()
        if member is None:
            raise AnonError(
                "anonymization job %d has no member (job_id=%d run_id=%r)"
                % (job_id, member_job_id, run_id))
        level_ledger = [dict(r) for r in self.conn.execute(
            "SELECT * FROM anon_levels WHERE anon_job_id=? ORDER BY level_index",
            (job_id,))]
        result = dict(member)
        result["level_ledger"] = level_ledger
        group = self.conn.execute(
            "SELECT * FROM anon_groups WHERE anon_job_id=? AND internal_group_no=?",
            (job_id, member["internal_group_no"])).fetchone()
        if member["status"] == "dropped":
            result["explanation"] = member["drop_reason"]
        else:
            header = self.conn.execute(
                "SELECT k FROM anon_jobs WHERE anon_job_id=?", (job_id,)).fetchone()
            result["explanation"] = (
                "retained at level %d in public group %d; its whole coarse "
                "sequence has %d distinct vehicle units (k=%d)"
                % (group["level_index"], group["public_group_no"],
                   group["vehicle_count"], header["k"]))
        return result


def _utc_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Main database DDL
# ---------------------------------------------------------------------------

ANON_DDL = """
CREATE TABLE IF NOT EXISTS anon_jobs (
    anon_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    k INTEGER NOT NULL CHECK (k >= 2),
    selected_level_index INTEGER NOT NULL,
    cell_microdeg INTEGER NOT NULL,
    bucket_s INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    published_groups INTEGER NOT NULL,
    published_trips INTEGER NOT NULL,
    dropped_trips INTEGER NOT NULL,
    distinct_vehicles INTEGER NOT NULL,
    final_outcome TEXT NOT NULL CHECK (final_outcome IN
        ('selected', 'final_rare_groups_dropped')),
    manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anon_levels (
    anon_job_id INTEGER NOT NULL,
    level_index INTEGER NOT NULL,
    cell_microdeg INTEGER NOT NULL,
    bucket_s INTEGER NOT NULL,
    total_groups INTEGER NOT NULL,
    rare_groups INTEGER NOT NULL,
    retained_trips INTEGER NOT NULL,
    dropped_trips INTEGER NOT NULL,
    retained_vehicles INTEGER NOT NULL,
    rare_vehicles INTEGER NOT NULL,
    total_trips INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN
        ('promoted', 'selected', 'final_rare_groups_dropped')),
    PRIMARY KEY (anon_job_id, level_index)
);

CREATE TABLE IF NOT EXISTS anon_groups (
    anon_job_id INTEGER NOT NULL,
    internal_group_no INTEGER NOT NULL,
    public_group_no INTEGER,
    level_index INTEGER NOT NULL,
    seq_hash TEXT NOT NULL,
    trip_count INTEGER NOT NULL,
    vehicle_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('retained', 'dropped')),
    drop_reason TEXT,
    PRIMARY KEY (anon_job_id, internal_group_no)
);

CREATE TABLE IF NOT EXISTS anon_group_sequences (
    anon_job_id INTEGER NOT NULL,
    internal_group_no INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    bucket INTEGER NOT NULL,
    lat_cell INTEGER NOT NULL,
    lon_cell INTEGER NOT NULL,
    PRIMARY KEY (anon_job_id, internal_group_no, ordinal)
);

CREATE TABLE IF NOT EXISTS anon_members (
    anon_job_id INTEGER NOT NULL,
    internal_group_no INTEGER NOT NULL,
    member_seq INTEGER NOT NULL,
    manifest_line INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    vehicle_unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('retained', 'dropped')),
    drop_reason TEXT,
    PRIMARY KEY (anon_job_id, member_seq)
);

CREATE INDEX IF NOT EXISTS idx_anon_members_instance
    ON anon_members (anon_job_id, job_id, run_id);

-- Fixed-column public export.  No run_id, vehicle_unit, job_id, sample_id,
-- exact coordinate, raw epoch second or source file is ever stored here.
CREATE TABLE IF NOT EXISTS anon_public (
    anon_job_id INTEGER NOT NULL,
    public_group_no INTEGER NOT NULL,
    level_index INTEGER NOT NULL,
    cell_microdeg INTEGER NOT NULL,
    bucket_s INTEGER NOT NULL,
    trip_count INTEGER NOT NULL,
    vehicle_count INTEGER NOT NULL,
    sequence_text TEXT NOT NULL,
    PRIMARY KEY (anon_job_id, public_group_no)
);
"""
