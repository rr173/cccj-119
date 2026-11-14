"""Domain helpers: CSV reading, GTFS field parsing, time-zone/day contracts."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import RejectError

# ---------------------------------------------------------------------------
# CSV handling (UTF-8 BOM, RFC4180 quotes/commas/newlines)
# ---------------------------------------------------------------------------

_BOM = b"\xef\xbb\xbf"
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def read_csv_bytes(data: bytes) -> tuple[list[str], list[tuple[dict[str, str], int, int]]]:
    """Decode a GTFS CSV member.

    Returns (header, rows) where each row is (raw_record, line_number,
    field_count).  Blank physical lines are skipped.  Quoted commas and
    newlines are handled by the csv module; line numbers therefore refer to
    the physical starting line of the record.
    """
    if data.startswith(_BOM):
        data = data[len(_BOM):]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RejectError(
            "encoding",
            "file is not valid UTF-8 (byte offset %d)" % exc.start,
            file_name="?",
        ) from exc
    # newline='' so that embedded CRLF inside a quoted cell is preserved;
    # the reader still understands the record separator.
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] = []
    rows: list[tuple[dict[str, str], int, int]] = []

    def physical_lines(rec: list[str]) -> int:
        joined = ",".join(rec)
        # Count CRLF / CR / LF exactly once each.
        return len(_NEWLINE_RE.findall(joined)) + 1

    line_no = 1          # physical line where the next record starts
    header_seen = False
    try:
        for record in reader:
            if not header_seen:
                if record and all(cell == "" for cell in record):
                    line_no += physical_lines(record)
                    continue
                header = record
                header_seen = True
                line_no += physical_lines(record)
                continue
            if not record or all(cell == "" for cell in record):
                line_no += physical_lines(record)
                continue
            # Short records are padded with empty cells by the caller's raw map.
            raw = {h: (record[i] if i < len(record) else "") for i, h in enumerate(header)}
            rows.append((raw, line_no, len(record)))
            line_no += physical_lines(record)
    except csv.Error as exc:
        raise RejectError("structure", "malformed CSV near line %d: %s" % (line_no, exc),
                          file_name="?") from exc
    if not header_seen:
        raise RejectError("structure", "file has no header row", file_name="?")
    return header, rows


# ---------------------------------------------------------------------------
# GTFS scalar parsers.  Identifiers are opaque strings, never normalised.
# ---------------------------------------------------------------------------


def as_text(value: str | None) -> str:
    if value is None:
        return ""
    return value


def require_text(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def parse_uint(value: str, allow_plus: bool = False, maximum: int | None = None) -> int | None:
    """Parse a GTFS non-negative integer, rejecting signs, whitespace, 0x.

    GTFS integer fields have no sign and no surrounding space; a leading '+'
    is only allowed where the specification's own grammar permits it (nowhere
    in this package).
    """
    if value is None or value == "":
        return None
    if value.startswith("+") and allow_plus and value[1:].isdigit():
        body = value[1:]
    elif value.isdigit():
        body = value
    else:
        return None
    if body == "":
        return None
    number = int(body)
    if maximum is not None and number > maximum:
        return None
    return number


def parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    v = value
    if v[0] in "+-":
        if not v[1:].isdigit():
            return None
    elif not v.isdigit():
        return None
    return int(v)


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


_TIME_RE = re.compile(r"^(\d{2,}):(\d\d):(\d\d)$")
_DATE_RE = re.compile(r"^\d{8}$")
_AGENCY_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


def parse_time(value: str | None) -> int | None:
    """HH:MM:SS (hours >= 0) into integer seconds; hours above 23 are legal."""
    if not value:
        return None
    m = _TIME_RE.match(value)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if mi > 59 or s > 59:
        return None
    return h * 3600 + mi * 60 + s


def parse_date(value: str | None) -> dt.date | None:
    if not value or not _DATE_RE.match(value):
        return None
    try:
        return dt.date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_iso_date(value: str | None) -> dt.date | None:
    """Operator-facing dates are ISO; GTFS file dates stay YYYYMMDD."""
    if not value:
        return None
    if _DATE_RE.match(value):
        return parse_date(value)
    if not _ISO_DATE_RE.match(value):
        return None
    try:
        return dt.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return None


def format_date(d: dt.date) -> str:
    return "%04d%02d%02d" % (d.year, d.month, d.day)


def format_time(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return "%s%02d:%02d:%02d" % (sign, seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def seconds_to_midnight(seconds: int) -> str:
    """Render an offset from service-day start as HH:MM:SS (may exceed 24)."""
    return format_time(seconds)


def parse_timezone(name: str) -> ZoneInfo:
    if not name or not _AGENCY_TZ_RE.match(name):
        raise RejectError("timezone", "agency_timezone is not an IANA zone name: %r" % (name or ""))
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RejectError("timezone", "unknown IANA timezone %r" % name) from exc


def unknown_timezone_error(name: str, where: str) -> RejectError:
    return RejectError("timezone", "unknown IANA timezone %r" % name, file_name=where)


# ---------------------------------------------------------------------------
# Service-day contract
# ---------------------------------------------------------------------------

NOON = dt.time(12, 0, 0)


def day_anchor_utc(service_date: dt.date, tz: ZoneInfo) -> dt.datetime:
    """Local noon of the service date, converted to UTC, minus 12 actual hours.

    The result is an instant (aware UTC).  Service seconds are counted from
    it, so 26:00:00 is 02:00 local on the following civil day even across a
    DST transition; no per-stop local clock arithmetic is performed.
    """
    noon_local = dt.datetime.combine(service_date, NOON, tzinfo=tz)
    anchor = noon_local.astimezone(dt.timezone.utc) - dt.timedelta(hours=12)
    return anchor


def utc_at_anchor(anchor_utc: dt.datetime, offset_seconds: int) -> dt.datetime:
    if anchor_utc.tzinfo is None:
        raise ValueError("anchor must be UTC-aware")
    return (anchor_utc + dt.timedelta(seconds=offset_seconds)).astimezone(dt.timezone.utc)


def local_at(anchor_utc: dt.datetime, offset_seconds: int, tz: ZoneInfo) -> dt.datetime:
    return (anchor_utc + dt.timedelta(seconds=offset_seconds)).astimezone(tz)


def utc_offset_text(local: dt.datetime) -> str:
    offset = local.utcoffset()
    if offset is None:
        return ""
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


def iso_utc(value: dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_local(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Entity row containers
# ---------------------------------------------------------------------------


@dataclass
class CsvContext:
    file_name: str

    def problem(self, category: str, message: str, line: int | None = None,
                field: str | None = None, entity_id: str | None = None) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "line_number": line,
            "record_id": entity_id,
            "field": field,
            "category": category,
            "message": message,
        }


