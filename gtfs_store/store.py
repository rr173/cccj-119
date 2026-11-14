"""SQLite persistence: staging -> canonical base tables, atomic expansion."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import archive
from .errors import GtfsError
from .model import (
    day_anchor_utc,
    format_date,
    iso_local,
    iso_utc,
    local_at,
    parse_iso_date,
    utc_at_anchor,
    utc_offset_text,
)
from .validate import WEEKDAYS, Staging, parse_package

SCHEMA_VERSION = "1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _boardable(template: list[dict]) -> list[dict]:
    """Stops where passengers may actually board.

    pickup_type=1 means "no pickup"; such a call still defines the template's
    timing offsets but is not a departure a rider can use.
    """
    return [s for s in template if s.get("pickup_type", "0") != "1"]


DDL = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    feed_key TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS editions (
    feed_key TEXT NOT NULL REFERENCES feeds(feed_key),
    edition TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'rejected')),
    timezone TEXT,
    agency_name TEXT,
    loaded_at TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition)
);

CREATE TABLE IF NOT EXISTS load_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    seq INTEGER NOT NULL,
    category TEXT NOT NULL,
    file_name TEXT,
    line_number INTEGER,
    record_id TEXT,
    field TEXT,
    message TEXT NOT NULL,
    context TEXT
);

CREATE TABLE IF NOT EXISTS trip_issues (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    file_name TEXT,
    line_number INTEGER
);

CREATE TABLE IF NOT EXISTS agency (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    agency_seq INTEGER NOT NULL,
    agency_id TEXT,
    agency_name TEXT,
    agency_url TEXT,
    agency_timezone TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, agency_seq)
);

CREATE TABLE IF NOT EXISTS stops (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_name TEXT,
    stop_lat REAL,
    stop_lon REAL,
    location_type TEXT,
    parent_station TEXT,
    PRIMARY KEY (feed_key, edition, stop_id)
);

CREATE TABLE IF NOT EXISTS routes (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    route_id TEXT NOT NULL,
    agency_id TEXT,
    route_short_name TEXT,
    route_long_name TEXT,
    route_type TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, route_id)
);

CREATE TABLE IF NOT EXISTS trips (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    trip_headsign TEXT,
    PRIMARY KEY (feed_key, edition, trip_id)
);

CREATE TABLE IF NOT EXISTS stop_times (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_seconds INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL,
    pickup_type TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (feed_key, edition, trip_id, stop_sequence)
);

CREATE TABLE IF NOT EXISTS calendar (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    service_id TEXT NOT NULL,
    monday INTEGER NOT NULL,
    tuesday INTEGER NOT NULL,
    wednesday INTEGER NOT NULL,
    thursday INTEGER NOT NULL,
    friday INTEGER NOT NULL,
    saturday INTEGER NOT NULL,
    sunday INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, service_id)
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    service_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exception_type INTEGER NOT NULL CHECK (exception_type IN (1, 2)),
    PRIMARY KEY (feed_key, edition, service_id, date)
);

CREATE TABLE IF NOT EXISTS frequencies (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    freq_id INTEGER NOT NULL,
    trip_id TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    headway_secs INTEGER NOT NULL,
    exact_times INTEGER NOT NULL CHECK (exact_times IN (0, 1)),
    PRIMARY KEY (feed_key, edition, freq_id)
);

-- One row per (feed, edition, service_date) expansion result set.  The
-- idempotency hash is of the range plus base data generation.
CREATE TABLE IF NOT EXISTS expansions (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    service_date TEXT NOT NULL,
    request_key TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, service_date)
);

CREATE TABLE IF NOT EXISTS departures (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    service_date TEXT NOT NULL,
    trip_instance_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    frequency_row INTEGER,
    frequency_offset INTEGER,
    template_start_seconds INTEGER NOT NULL,
    template_first_departure INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL,
    departure_utc TEXT NOT NULL,
    departure_local TEXT NOT NULL,
    utc_offset TEXT NOT NULL,
    certainty TEXT NOT NULL CHECK (certainty IN ('exact', 'headway')),
    min_departure_seconds INTEGER,
    max_departure_seconds INTEGER,
    headway_secs INTEGER,
    PRIMARY KEY (feed_key, edition, service_date, trip_instance_id, stop_sequence)
);

CREATE TABLE IF NOT EXISTS service_days (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    service_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    day_start_utc TEXT NOT NULL,
    ranges_spec TEXT NOT NULL,
    enabled_services_json TEXT NOT NULL,
    trip_instances INTEGER NOT NULL,
    departure_rows INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, service_date)
);

CREATE INDEX IF NOT EXISTS idx_departures_stop
    ON departures (feed_key, edition, service_date, stop_id, departure_seconds);
"""

# Foreign keys are enforced by the validator with richer problem reporting;
# the canonical tables additionally declare logical keys in the DDL above.
BASE_TABLES = ("agency", "stops", "routes", "trips", "stop_times", "calendar",
               "calendar_dates", "frequencies")


class _InjectedFailure(GtfsError):
    """Test hook: simulate a crash in the middle of the replacing transaction."""

    exit_code = 3


def _fault_after_days() -> int | None:
    raw = os.environ.get("GTFS_FAULT_EXPAND_AFTER_DAYS")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise GtfsError("GTFS_FAULT_EXPAND_AFTER_DAYS must be an integer") from None
    return value if value > 0 else None


class Store:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(DDL)
        stored = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if stored is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                              (SCHEMA_VERSION,))
        elif stored[0] != SCHEMA_VERSION:
            raise GtfsError(
                "database %s was built with schema version %s, this code expects %s; "
                "start a new database file" % (self.db_path, stored[0], SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _content_sha(self, contents) -> str:
        h = hashlib.sha256()
        for name in sorted(contents.members):
            h.update(name.encode("utf-8"))
            h.update(b"\x00")
            h.update(hashlib.sha256(contents.members[name]).digest())
        return h.hexdigest()

    def load(self, feed_key: str, edition: str, package_path: str | os.PathLike[str],
             raw_bytes: bytes | None = None) -> dict[str, Any]:
        """Validate a package and, only if fully valid, install it atomically.

        Returns a summary dict.  A rejected package records its problems and
        leaves any previously ready edition (and all expansions) untouched.
        """
        if not feed_key:
            raise GtfsError("feed_key must not be empty")
        if not edition:
            raise GtfsError("edition must not be empty")
        if raw_bytes is None:
            raw_bytes = Path(package_path).read_bytes()
        contents = archive.read_package(package_path, raw_bytes)
        archive_sha = _sha256(raw_bytes)
        content_sha = self._content_sha(contents)

        existing = self.conn.execute(
            "SELECT archive_sha256, content_sha256, status FROM editions "
            "WHERE feed_key=? AND edition=?", (feed_key, edition)).fetchone()
        if existing is not None:
            if existing["content_sha256"] == content_sha:
                result = {
                    "feed_key": feed_key,
                    "edition": edition,
                    "status": existing["status"],
                    "archive_sha256": existing["archive_sha256"],
                    "content_sha256": content_sha,
                    "resend": True,
                    "message": "identical package contents already stored; "
                               "no base tables changed",
                }
                if existing["status"] == "rejected":
                    result["problems"] = self._stored_problems(feed_key, edition, content_sha)
                return result
            raise GtfsError(
                "edition %r already exists for feed_key %r with different contents "
                "(stored %s, incoming %s); choose a new edition"
                % (edition, feed_key, existing["content_sha256"], content_sha))

        staging = parse_package(feed_key, edition, content_sha, contents)
        if not staging.ok:
            self._record_rejection(feed_key, edition, archive_sha, content_sha, staging)
            return {
                "feed_key": feed_key,
                "edition": edition,
                "status": "rejected",
                "archive_sha256": archive_sha,
                "content_sha256": content_sha,
                "problems": [p.to_dict() for p in staging.problems],
                "message": "package rejected; no edition was made available",
            }
        self._install(feed_key, edition, archive_sha, content_sha, staging)
        return {
            "feed_key": feed_key,
            "edition": edition,
            "status": "ready",
            "archive_sha256": archive_sha,
            "content_sha256": content_sha,
            "timezone": staging.timezone,
            "counts": {table: len(getattr(staging, table)) for table in BASE_TABLES},
            "quarantined_trips": sorted({t["trip_id"] for t in staging.trip_issues}),
            "trip_issues": staging.trip_issues,
            "message": "edition loaded",
        }

    def _stored_problems(self, feed_key: str, edition: str, sha: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT category, file_name, line_number, record_id, field, message "
            "FROM load_rejections WHERE feed_key=? AND edition=? AND content_sha256=? "
            "ORDER BY seq", (feed_key, edition, sha)).fetchall()
        return [{"category": r["category"], "file": r["file_name"], "line": r["line_number"],
                 "record_id": r["record_id"], "field": r["field"], "message": r["message"]}
                for r in rows]

    def _record_rejection(self, feed_key: str, edition: str, archive_sha: str,
                          content_sha: str, staging: Staging) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT OR IGNORE INTO feeds(feed_key) VALUES(?)", (feed_key,))
            # A rejected load must not disturb an existing ready edition; but
            # a placeholder edition row would occupy the name.  Record rejections
            # separately when the edition name is already taken.
            taken = self.conn.execute(
                "SELECT 1 FROM editions WHERE feed_key=? AND edition=?",
                (feed_key, edition)).fetchone()
            if not taken:
                self.conn.execute(
                    "INSERT INTO editions(feed_key, edition, archive_sha256, content_sha256, "
                    "status, loaded_at) VALUES(?,?,?,?, 'rejected', ?)",
                    (feed_key, edition, archive_sha, content_sha, now))
            for seq, problem in enumerate(staging.problems):
                self.conn.execute(
                    "INSERT INTO load_rejections(feed_key, edition, archive_sha256, "
                    "content_sha256, seq, category, file_name, line_number, record_id, field, "
                    "message, context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (feed_key, edition, archive_sha, content_sha, seq, problem.category,
                     problem.file_name,
                     problem.line_number, problem.record_id, problem.field, problem.message,
                     json.dumps(problem.context, ensure_ascii=False, sort_keys=True)
                     if problem.context else None))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _install(self, feed_key: str, edition: str, archive_sha: str,
                 content_sha: str, staging: Staging) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT OR IGNORE INTO feeds(feed_key) VALUES(?)", (feed_key,))
            for table in BASE_TABLES:
                self.conn.execute("DELETE FROM %s WHERE feed_key=? AND edition=?" % table,
                                  (feed_key, edition))
            self.conn.execute("DELETE FROM trip_issues WHERE feed_key=? AND edition=?",
                              (feed_key, edition))
            self.conn.execute("DELETE FROM editions WHERE feed_key=? AND edition=?",
                              (feed_key, edition))
            self.conn.execute(
                "INSERT INTO editions(feed_key, edition, archive_sha256, content_sha256, "
                "status, timezone, agency_name, loaded_at) "
                "VALUES(?,?,?,?, 'ready', ?,?,?)",
                (feed_key, edition, archive_sha, content_sha, staging.timezone,
                 staging.agency[0]["agency_name"] if staging.agency else None, now))
            self.conn.executemany(
                "INSERT INTO agency VALUES(?,?,?,?,?,?,?)",
                [(feed_key, edition, i, a["agency_id"], a["agency_name"], a["agency_url"],
                  a["agency_timezone"]) for i, a in enumerate(staging.agency)])
            self.conn.executemany(
                "INSERT INTO stops VALUES(?,?,?,?,?,?,?,?)",
                [(feed_key, edition, s["stop_id"], s["stop_name"], s["stop_lat"], s["stop_lon"],
                  s["location_type"], s["parent_station"]) for s in staging.stops])
            self.conn.executemany(
                "INSERT INTO routes VALUES(?,?,?,?,?,?,?)",
                [(feed_key, edition, r["route_id"], r["agency_id"], r["route_short_name"],
                  r["route_long_name"], r["route_type"]) for r in staging.routes])
            self.conn.executemany(
                "INSERT INTO trips VALUES(?,?,?,?,?,?)",
                [(feed_key, edition, t["trip_id"], t["route_id"], t["service_id"],
                  t["trip_headsign"]) for t in staging.trips])
            self.conn.executemany(
                "INSERT INTO stop_times VALUES(?,?,?,?,?,?,?,?)",
                [(feed_key, edition, s["trip_id"], s["stop_sequence"], s["stop_id"],
                  s["arrival"], s["departure"], s["pickup_type"])
                 for s in staging.stop_times])
            self.conn.executemany(
                "INSERT INTO calendar VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(feed_key, edition, c["service_id"], c["days"]["monday"], c["days"]["tuesday"],
                  c["days"]["wednesday"], c["days"]["thursday"], c["days"]["friday"],
                  c["days"]["saturday"], c["days"]["sunday"], c["start_date"], c["end_date"])
                 for c in staging.calendar])
            self.conn.executemany(
                "INSERT INTO calendar_dates VALUES(?,?,?,?,?)",
                [(feed_key, edition, c["service_id"], c["date"], c["exception_type"])
                 for c in staging.calendar_dates])
            self.conn.executemany(
                "INSERT INTO frequencies VALUES(?,?,?,?,?,?,?,?)",
                [(feed_key, edition, i, f["trip_id"], f["start_time"], f["end_time"],
                  f["headway_secs"], f["exact_times"])
                 for i, f in enumerate(staging.frequencies)])
            self.conn.executemany(
                "INSERT INTO trip_issues VALUES(?,?,?,?,?,?,?)",
                [(feed_key, edition, t["trip_id"], t["category"], t["message"],
                  t["file_name"], t["line_number"]) for t in staging.trip_issues])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list_feeds(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.feed_key, e.edition, e.status, e.archive_sha256, e.content_sha256, "
            " e.timezone, e.loaded_at, "
            " (SELECT COUNT(*) FROM service_days d WHERE d.feed_key=e.feed_key AND d.edition=e.edition) "
            " AS expanded_days "
            "FROM editions e ORDER BY e.feed_key, e.edition").fetchall()
        return [dict(r) for r in rows]

    def rejection_report(self, feed_key: str, edition: str | None = None) -> dict[str, Any]:
        sql = ("SELECT feed_key, edition, archive_sha256, seq, category, file_name, line_number, "
               "archive_sha256, content_sha256, record_id, field, message "
               "FROM load_rejections WHERE feed_key=?")
        args: list[Any] = [feed_key]
        if edition is not None:
            sql += " AND edition=?"
            args.append(edition)
        sql += " ORDER BY edition, seq"
        rows = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        editions = self.conn.execute(
            "SELECT edition, status, archive_sha256, content_sha256, loaded_at "
            "FROM editions WHERE feed_key=? ORDER BY edition", (feed_key,)).fetchall()
        return {"feed_key": feed_key, "editions": [dict(r) for r in editions], "problems": rows}

    def base_rows(self, feed_key: str, edition: str, table: str,
                  limit: int | None = None) -> list[dict[str, Any]]:
        """Read a canonical base table for one edition only (never mixed)."""
        if table not in BASE_TABLES:
            raise GtfsError("%r is not a canonical base table" % table)
        order = {
            "agency": "agency_seq",
            "stops": "stop_id",
            "routes": "route_id",
            "trips": "trip_id",
            "stop_times": "trip_id, stop_sequence",
            "calendar": "service_id",
            "calendar_dates": "service_id, date",
            "frequencies": "freq_id",
        }[table]
        sql = ("SELECT * FROM %s WHERE feed_key=? AND edition=? ORDER BY %s"
               % (table, order))
        args: list[Any] = [feed_key, edition]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def status(self, feed_key: str, edition: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM editions WHERE feed_key=? AND edition=?", (feed_key, edition)).fetchone()
        if row is None:
            raise GtfsError("feed_key %r has no edition %r" % (feed_key, edition))
        counts = {}
        for table in BASE_TABLES:
            counts[table] = self.conn.execute(
                "SELECT COUNT(*) FROM %s WHERE feed_key=? AND edition=?" % table,
                (feed_key, edition)).fetchone()[0]
        issues = [dict(r) for r in self.conn.execute(
            "SELECT trip_id, category, message, file_name, line_number FROM trip_issues "
            "WHERE feed_key=? AND edition=? ORDER BY trip_id, category, line_number",
            (feed_key, edition))]
        days = [dict(r) for r in self.conn.execute(
            "SELECT service_date, day_start_utc, enabled_services_json, trip_instances, "
            "departure_rows, completed_at FROM service_days WHERE feed_key=? AND edition=? "
            "ORDER BY service_date",
            (feed_key, edition)).fetchall()]
        return {"feed_key": feed_key, "edition": edition, **dict(row), "counts": counts,
                "trip_issues": issues, "expanded_days": days}

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------

    def expand(self, feed_key: str, edition: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Replace expansion rows for [start_date, end_date] in one transaction."""
        start = parse_iso_date(start_date)
        end = parse_iso_date(end_date)
        if start is None or end is None:
            raise GtfsError("start and end must be valid calendar dates (YYYY-MM-DD)")
        if start > end:
            raise GtfsError("start %s is after end %s" % (start_date, end_date))
        row = self.conn.execute(
            "SELECT status, timezone FROM editions WHERE feed_key=? AND edition=?",
            (feed_key, edition)).fetchone()
        if row is None:
            raise GtfsError("feed_key %r has no edition %r" % (feed_key, edition))
        if row["status"] != "ready":
            raise GtfsError("edition %r of feed_key %r is %s and cannot be expanded"
                            % (edition, feed_key, row["status"]))
        tz = ZoneInfo(row["timezone"])
        base = self._load_base(feed_key, edition)
        request_key = json.dumps({"start": format_date(start), "end": format_date(end),
                                  "generation": base["generation"]}, sort_keys=True)

        days = []
        d = start
        while d <= end:
            days.append(d)
            d += dt.timedelta(days=1)

        # Build everything before opening the write transaction; only the
        # replacement itself is inside it, so readers never see a partial
        # replacement and a failure rolls back cleanly.
        computed = []
        for day in days:
            enabled = self._enabled_services(base, day)
            rows = self._expand_day(feed_key, edition, base, day, tz, enabled)
            computed.append((day, enabled, rows))
        self._write_days(feed_key, edition, start, end, tz, computed, request_key)
        return {
            "feed_key": feed_key,
            "edition": edition,
            "start_date": format_date(start),
            "end_date": format_date(end),
            "days": [
                {
                    "service_date": format_date(day),
                    "enabled_services": sorted(enabled),
                    "trip_instances": len({r["trip_instance_id"] for r in rows}),
                    "departure_rows": len(rows),
                }
                for day, enabled, rows in computed
            ],
        }

    def _load_base(self, feed_key: str, edition: str) -> dict[str, Any]:
        conn = self.conn
        stops = {r["stop_id"]: dict(r) for r in conn.execute(
            "SELECT stop_id, stop_name FROM stops WHERE feed_key=? AND edition=?",
            (feed_key, edition))}
        trips = [dict(r) for r in conn.execute(
            "SELECT trip_id, route_id, service_id FROM trips WHERE feed_key=? AND edition=? "
            "ORDER BY trip_id", (feed_key, edition))]
        stop_times: dict[str, list[dict]] = defaultdict(list)
        for r in conn.execute(
                "SELECT trip_id, stop_sequence, stop_id, arrival_seconds, departure_seconds, "
                "pickup_type FROM stop_times WHERE feed_key=? AND edition=? "
                "ORDER BY trip_id, stop_sequence", (feed_key, edition)):
            stop_times[r["trip_id"]].append(dict(r))
        calendar = {}
        for r in conn.execute(
                "SELECT * FROM calendar WHERE feed_key=? AND edition=?", (feed_key, edition)):
            calendar[r["service_id"]] = dict(r)
        exceptions: dict[tuple[str, str], int] = {}
        for r in conn.execute(
                "SELECT service_id, date, exception_type FROM calendar_dates "
                "WHERE feed_key=? AND edition=?", (feed_key, edition)):
            exceptions[(r["service_id"], r["date"])] = r["exception_type"]
        frequencies: dict[str, list[dict]] = defaultdict(list)
        for r in conn.execute(
                "SELECT * FROM frequencies WHERE feed_key=? AND edition=? "
                "ORDER BY trip_id, start_time, end_time, freq_id", (feed_key, edition)):
            frequencies[r["trip_id"]].append(dict(r))
        # Trips whose internal times contradict each other are not usable for
        # expansion; they stay in the base tables and are reported as issues.
        quarantined = {r["trip_id"] for r in conn.execute(
            "SELECT DISTINCT trip_id FROM trip_issues WHERE feed_key=? AND edition=? "
            "AND category IN ('invalid_trip_timing', 'trip_quarantined')", (feed_key, edition))}
        # Generation hash: a digest of every base row reachable by expansion,
        # so an identical request after a reload is demonstrably coherent.
        generation = self._generation_digest(trips, stop_times, calendar, exceptions,
                                             frequencies)
        return {"stops": stops, "trips": trips, "stop_times": stop_times,
                "calendar": calendar, "exceptions": exceptions,
                "frequencies": frequencies, "quarantined": quarantined,
                "generation": generation}

    @staticmethod
    def _generation_digest(trips, stop_times, calendar, exceptions, frequencies) -> str:
        gen = hashlib.sha256()
        for t in trips:
            gen.update(("T\x1f%s\x1f%s\x1f%s\x1e"
                        % (t["trip_id"], t["route_id"], t["service_id"])).encode())
            for st in stop_times[t["trip_id"]]:
                gen.update(("S\x1f%d\x1f%s\x1f%d\x1f%d\x1f%s\x1e"
                            % (st["stop_sequence"], st["stop_id"], st["arrival_seconds"],
                               st["departure_seconds"], st["pickup_type"])).encode())
        for sid in sorted(calendar):
            c = calendar[sid]
            gen.update(("C\x1f%s\x1f%s\x1f%s\x1f%s\x1e"
                        % (sid, c["start_date"], c["end_date"],
                           "".join(str(c[d]) for d in WEEKDAYS))).encode())
        for (sid, d), kind in sorted(exceptions.items()):
            gen.update(("X\x1f%s\x1f%s\x1f%d\x1e" % (sid, d, kind)).encode())
        for tid in sorted(frequencies):
            for f in frequencies[tid]:
                gen.update(("F\x1f%s\x1f%d\x1f%d\x1f%d\x1f%d\x1e"
                            % (tid, f["start_time"], f["end_time"], f["headway_secs"],
                               f["exact_times"])).encode())
        return gen.hexdigest()

    def _enabled_services(self, base: dict[str, Any], day: dt.date) -> set[str]:
        date_str = format_date(day)
        weekday = WEEKDAYS[day.weekday()]
        enabled: set[str] = set()
        for sid, row in base["calendar"].items():
            if row["start_date"] <= date_str <= row["end_date"] and row[weekday] == 1:
                enabled.add(sid)
        # calendar_dates replaces the weekly decision for that exact date,
        # including services that exist only through an addition.
        for (sid, d), kind in base["exceptions"].items():
            if d != date_str:
                continue
            if kind == 1:
                enabled.add(sid)
            else:
                enabled.discard(sid)
        return enabled

    def _expand_day(self, feed_key: str, edition: str, base: dict[str, Any], day: dt.date,
                    tz: ZoneInfo, enabled: set[str]) -> list[dict[str, Any]]:
        day_start = day_anchor_utc(day, tz)
        rows: list[dict[str, Any]] = []
        for trip in base["trips"]:
            if trip["service_id"] not in enabled:
                continue
            if trip["trip_id"] in base["quarantined"]:
                continue
            template = base["stop_times"].get(trip["trip_id"], [])
            if not template:
                continue
            if trip["trip_id"] not in base["frequencies"]:
                # Deterministic trip: one instance, template times as-is.
                rows.extend(self._instance_rows(feed_key, edition, day, trip, template,
                                                base, day_start, tz, None))
                continue
            first_departure = template[0]["departure_seconds"]
            for freq in base["frequencies"][trip["trip_id"]]:
                rows.extend(self._frequency_rows(feed_key, edition, day, trip, template,
                                                 base, day_start, tz, freq, first_departure))
        return rows

    def _instance_rows(self, feed_key: str, edition: str, day: dt.date, trip: dict,
                       template: list[dict], base: dict[str, Any], day_start: dt.datetime,
                       tz: ZoneInfo, frequency: dict[str, Any] | None,
                       *, trip_instance_id: str | None = None,
                       shift: int | None = None) -> list[dict[str, Any]]:
        rows = []
        instance = trip_instance_id
        for stop in _boardable(template):
            departure = stop["departure_seconds"]
            if shift is not None:
                depart_seconds = shift + (departure - template[0]["departure_seconds"])
            else:
                depart_seconds = departure
            if instance is None:
                instance = "T:%s" % trip["trip_id"]
            local = local_at(day_start, depart_seconds, tz)
            rows.append({
                "feed_key": feed_key,
                "edition": edition,
                "service_date": format_date(day),
                "trip_instance_id": instance,
                "trip_id": trip["trip_id"],
                "route_id": trip["route_id"],
                "service_id": trip["service_id"],
                "stop_id": stop["stop_id"],
                "stop_sequence": stop["stop_sequence"],
                "frequency_row": frequency["freq_id"] if frequency else None,
                "frequency_offset": frequency["start_time"] if frequency else None,
                "template_first_departure": template[0]["departure_seconds"],
                "template_start_seconds": template[0]["departure_seconds"],
                "departure_seconds": depart_seconds,
                "departure_utc": iso_utc(utc_at_anchor(day_start, depart_seconds)),
                "departure_local": iso_local(local),
                "utc_offset": utc_offset_text(local),
                "certainty": "exact",
                "min_departure_seconds": None,
                "max_departure_seconds": None,
                "headway_secs": None,
            })
        return rows

    def _frequency_rows(self, feed_key: str, edition: str, day: dt.date, trip: dict,
                        template: list[dict], base: dict[str, Any], day_start: dt.datetime,
                        tz: ZoneInfo, freq: dict[str, Any], first_departure: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if freq["exact_times"] == 1:
            start, end, headway = freq["start_time"], freq["end_time"], freq["headway_secs"]
            trip_number = 0
            departure = start
            while departure < end:  # right endpoint excluded
                instance = "F:%s:%d:%d" % (trip["trip_id"], freq["freq_id"], trip_number)
                rows.extend(self._instance_rows(feed_key, edition, day, trip, template, base,
                                                day_start, tz, freq,
                                                trip_instance_id=instance, shift=departure))
                trip_number += 1
                departure += headway
            return rows
        # exact_times=0: a headway promise, not concrete trips.  One row per
        # stop with the window shifted by the stop's offset from stop 0.
        start, end = freq["start_time"], freq["end_time"]
        instance = "W:%s:%d" % (trip["trip_id"], freq["freq_id"])
        for stop in _boardable(template):
            offset = stop["departure_seconds"] - first_departure
            lo = start + offset
            hi = end + offset  # endpoint excluded
            local = local_at(day_start, lo, tz)
            rows.append({
                "feed_key": feed_key,
                "edition": edition,
                "service_date": format_date(day),
                "trip_instance_id": instance,
                "trip_id": trip["trip_id"],
                "route_id": trip["route_id"],
                "service_id": trip["service_id"],
                "stop_id": stop["stop_id"],
                "stop_sequence": stop["stop_sequence"],
                "frequency_row": freq["freq_id"],
                "frequency_offset": offset,
                "template_first_departure": first_departure,
                "template_start_seconds": first_departure,
                "departure_seconds": lo,
                "departure_utc": iso_utc(utc_at_anchor(day_start, lo)),
                "departure_local": iso_local(local),
                "utc_offset": utc_offset_text(local),
                "certainty": "headway",
                "min_departure_seconds": lo,
                "max_departure_seconds": hi,
                "headway_secs": freq["headway_secs"],
            })
        return rows

    def _write_days(self, feed_key: str, edition: str, start: dt.date, end: dt.date,
                    tz: ZoneInfo, computed: list[tuple[dt.date, set[str], list[dict]]],
                    request_key: str) -> None:
        conn = self.conn
        fault_after = _fault_after_days()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Bounded range delete: a long range must not hit SQLite's
            # parameter limit, and the range is exactly what was computed.
            lo, hi = format_date(start), format_date(end)
            for table in ("departures", "service_days", "expansions"):
                conn.execute(
                    "DELETE FROM %s WHERE feed_key=? AND edition=? "
                    "AND service_date BETWEEN ? AND ?" % table,
                    (feed_key, edition, lo, hi))
            written = 0
            for day, enabled, rows in computed:
                date_str = format_date(day)
                start_utc = iso_utc(day_anchor_utc(day, tz))
                conn.execute(
                    "INSERT INTO service_days(feed_key, edition, service_date, timezone, "
                    "day_start_utc, ranges_spec, enabled_services_json, trip_instances, "
                    "departure_rows, completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (feed_key, edition, date_str, str(tz), start_utc,
                     "local noon -> UTC - 12h; offsets are integer seconds from day_start_utc",
                     json.dumps(sorted(enabled)),
                     len({r["trip_instance_id"] for r in rows}), len(rows),
                     dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
                conn.execute(
                    "INSERT INTO expansions(feed_key, edition, service_date, request_key, completed_at) "
                    "VALUES(?,?,?,?,?)",
                    (feed_key, edition, date_str, request_key,
                     dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
                if rows:
                    conn.executemany(
                        "INSERT INTO departures(feed_key, edition, service_date, trip_instance_id, "
                        "trip_id, route_id, service_id, stop_id, stop_sequence, frequency_row, "
                        "frequency_offset, template_start_seconds, template_first_departure, "
                        "departure_seconds, departure_utc, departure_local, utc_offset, "
                        "certainty, min_departure_seconds, max_departure_seconds, headway_secs) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(r["feed_key"], r["edition"], r["service_date"], r["trip_instance_id"],
                          r["trip_id"], r["route_id"], r["service_id"], r["stop_id"],
                          r["stop_sequence"], r["frequency_row"], r["frequency_offset"],
                          r["template_start_seconds"], r["template_first_departure"],
                          r["departure_seconds"],
                          r["departure_utc"], r["departure_local"], r["utc_offset"],
                          r["certainty"], r["min_departure_seconds"],
                          r["max_departure_seconds"], r["headway_secs"]) for r in rows])
                written += 1
                if fault_after is not None and written >= fault_after:
                    raise _InjectedFailure(
                        "fault injection: expansion interrupted after %d service day(s)" % written)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def departures(self, feed_key: str, edition: str, service_date: str,
                   stop_id: str | None = None) -> list[dict[str, Any]]:
        day = parse_iso_date(service_date)
        if day is None:
            raise GtfsError("service date %r must be a valid calendar date (YYYY-MM-DD)" % service_date)
        sql = "SELECT * FROM departures WHERE feed_key=? AND edition=? AND service_date=?"
        args: list[Any] = [feed_key, edition, format_date(day)]
        if stop_id is not None:
            sql += " AND stop_id=?"
            args.append(stop_id)
        sql += " ORDER BY departure_seconds, stop_id, trip_instance_id"
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def service_days(self, feed_key: str, edition: str, start: str, end: str) -> list[dict]:
        lo = parse_iso_date(start)
        hi = parse_iso_date(end)
        if lo is None or hi is None:
            raise GtfsError("start and end must be valid calendar dates (YYYY-MM-DD)")
        rows = self.conn.execute(
            "SELECT * FROM service_days WHERE feed_key=? AND edition=? AND service_date>=? "
            "AND service_date<=? ORDER BY service_date",
            (feed_key, edition, format_date(lo), format_date(hi))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled_services"] = json.loads(item.pop("enabled_services_json"))
            result.append(item)
        return result
