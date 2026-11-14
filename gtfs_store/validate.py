"""Parse and validate the accepted GTFS files into in-memory staging tables.

Every check emits a structured problem dict; validation is exhaustive rather
than fail-fast, so a rejected package reports all identifiable issues.  No
field value is normalised silently: identifiers stay as they appeared, and
timestamps are converted only where the GTFS meaning requires it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .errors import RejectError
from .model import (
    CsvContext,
    parse_int,
    format_date,
    parse_date,
    parse_float,
    parse_time,
    parse_timezone,
    parse_uint,
    read_csv_bytes,
    require_text,
    as_text,
    seconds_to_midnight,
)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

REQUIRED_COLUMNS = {
    "agency.txt": ("agency_name", "agency_url", "agency_timezone"),
    "stops.txt": ("stop_id",),
    "routes.txt": ("route_id", "route_type"),
    "trips.txt": ("route_id", "service_id", "trip_id"),
    "stop_times.txt": ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"),
    "calendar.txt": ("service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                     "saturday", "sunday", "start_date", "end_date"),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
    "frequencies.txt": ("trip_id", "start_time", "end_time", "headway_secs"),
}

# Column sets larger than this are treated as an unsupported/abusive layout.
MAX_COLUMNS = 200
MAX_ROWS_PER_FILE = 2000000


@dataclass
class Staging:
    feed_key: str
    edition: str
    archive_sha256: str
    agency: list[dict[str, Any]] = field(default_factory=list)
    stops: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    trips: list[dict[str, Any]] = field(default_factory=list)
    stop_times: list[dict[str, Any]] = field(default_factory=list)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    calendar_dates: list[dict[str, Any]] = field(default_factory=list)
    frequencies: list[dict[str, Any]] = field(default_factory=list)
    trip_issues: list[dict[str, Any]] = field(default_factory=list)
    timezone: str = ""
    problems: list[RejectError] = field(default_factory=list)
    present_files: set[str] = field(default_factory=set)

    def reject(self, category: str, message: str, *, file_name: str | None = None,
               line_number: int | None = None, field: str | None = None,
               record_id: str | None = None) -> None:
        self.problems.append(RejectError(
            category, message, file_name=file_name, line_number=line_number,
            field=field, record_id=record_id))

    def flag_trip(self, trip_id: str, category: str, message: str, *,
                  file_name: str | None = None, line_number: int | None = None) -> None:
        self.trip_issues.append({
            "trip_id": trip_id, "category": category, "message": message,
            "file_name": file_name, "line_number": line_number,
        })

    @property
    def ok(self) -> bool:
        return not self.problems


class _File:
    """Wrapper around one CSV member with problem recording."""

    def __init__(self, staging: Staging, name: str, data: bytes) -> None:
        self.staging = staging
        self.name = name
        self.ctx = CsvContext(name)
        try:
            header, rows = read_csv_bytes(data)
        except RejectError as exc:
            exc.file_name = name
            staging.problems.append(exc)
            self.header = []
            self.rows = []
            self.structurally_broken = True
            return
        self.structurally_broken = False
        self.header = header
        self.rows = rows
        self._validate_header()
        if len(rows) > MAX_ROWS_PER_FILE:
            staging.reject("size", "file has %d records, limit is %d"
                           % (len(rows), MAX_ROWS_PER_FILE), file_name=name)

    def _validate_header(self) -> None:
        if len(self.header) > MAX_COLUMNS:
            self.staging.reject("structure", "header has %d columns, limit is %d"
                                % (len(self.header), MAX_COLUMNS), file_name=self.name)
            self.structurally_broken = True
            return
        seen: dict[str, int] = {}
        for i, column in enumerate(self.header):
            if column == "":
                self.staging.reject("structure", "empty column name at position %d" % i,
                                    file_name=self.name, line_number=1, field="")
                continue
            if column in seen:
                self.staging.reject("structure", "duplicate column %r (positions %d and %d)"
                                    % (column, seen[column], i),
                                    file_name=self.name, line_number=1, field=column)
                continue
            seen[column] = i
        for required in REQUIRED_COLUMNS.get(self.name, ()):
            if required not in seen:
                self.staging.reject("missing_column", "required column %r is absent" % required,
                                    file_name=self.name, line_number=1, field=required)
                self.structurally_broken = True
        extra = [c for c in self.header if c and c not in REQUIRED_COLUMNS.get(self.name, ())]
        # Columns outside the GTFS meaning of the file are ignored only where
        # the GTFS specification itself defines them as optional metadata.
        allowed_extra = OPTIONAL_COLUMNS.get(self.name, set())
        for column in extra:
            if column not in allowed_extra and column not in REQUIRED_COLUMNS.get(self.name, ()):
                self.staging.reject(
                    "unsupported_semantics",
                    "column %r is not in the accepted %s layout" % (column, self.name),
                    file_name=self.name, line_number=1, field=column)

    def records(self):
        if self.structurally_broken:
            return
        for raw, line, width in self.rows:
            if width > len(self.header):
                self.staging.reject(
                    "structure",
                    "record has %d fields but header has %d" % (width, len(self.header)),
                    file_name=self.name, line_number=line)
                continue
            yield raw, line

    def problem(self, category: str, message: str, line: int | None = None,
                field: str | None = None, entity_id: str | None = None) -> None:
        self.staging.reject(category, message, file_name=self.name, line_number=line,
                            field=field, record_id=entity_id)


OPTIONAL_COLUMNS: dict[str, set[str]] = {
    "agency.txt": {
        "agency_id", "agency_lang", "agency_phone", "agency_fare_url", "agency_email",
        "agency_timezone", "agency_url", "agency_name",
    },
    "stops.txt": {
        "stop_id", "stop_code", "stop_name", "stop_desc", "stop_lat", "stop_lon",
        "zone_id", "stop_url", "location_type", "parent_station", "stop_timezone",
        "wheelchair_boarding", "level_id", "platform_code",
    },
    "routes.txt": {
        "route_id", "agency_id", "route_short_name", "route_long_name", "route_desc",
        "route_type", "route_url", "route_color", "route_text_color", "route_sort_order",
        "continuous_pickup", "continuous_drop_off", "network_id",
    },
    "trips.txt": {
        "route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
        "direction_id", "block_id", "shape_id", "wheelchair_accessible",
        "bikes_allowed",
    },
    "stop_times.txt": {
        "trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
        "stop_headsign", "pickup_type", "drop_off_type", "continuous_pickup",
        "continuous_drop_off", "shape_dist_traveled", "timepoint",
    },
    "calendar.txt": {
        "service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "start_date", "end_date",
    },
    "calendar_dates.txt": {"service_id", "date", "exception_type", "holiday_name"},
    "frequencies.txt": {
        "trip_id", "start_time", "end_time", "headway_secs", "exact_times",
    },
}


def _dates(file: _File, raw: dict[str, str], line: int, field: str,
           entity: str) -> dt.date | None:
    value = as_text(raw.get(field))
    if value == "":
        file.problem("missing_value", "%s is required" % field, line, field, entity)
        return None
    parsed = parse_date(value)
    if parsed is None:
        file.problem("invalid_date", "%s=%r is not a valid YYYYMMDD date" % (field, value),
                     line, field, entity)
        return None
    return parsed


def _range_field(raw: dict[str, str], line: int, field: str, file: _File, entity: str) -> dt.date | None:
    return _dates(file, raw, line, field, entity)


def parse_package(feed_key: str, edition: str, archive_sha256: str, contents) -> Staging:
    staging = Staging(feed_key=feed_key, edition=edition, archive_sha256=archive_sha256)
    staging.present_files = set(contents.members)
    files: dict[str, _File] = {}
    for name, data in contents.members.items():
        files[name] = _File(staging, name, data)

    missing = [name for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt",
                                 "stop_times.txt") if name not in contents.members]
    if missing:
        for name in missing:
            staging.reject("missing_file", "required file %s is absent" % name, file_name=name)
    if "calendar.txt" not in contents.members and "calendar_dates.txt" not in contents.members:
        staging.reject("missing_file",
                       "at least one of calendar.txt and calendar_dates.txt is required",
                       file_name="calendar.txt")

    agency = _parse_agency(staging, files.get("agency.txt"))
    stops = _parse_stops(staging, files.get("stops.txt"))
    routes = _parse_routes(staging, files.get("routes.txt"), agency)
    trips = _parse_trips(staging, files.get("trips.txt"), routes)
    stop_times = _parse_stop_times(staging, files.get("stop_times.txt"), trips, stops)
    calendar = _parse_calendar(staging, files.get("calendar.txt"))
    calendar_dates = _parse_calendar_dates(staging, files.get("calendar_dates.txt"), calendar)
    frequencies = _parse_frequencies(staging, files.get("frequencies.txt"), trips, stop_times)
    return staging


def _parse_agency(staging: Staging, file: _File | None) -> list[dict[str, Any]]:
    if file is None:
        return []
    rows: list[dict[str, Any]] = []
    zones: set[str] = set()
    for raw, line in file.records():
        agency_id = require_text(raw.get("agency_id"))
        name = require_text(raw.get("agency_name"))
        url = require_text(raw.get("agency_url"))
        zone = require_text(raw.get("agency_timezone"))
        if not name:
            file.problem("missing_value", "agency_name is required", line, "agency_name", agency_id)
        if not url:
            file.problem("missing_value", "agency_url is required", line, "agency_url", agency_id)
        if not zone:
            file.problem("missing_value", "agency_timezone is required", line,
                         "agency_timezone", agency_id)
            continue
        try:
            parse_timezone(zone)
        except RejectError as exc:
            file.problem("invalid_timezone", exc.message, line, "agency_timezone", agency_id)
            continue
        zones.add(zone)
        rows.append({"agency_id": agency_id, "agency_name": name, "agency_url": url,
                     "agency_timezone": zone})
    if rows and len(zones) != 1:
        file.problem("timezone",
                     "agency_timezone differs within the file: %s" % ", ".join(sorted(zones)))
    if not rows and not file.structurally_broken:
        staging.reject("empty_file", "agency.txt has no agencies", file_name="agency.txt")
    staging.agency = rows
    staging.timezone = next(iter(zones)) if len(zones) == 1 else ""
    return rows


def _parse_stops(staging: Staging, file: _File | None) -> list[dict[str, Any]]:
    if file is None:
        return []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw, line in file.records():
        sid = require_text(raw.get("stop_id"))
        if not sid:
            file.problem("missing_value", "stop_id is required", line, "stop_id")
            continue
        if sid in seen:
            file.problem("duplicate_id", "stop_id %r already appears on line %d" % (sid, seen[sid]),
                         line, "stop_id", sid)
            continue
        seen[sid] = line
        lat = raw.get("stop_lat")
        lon = raw.get("stop_lon")
        lat_v = parse_float(lat)
        lon_v = parse_float(lon)
        if lat not in (None, "") and lat_v is None:
            file.problem("invalid_value", "stop_lat=%r is not a number" % lat, line, "stop_lat", sid)
        if lon not in (None, "") and lon_v is None:
            file.problem("invalid_value", "stop_lon=%r is not a number" % lon, line, "stop_lon", sid)
        if lat_v is not None and not -90 <= lat_v <= 90:
            file.problem("invalid_value", "stop_lat=%r is outside [-90, 90]" % lat, line,
                         "stop_lat", sid)
        if lon_v is not None and not -180 <= lon_v <= 180:
            file.problem("invalid_value", "stop_lon=%r is outside [-180, 180]" % lon, line,
                         "stop_lon", sid)
        stop_tz = require_text(raw.get("stop_timezone"))
        if stop_tz is not None:
            if not staging.timezone:
                file.problem("unsupported_semantics",
                             "stop_timezone=%r is not supported (agency-level timezone only)"
                             % stop_tz, line, "stop_timezone", sid)
            else:
                try:
                    parse_timezone(stop_tz)
                except RejectError as exc:
                    file.problem("invalid_timezone", exc.message, line, "stop_timezone", sid)
                if stop_tz != staging.timezone:
                    file.problem("unsupported_semantics",
                                 "stop_timezone=%r differs from agency_timezone=%r; "
                                 "per-stop timezones are not supported"
                                 % (stop_tz, staging.timezone), line, "stop_timezone", sid)
        location_type = raw.get("location_type", "")
        if location_type not in (None, "", "0", "1", "2", "3", "4"):
            file.problem("invalid_value", "location_type=%r is not a GTFS location type"
                         % location_type, line, "location_type", sid)
        rows.append({
            "stop_id": sid,
            "stop_name": require_text(raw.get("stop_name")),
            "stop_lat": lat_v,
            "stop_lon": lon_v,
            "location_type": location_type or "0",
            "parent_station": require_text(raw.get("parent_station")),
        })
    staging.stops = rows
    return rows


def _parse_routes(staging: Staging, file: _File | None,
                  agency: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if file is None:
        return []
    agency_ids = {a["agency_id"] for a in agency if a["agency_id"] is not None}
    single_agency = len(agency) == 1
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw, line in file.records():
        rid = require_text(raw.get("route_id"))
        rtype = as_text(raw.get("route_type"))
        if not rid:
            file.problem("missing_value", "route_id is required", line, "route_id")
            continue
        if rid in seen:
            file.problem("duplicate_id", "route_id %r already appears on line %d" % (rid, seen[rid]),
                         line, "route_id", rid)
            continue
        seen[rid] = line
        t = parse_int(rtype)
        if t is None:
            file.problem("missing_value" if rtype == "" else "invalid_value",
                         "route_type is required and must be an integer", line, "route_type", rid)
            continue
        if t < 0:
            file.problem("invalid_value", "route_type=%r must not be negative" % rtype,
                         line, "route_type", rid)
            continue
        agency_id = require_text(raw.get("agency_id"))
        if agency_id is not None:
            if agency_id not in agency_ids:
                file.problem("foreign_key",
                             "route agency_id %r does not exist in agency.txt" % agency_id,
                             line, "agency_id", rid)
        elif not single_agency:
            file.problem("missing_value",
                         "routes.txt needs agency_id when agency.txt has more than one agency",
                         line, "agency_id", rid)
        rows.append({"route_id": rid, "agency_id": agency_id,
                     "route_short_name": require_text(raw.get("route_short_name")),
                     "route_long_name": require_text(raw.get("route_long_name")),
                     "route_type": rtype})
    staging.routes = rows
    return rows


def _parse_trips(staging: Staging, file: _File | None, routes: list[dict]) -> list[dict[str, Any]]:
    if file is None:
        return []
    route_ids = {r["route_id"] for r in routes}
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw, line in file.records():
        tid = require_text(raw.get("trip_id"))
        sid = require_text(raw.get("service_id"))
        rid = require_text(raw.get("route_id"))
        missing = False
        if not tid:
            file.problem("missing_value", "trip_id is required", line, "trip_id")
            missing = True
        if not sid:
            file.problem("missing_value", "service_id is required", line, "service_id", tid)
            missing = True
        if not rid:
            file.problem("missing_value", "route_id is required", line, "route_id", tid)
            missing = True
        if missing:
            continue
        if tid in seen:
            file.problem("duplicate_id", "trip_id %r already appears on line %d" % (tid, seen[tid]),
                         line, "trip_id", tid)
            continue
        seen[tid] = line
        if rid not in route_ids:
            file.problem("foreign_key", "trip route_id %r does not exist in routes.txt" % rid,
                         line, "route_id", tid)
        direction = raw.get("direction_id", "")
        if direction not in (None, "", "0", "1"):
            file.problem("invalid_value", "direction_id=%r is not 0 or 1" % direction, line,
                         "direction_id", tid)
        rows.append({"trip_id": tid, "route_id": rid, "service_id": sid,
                     "trip_headsign": require_text(raw.get("trip_headsign"))})
    staging.trips = rows
    return rows


def _parse_stop_times(staging: Staging, file: _File | None, trips: list[dict],
                      stops: list[dict]) -> list[dict[str, Any]]:
    if file is None:
        return []
    trip_ids = {t["trip_id"] for t in trips}
    stop_ids = {s["stop_id"] for s in stops}
    rows: list[dict[str, Any]] = []
    by_trip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sequence_seen: dict[tuple[str, int], int] = {}
    for raw, line in file.records():
        tid = require_text(raw.get("trip_id"))
        sid = require_text(raw.get("stop_id"))
        seq_raw = as_text(raw.get("stop_sequence"))
        arr_raw = as_text(raw.get("arrival_time"))
        dep_raw = as_text(raw.get("departure_time"))
        problems_before = len(staging.problems)
        if not tid:
            file.problem("missing_value", "trip_id is required", line, "trip_id")
        if not sid:
            file.problem("missing_value", "stop_id is required", line, "stop_id", tid)
        if seq_raw == "":
            file.problem("missing_value", "stop_sequence is required", line, "stop_sequence", tid)
        seq = parse_uint(seq_raw)
        if seq_raw != "" and seq is None:
            file.problem("invalid_value", "stop_sequence=%r is not a non-negative integer"
                         % seq_raw, line, "stop_sequence", tid)
        if arr_raw == "":
            file.problem("missing_time", "arrival_time is required (no interpolation is performed)",
                         line, "arrival_time", tid)
        arr = parse_time(arr_raw)
        if arr_raw != "" and arr is None:
            file.problem("invalid_time", "arrival_time=%r is not a valid HH:MM:SS" % arr_raw,
                         line, "arrival_time", tid)
        if dep_raw == "":
            file.problem("missing_time", "departure_time is required (no interpolation is performed)",
                         line, "departure_time", tid)
        dep = parse_time(dep_raw)
        if dep_raw != "" and dep is None:
            file.problem("invalid_time", "departure_time=%r is not a valid HH:MM:SS" % dep_raw,
                         line, "departure_time", tid)
        if len(staging.problems) > problems_before:
            continue
        if tid not in trip_ids:
            file.problem("foreign_key", "stop_times trip_id %r does not exist in trips.txt" % tid,
                         line, "trip_id", tid)
            continue
        if sid not in stop_ids:
            file.problem("foreign_key", "stop_times stop_id %r does not exist in stops.txt" % sid,
                         line, "stop_id", tid)
            continue
        key = (tid, seq)
        if key in sequence_seen:
            file.problem("duplicate_id",
                         "trip %r already has stop_sequence %d on line %d" % (tid, seq, sequence_seen[key]),
                         line, "stop_sequence", tid)
            continue
        sequence_seen[key] = line
        pickup_raw = as_text(raw.get("pickup_type", ""))
        dropoff_raw = as_text(raw.get("drop_off_type", ""))
        if pickup_raw not in ("", "0", "1", "2", "3"):
            file.problem("invalid_value", "pickup_type=%r is not a GTFS pickup type"
                         % pickup_raw, line, "pickup_type", tid)
            continue
        if dropoff_raw not in ("", "0", "1", "2", "3"):
            file.problem("invalid_value", "drop_off_type=%r is not a GTFS drop-off type"
                         % dropoff_raw, line, "drop_off_type", tid)
            continue
        for flag in ("continuous_pickup", "continuous_drop_off"):
            value = as_text(raw.get(flag, ""))
            if value not in ("", "1"):
                file.problem(
                    "unsupported_semantics",
                    "%s=%r requests boarding/alighting along the vehicle path (continuous "
                    "stopping), which this store does not model" % (flag, value),
                    line, flag, tid)
                continue
        row = {"trip_id": tid, "stop_sequence": seq, "stop_id": sid,
               "arrival": arr, "departure": dep, "line": line,
               "pickup_type": pickup_raw or "0"}
        rows.append(row)
        by_trip[tid].append(row)
    for tid in trip_ids:
        if tid not in by_trip:
            staging.reject("invalid_trip_structure",
                           "trip %r has no stop_times records" % tid,
                           file_name="trips.txt", field="trip_id", record_id=tid)
    for tid, trip_rows in by_trip.items():
        trip_rows.sort(key=lambda r: r["stop_sequence"])
        broken = False
        for i, row in enumerate(trip_rows):
            if row["arrival"] > row["departure"]:
                staging.flag_trip(
                    tid, "invalid_trip_timing",
                    "stop_sequence %d arrives at %s after departing at %s"
                    % (row["stop_sequence"], seconds_to_midnight(row["arrival"]),
                       seconds_to_midnight(row["departure"])),
                    file_name="stop_times.txt", line_number=row["line"])
                broken = True
            if i:
                prev = trip_rows[i - 1]
                if row["arrival"] < prev["departure"]:
                    staging.flag_trip(
                        tid, "invalid_trip_timing",
                        "stop_sequence %d arrives at %s before the previous stop (sequence %d) "
                        "departs at %s"
                        % (row["stop_sequence"], seconds_to_midnight(row["arrival"]),
                           prev["stop_sequence"], seconds_to_midnight(prev["departure"])),
                        file_name="stop_times.txt", line_number=row["line"])
                    broken = True
        if broken:
            staging.flag_trip(
                tid, "trip_quarantined",
                "the whole trip is unusable for expansion; its stop order and times are "
                "left untouched (no sorting or interpolation)",
                file_name="stop_times.txt")
    staging.stop_times = rows
    return rows


def _parse_calendar(staging: Staging, file: _File | None) -> list[dict[str, Any]]:
    if file is None:
        return []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw, line in file.records():
        sid = require_text(raw.get("service_id"))
        if not sid:
            file.problem("missing_value", "service_id is required", line, "service_id")
            continue
        if sid in seen:
            file.problem("duplicate_id", "service_id %r already appears on line %d"
                         % (sid, seen[sid]), line, "service_id", sid)
            continue
        seen[sid] = line
        days: dict[str, int] = {}
        bad = False
        for day in WEEKDAYS:
            value = as_text(raw.get(day))
            if value == "":
                file.problem("missing_value", "%s is required" % day, line, day, sid)
                bad = True
            elif value not in ("0", "1"):
                file.problem("invalid_weekday", "%s=%r is not 0 or 1" % (day, value),
                             line, day, sid)
                bad = True
            else:
                days[day] = int(value)
        start = _range_field(raw, line, "start_date", file, sid)
        end = _range_field(raw, line, "end_date", file, sid)
        if start is None or end is None:
            bad = True
        if start and end and start > end:
            file.problem("invalid_date", "start_date %s is after end_date %s"
                         % (format_date(start), format_date(end)), line, "start_date")
            bad = True
        if bad:
            continue
        rows.append({"service_id": sid, "days": days,
                     "start_date": format_date(start), "end_date": format_date(end)})
    staging.calendar = rows
    return rows


def _parse_calendar_dates(staging: Staging, file: _File | None,
                          calendar: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if file is None:
        return []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    added: set[str] = set()
    for raw, line in file.records():
        sid = require_text(raw.get("service_id"))
        date_raw = as_text(raw.get("date"))
        exception_raw = as_text(raw.get("exception_type"))
        bad = False
        if not sid:
            file.problem("missing_value", "service_id is required", line, "service_id")
            bad = True
        if date_raw == "":
            file.problem("missing_value", "date is required", line, "date", sid)
            bad = True
        date = parse_date(date_raw)
        if date_raw != "" and date is None:
            file.problem("invalid_date", "date=%r is not a valid YYYYMMDD date" % date_raw,
                         line, "date", sid)
            bad = True
        if exception_raw == "":
            file.problem("missing_value", "exception_type is required", line, "exception_type", sid)
            bad = True
        elif exception_raw not in ("1", "2"):
            file.problem("invalid_value", "exception_type=%r is not 1 or 2" % exception_raw,
                         line, "exception_type", sid)
            bad = True
        if bad:
            continue
        key = (sid, format_date(date))
        existing = by_key.get(key)
        if existing is not None:
            kinds = {existing["exception_type"], exception_raw}
            detail = ("add and remove" if len(kinds) == 2
                      else "exception type %s twice" % exception_raw)
            file.problem("conflict",
                         "service %r on %s has two exception entries (lines %d and %d): %s; "
                         "the conflict is not resolved by file order"
                         % (sid, format_date(date), existing["line"], line, detail),
                         line, "exception_type", sid)
            continue
        # A service may be defined solely by calendar_dates additions: no
        # calendar.txt row is required for it.
        if exception_raw == "1":
            added.add(sid)
        row = {"service_id": sid, "date": format_date(date),
               "exception_type": int(exception_raw), "line": line}
        by_key[key] = row
        rows.append(row)
    # A removal of a service that is neither in calendar.txt nor ever added
    # is an orphan exception and would silently do nothing.
    known = {c["service_id"] for c in calendar} | added
    for row in rows:
        if row["exception_type"] == 2 and row["service_id"] not in known:
            file.problem("foreign_key",
                         "calendar_dates removes service_id %r but no calendar.txt row "
                         "or add-exception defines it" % row["service_id"],
                         row["line"], "service_id", row["service_id"])
    staging.calendar_dates = rows
    return rows


def _parse_frequencies(staging: Staging, file: _File | None, trips: list[dict],
                       stop_times: list[dict]) -> list[dict[str, Any]]:
    if file is None:
        return []
    trip_ids = {t["trip_id"] for t in trips}
    by_trip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw, line in file.records():
        tid = require_text(raw.get("trip_id"))
        start_raw = as_text(raw.get("start_time"))
        end_raw = as_text(raw.get("end_time"))
        headway_raw = as_text(raw.get("headway_secs"))
        exact_raw = as_text(raw.get("exact_times", ""))
        bad = False
        if not tid:
            file.problem("missing_value", "trip_id is required", line, "trip_id")
            bad = True
        start = parse_time(start_raw)
        if start_raw == "":
            file.problem("missing_value", "start_time is required", line, "start_time", tid)
            bad = True
        elif start is None:
            file.problem("invalid_time", "start_time=%r is not a valid HH:MM:SS" % start_raw,
                         line, "start_time", tid)
            bad = True
        end = parse_time(end_raw)
        if end_raw == "":
            file.problem("missing_value", "end_time is required", line, "end_time", tid)
            bad = True
        elif end is None:
            file.problem("invalid_time", "end_time=%r is not a valid HH:MM:SS" % end_raw,
                         line, "end_time", tid)
            bad = True
        headway = parse_uint(headway_raw, allow_plus=False)
        if headway_raw == "":
            file.problem("missing_value", "headway_secs is required", line, "headway_secs", tid)
            bad = True
        elif headway is None or headway == 0:
            file.problem("invalid_headway", "headway_secs=%r must be a positive integer"
                         % headway_raw, line, "headway_secs", tid)
            bad = True
        if exact_raw not in ("", "0", "1"):
            file.problem("invalid_value", "exact_times=%r is not 0 or 1" % exact_raw,
                         line, "exact_times", tid)
            bad = True
        if bad:
            continue
        if tid not in trip_ids:
            file.problem("foreign_key", "frequencies trip_id %r does not exist in trips.txt" % tid,
                         line, "trip_id", tid)
            continue
        if start > end:
            file.problem("invalid_interval", "start_time %s is after end_time %s"
                         % (start_raw, end_raw), line, "start_time", tid)
            continue
        row = {"trip_id": tid, "start_time": start, "end_time": end,
               "headway_secs": headway, "exact_times": int(exact_raw or "0"), "line": line}
        by_trip[tid].append(row)
    for tid, intervals in by_trip.items():
        intervals.sort(key=lambda r: (r["start_time"], r["end_time"], r["line"]))
        for i in range(1, len(intervals)):
            prev, cur = intervals[i - 1], intervals[i]
            if cur["start_time"] < prev["end_time"]:
                file.problem(
                    "overlapping_frequency",
                    "trip %r frequency intervals overlap: [%s,%s) on line %d and [%s,%s) on line %d; "
                    "touching intervals are legal"
                    % (tid, seconds_to_midnight(prev["start_time"]),
                       seconds_to_midnight(prev["end_time"]), prev["line"],
                       seconds_to_midnight(cur["start_time"]),
                       seconds_to_midnight(cur["end_time"]), cur["line"]),
                    cur["line"], "start_time", tid)
        # A frequency template must have well-formed stop_times to be expanded.
        template = [s for s in stop_times if s["trip_id"] == tid]
        template.sort(key=lambda r: r["stop_sequence"])
        if not template:
            file.problem("invalid_trip_structure",
                         "frequency trip %r has no stop_times template" % tid, entity_id=tid)
        for row in intervals:
            row["template"] = template
    staging.frequencies = [r for rows in by_trip.values() for r in rows]
    return staging.frequencies
