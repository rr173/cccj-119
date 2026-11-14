"""Terminal entry points for loading, inspection, expansion and stop reads."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .errors import GtfsError
from .store import Store


def _out(obj: Any, as_json: bool, plain: list[str] | None = None) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))
        return
    if plain:
        for line in plain:
            print(line)
        return
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_load(args, store: Store) -> int:
    result = store.load(args.feed_key, args.edition, args.package)
    if result.get("resend"):
        _out(result, args.json, [
            "resend feed_key=%s edition=%s status=%s; base tables unchanged"
            % (result["feed_key"], result["edition"], result["status"]),
            "archive sha256=%s content sha256=%s"
            % (result["archive_sha256"], result["content_sha256"]),
        ])
        return 0 if result["status"] == "ready" else 1
    if result["status"] == "ready":
        _out(result, args.json, [
            "loaded feed_key=%s edition=%s status=ready timezone=%s"
            % (result["feed_key"], result["edition"], result["timezone"]),
            "archive sha256=%s content sha256=%s"
            % (result["archive_sha256"], result["content_sha256"]),
            "base rows " + " ".join("%s=%d" % (k, v) for k, v in result["counts"].items()),
        ])
        return 0
    _out(result, args.json, [
        "REJECTED feed_key=%s edition=%s (%d problems); edition is not available"
        % (result["feed_key"], result["edition"], len(result["problems"]))
    ])
    for problem in result["problems"]:
        print("  %s | %s | %s" % (problem["file"] or "-",
                                  problem["line"] if problem["line"] is not None else "-",
                                  problem["message"]))
    return 1


def cmd_rejections(args, store: Store) -> int:
    report = store.rejection_report(args.feed_key, args.edition)
    plain = []
    for ed in report["editions"]:
        plain.append("edition %s status=%s content_sha256=%s"
                     % (ed["edition"], ed["status"], ed["content_sha256"]))
    for p in report["problems"]:
        plain.append("%s %s line=%s record=%s field=%s category=%s: %s"
                     % (p["edition"], p["file_name"] or "-", p["line_number"] if p["line_number"] is not None else "-",
                        p["record_id"] or "-", p["field"] or "-", p["category"], p["message"]))
    if not plain:
        plain.append("no stored editions or rejections for feed_key=%s" % args.feed_key)
    _out(report, args.json, plain)
    return 0


def cmd_status(args, store: Store) -> int:
    status = store.status(args.feed_key, args.edition)
    if args.days:
        status["expanded_days"] = status["expanded_days"][: args.days]
    plain = [
        "feed_key=%s edition=%s status=%s timezone=%s" % (status["feed_key"], status["edition"],
                                                        status["status"], status["timezone"]),
        "archive sha256=%s" % status["archive_sha256"],
    ]
    plain += ["base %s=%d" % (k, v) for k, v in status["counts"].items()]
    for issue in status["trip_issues"]:
        plain.append("trip issue %s %s: %s" % (issue["trip_id"], issue["category"],
                                               issue["message"]))
    for day in status["expanded_days"]:
        plain.append("expanded %s day_start_utc=%s trip_instances=%d rows=%d"
                     % (day["service_date"], day["day_start_utc"], day["trip_instances"],
                        day["departure_rows"]))
    if not status["expanded_days"]:
        plain.append("no expanded service dates")
    _out(status, args.json, plain)
    return 0


def cmd_expand(args, store: Store) -> int:
    result = store.expand(args.feed_key, args.edition, args.start, args.end)
    plain = ["expand feed_key=%s edition=%s %s..%s"
             % (result["feed_key"], result["edition"], result["start_date"], result["end_date"])]
    for day in result["days"]:
        plain.append("%s services=%s trip_instances=%d rows=%d"
                     % (day["service_date"], ",".join(day["enabled_services"]) or "-",
                        day["trip_instances"], day["departure_rows"]))
    _out(result, args.json, plain)
    return 0


def _departure_plain(row: dict[str, Any]) -> str:
    if row["certainty"] == "headway":
        return ("%s  stop=%s seq=%d  window=%s..%s every %ss  (headway promise, trip %s off=%d)"
                % (row["service_date"], row["stop_id"], row["stop_sequence"],
                   row["min_departure_seconds"], row["max_departure_seconds"], row["headway_secs"],
                   row["trip_id"], row["frequency_offset"]))
    return ("%s  stop=%s seq=%d  dep=%s  local=%s%s  utc=%s  trip=%s  instance=%s"
            % (row["service_date"], row["stop_id"], row["stop_sequence"],
               _hms(row["departure_seconds"]), row["departure_local"], row["utc_offset"],
               row["departure_utc"], row["trip_id"], row["trip_instance_id"]))


def _hms(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def cmd_departures(args, store: Store) -> int:
    rows = store.departures(args.feed_key, args.edition, args.service_date, args.stop)
    plain = ["%s %s stop=%s: %d row(s)" % (args.feed_key, args.edition, args.stop or "*", len(rows))]
    plain += [_departure_plain(r) for r in rows]
    _out(rows, args.json, plain)
    return 0


def cmd_days(args, store: Store) -> int:
    rows = store.service_days(args.feed_key, args.edition, args.start, args.end)
    plain = ["%s %s %s..%s: %d expanded day(s)"
             % (args.feed_key, args.edition, args.start, args.end, len(rows))]
    plain += ["%s day_start_utc=%s trip_instances=%d rows=%d"
              % (r["service_date"], r["day_start_utc"], r["trip_instances"],
                 r["departure_rows"]) for r in rows]
    _out(rows, args.json, plain)
    return 0


def cmd_show(args, store: Store) -> int:
    rows = store.base_rows(args.feed_key, args.edition, args.table, args.limit)
    plain = ["%s %s %s: %d row(s)" % (args.feed_key, args.edition, args.table, len(rows))]
    for row in rows:
        plain.append("  " + " | ".join(
            "%s=%s" % (k, v) for k, v in row.items()
            if k not in ("feed_key", "edition")))
    _out(rows, args.json, plain)
    return 0


def cmd_list(args, store: Store) -> int:
    rows = store.list_feeds()
    plain = ["%s/%s status=%s timezone=%s expanded_days=%d"
             % (r["feed_key"], r["edition"], r["status"], r["timezone"], r["expanded_days"])
             for r in rows] or ["no feeds loaded"]
    _out(rows, args.json, plain)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtfs-store",
        description="Local GTFS static intake and service-date expansion (SQLite).")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--db", default="gtfs.db", help="SQLite database file (default: gtfs.db)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("load", help="validate and atomically load an edition")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--package", required=True)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("rejections", help="show refusal reasons for a feed_key")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition")
    p.set_defaults(func=cmd_rejections)

    p = sub.add_parser("status", help="show edition state and expansion rows")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--days", type=int, default=None,
                   help="limit the listed expanded days (default: all)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("expand", help="(re)build departures for an inclusive date range")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_expand)

    p = sub.add_parser("departures", help="read stop departures for one service date")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--date", dest="service_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--stop")
    p.set_defaults(func=cmd_departures)

    p = sub.add_parser("days", help="read expanded service days")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_days)

    p = sub.add_parser("show", help="read one canonical base table for one edition")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--table", required=True,
                   help="one of: agency stops routes trips stop_times calendar "
                        "calendar_dates frequencies")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list stored feeds and editions")
    p.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        with Store(args.db) as store:
            return args.func(args, store)
    except GtfsError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
