"""Terminal entry points for loading, inspection, expansion and stop reads.

Realtime subcommands (rt-*) are only wired up when the optional
gtfs-realtime-bindings are importable; the static commands never need them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Offline fare clearing
# ---------------------------------------------------------------------------

def _fares(store: Store):
    from .fares import FareStore
    return FareStore(store)


def cmd_fare_install(args, store: Store) -> int:
    result = _fares(store).fare_install(args.package)
    if result.get("resend"):
        if result["status"] == "ready":
            plain = ["resend fare_package=%s/%s/%s status=%s; fare tables unchanged"
                     % (result["feed_key"], result["edition"], result["fare_version"],
                        result["status"]),
                     "content sha256=%s" % result["content_sha256"]]
        else:
            plain = ["resend of previously REJECTED fare_package=%s/%s/%s (%d problem(s))"
                     % (result["feed_key"], result["edition"], result["fare_version"],
                        len(result.get("problems", [])))]
        code = 0 if result["status"] == "ready" else 1
        _out(result, args.json, plain)
        return code
    if result["status"] == "ready":
        _out(result, args.json, [
            "installed fare_package=%s/%s/%s status=ready currencies=%s"
            % (result["feed_key"], result["edition"], result["fare_version"],
               ",".join(result["currencies"])),
            "content sha256=%s" % result["content_sha256"],
            "stop_zones=%d products=%d rules=%d"
            % (result["stop_zone_rows"], result["products"], result["rules"]),
        ])
        return 0
    _out(result, args.json, [
        "REJECTED fare_package=%s/%s/%s (%d problem(s)); no fare version made available"
        % (result["feed_key"], result["edition"], result["fare_version"],
           len(result["problems"]))])
    for problem in result["problems"]:
        print("  %s | %s | %s | %s"
              % (problem.get("file") or "-", problem.get("record_id") or "-",
                 problem.get("field") or "-", problem["message"]))
    return 1


def cmd_fare_packages(args, store: Store) -> int:
    rows = _fares(store).fare_packages(args.feed_key, args.edition)
    plain = ["%s/%s/%s status=%s currencies=%s"
             % (r["feed_key"], r["edition"], r["fare_version"], r["status"],
                r["currency_set"] or "-") for r in rows] or ["no fare packages installed"]
    _out(rows, args.json, plain)
    return 0


def cmd_fare_products(args, store: Store) -> int:
    rows = _fares(store).fare_products(args.feed_key, args.edition, args.fare_version)
    plain = ["%s %s %s validity=%ss max_legs=%d covers=%s"
             % (r["fare_version"], r["product_id"], r["amount_text"],
                r["validity_seconds"], r["max_legs"],
                ",".join(r["covers"]) if r["covers"] else "-") for r in rows] \
        or ["no products found"]
    _out(rows, args.json, plain)
    return 0


def cmd_fare_rules(args, store: Store) -> int:
    rows = _fares(store).fare_rules(args.feed_key, args.edition, args.fare_version)
    plain = ["%s prio=%d route=%s from=%s to=%s -> %s"
             % (r["rule_id"], r["priority"], r["route_id"] or "*",
                r["from_zone"] or "*", r["to_zone"] or "*", r["product_id"])
            for r in rows] or ["no rules found"]
    _out(rows, args.json, plain)
    return 0


def cmd_fare_zones(args, store: Store) -> int:
    rows = _fares(store).fare_stop_zones(args.feed_key, args.edition, args.fare_version)
    plain = ["%s -> %s" % (r["stop_id"], r["zone_id"]) for r in rows] \
        or ["no stop/zone mappings found"]
    _out(rows, args.json, plain)
    return 0


def cmd_fare_rejections(args, store: Store) -> int:
    rows = _fares(store).fare_rejections(args.feed_key, args.edition, args.fare_version)
    plain = ["%s seq=%d %s | %s | %s | %s"
             % (r["fare_version"], r["seq"], r["file_name"] or "-",
                r["record_id"] or "-", r["field"] or "-", r["message"])
            for r in rows] or ["no fare rejections recorded"]
    _out(rows, args.json, plain)
    return 0


def _leg_plain(leg: dict[str, Any]) -> list[str]:
    rule = leg["matched_rule_id"] or "-"
    currency = leg["base_product"]["currency"]
    if leg["paid_product_id"] == leg["base_product_id"] and leg["source_leg_index"] == leg["leg_index"]:
        source = "(bought here)"
    else:
        source = "leg %d" % leg["source_leg_index"]
    reasons = ",".join(leg["purchase_reasons"]) if leg["purchase_reasons"] else "free_transfer"
    return [
        "  leg %d route=%s trip=%s board=%s(seq=%d,%s) alight=%s(seq=%d,%s) rule=%s"
        % (leg["leg_index"], leg["route_id"], leg["trip_id"],
           leg["board_stop_id"], leg["board_sequence"], leg["board_zone"],
           leg["alight_stop_id"], leg["alight_sequence"], leg["alight_zone"], rule),
        "    base_product=%s paid_product=%s source=%s charge=%s %s expires@%d used_legs=%d %s"
        % (leg["base_product_id"], leg["paid_product_id"], source, leg["charge_text"],
           currency, leg["ticket_expires_utc"], leg["used_legs_after"], reasons),
    ]


def cmd_fare_settle(args, store: Store) -> int:
    batch = Path(args.batch).read_bytes()
    result = _fares(store).settle(args.feed_key, args.edition, args.fare_version, batch)
    plain = ["settle against %s/%s/%s content=%s rows=%d"
             % (result["feed_key"], result["edition"], result["fare_version"],
                result["content_sha256"][:12], result["batch_rows"])]
    for problem in result["batch_problems"]:
        plain.append("  BATCH line=%s: %s" % (problem.get("input_line"), problem["message"]))
    for journey in result["journeys"]:
        if journey["status"] == "settled":
            plain.append("journey %s SETTLED currency=%s total=%s"
                         % (journey["journey_id"], journey["currency"], journey["total_text"]))
            for leg in journey["legs"]:
                plain.extend(_leg_plain(leg))
        else:
            plain.append("journey %s REJECTED (no partial total):" % journey["journey_id"])
            for reason in journey["reasons"]:
                where = reason.get("input_line")
                leg_index = reason.get("leg_index")
                plain.append("  line=%s leg=%s %s: %s"
                             % (where if where is not None else "-",
                                leg_index if leg_index is not None else "-",
                                reason["code"], reason["message"]))
    totals = result["summary"]["by_currency"]
    plain.append("summary: settled=%d rejected=%d"
                 % (result["summary"]["settled"], result["summary"]["rejected"]))
    for currency, entry in sorted(totals.items()):
        plain.append("  %s total=%s across %d journey(s)"
                     % (currency, entry["text"], entry["journeys"]))
    # Per-journey business failures are a normal batch outcome (exit 0);
    # batch-level parse problems make the run unusable (exit 1).
    code = 1 if result["batch_problems"] else 0
    _out(result, args.json, plain)
    return code


# ---------------------------------------------------------------------------
# Offline GPS dwell processing
# ---------------------------------------------------------------------------

def _dwell(store: Store):
    from .dwell import DwellStore
    return DwellStore(store)


def _dwell_jsonable(result: dict) -> dict:
    """Strip dataclass instances from a dwell process result for --json."""
    payload = {k: v for k, v in result.items() if k != "runs"}
    runs = []
    for run in result["runs"]:
        item = {k: v for k, v in run.items()
                if k not in ("dwells", "samples", "rows", "binding")}
        item["binding"] = run.get("binding")
        item["dwells"] = [
            {"stop_id": f.stop["stop_id"], "end_reason": f.end_reason,
             "arrival_utc": f.arrival_sample.observed_utc,
             "confirmed_utc": f.confirm_sample.observed_utc,
             "last_observed_utc": f.last_observed_sample.observed_utc,
             "departure_utc": (f.departure_sample.observed_utc
                               if f.departure_sample else None),
             "support_points": len(f.samples),
             "first_sample_id": f.arrival_sample.sample_id,
             "last_inside_sample_id": f.last_observed_sample.sample_id,
             "first_outside_sample_id": (f.departure_sample.sample_id
                                         if f.departure_sample else None)}
            for f in run["dwells"]]
        runs.append(item)
    payload["runs"] = runs
    return payload


def cmd_dwell_process(args, store: Store) -> int:
    from .dwell import DwellParams, DwellStore
    params = DwellParams(args.enter_m, args.exit_m, args.min_dwell_s, args.max_gap_s)
    result = DwellStore(store).process(args.samples, args.bindings, params)
    plain = [
        "dwell job=%d source=%s sha256=%s"
        % (result["job_id"], result["source_file"], result["source_sha256"][:16]),
        "params enter_m=%d exit_m=%d min_dwell_s=%d max_gap_s=%d"
        % (args.enter_m, args.exit_m, args.min_dwell_s, args.max_gap_s),
        "runs total=%d processed=%d failed=%d dwell_facts=%d"
        % (result["total_runs"], result["processed_runs"],
           result["failed_runs"], result["dwell_facts"]),
    ]
    for run in result["runs"]:
        if run["status"] == "failed":
            plain.append("  run=%s FAILED" % run["run_id"])
            for diag in run["diagnostics"]:
                plain.append("    line=%s %s: %s"
                             % (diag.get("source_line") if diag.get("source_line") is not None else "-",
                                diag["code"], diag["message"]))
        else:
            index = run["index"]
            plain.append(
                "  run=%s OK dwells=%d indexed_stops=%d candidate_checks=%d cell_scans=%d"
                % (run["run_id"], len(run["dwells"]), index["indexed_stops"],
                   index["candidate_checks"], index["cell_scans"]))
    _out(_dwell_jsonable(result) if args.json else result, args.json, plain)
    return 0


def cmd_dwell_jobs(args, store: Store) -> int:
    rows = _dwell(store).jobs()
    plain = ["job=%d runs=%d/%d failed=%d dwells=%d source=%s created=%s"
             % (r["job_id"], r["processed_runs"], r["total_runs"], r["failed_runs"],
                r["dwell_facts"], r["source_file"], r["created_at"])
             for r in rows] or ["no dwell jobs recorded"]
    _out(rows, args.json, plain)
    return 0


def cmd_dwell_job(args, store: Store) -> int:
    detail = _dwell(store).job(args.job_id)
    plain = [
        "job=%d source=%s enter_m=%d exit_m=%d min_dwell_s=%d max_gap_s=%d"
        % (detail["job_id"], detail["source_file"], detail["enter_m"],
           detail["exit_m"], detail["min_dwell_s"], detail["max_gap_s"]),
        "runs total=%d processed=%d failed=%d dwell_facts=%d"
        % (detail["total_runs"], detail["processed_runs"],
           detail["failed_runs"], detail["dwell_facts"]),
    ]
    for binding in detail["bindings"]:
        plain.append("  bind run=%s -> %s/%s trip=%s (line %d)"
                     % (binding["run_id"], binding["feed_key"], binding["edition"],
                        binding["trip_id"], binding["source_line"]))
    for run in detail["runs"]:
        plain.append("  run=%s status=%s samples=%d dwells=%d"
                     % (run["run_id"], run["status"], run["sample_rows"],
                        run["dwell_count"]))
    _out(detail, args.json, plain)
    return 0


def cmd_dwell_run(args, store: Store) -> int:
    detail = _dwell(store).run_detail(args.job_id, args.run_id)
    plain = ["job=%d run=%s status=%s samples=%d dwells=%d"
             % (args.job_id, args.run_id, detail["status"],
                len(detail["samples"]), len(detail["dwells"]))]
    if detail["index"]:
        idx = detail["index"]
        plain.append("  index %s cell_deg=%.8f cells=%d indexed_stops=%d "
                     "candidate_checks=%d cell_scans=%d antimeridian_safe=%s pole_safe=%s"
                     % (idx["index"], idx["cell_size_degrees"], idx["occupied_cells"],
                        idx["indexed_stops"], idx["candidate_checks"], idx["cell_scans"],
                        idx["antimeridian_safe"], idx["pole_safe"]))
    for diag in detail["diagnostics"]:
        plain.append("  DIAG line=%s %s: %s"
                     % (diag.get("source_line") if diag.get("source_line") is not None else "-",
                        diag["code"], diag["message"]))
    for sample in detail["samples"]:
        plain.append("  sample seq=%d id=%s t=%s lat=%s lon=%s line=%d"
                     % (sample["seq"], sample["sample_id"] or "-",
                        sample["observed_utc"] if sample["observed_utc"] is not None else "-",
                        sample["lat"] if sample["lat"] is not None else "-",
                        sample["lon"] if sample["lon"] is not None else "-",
                        sample["source_line"]))
    for dwell in detail["dwells"]:
        plain.append(
            "  dwell_seq=%d stop=%s(%s) arrival=%s confirm=%s last_obs=%s departure=%s "
            "reason=%s support=%d first=%s last_inside=%s first_outside=%s"
            % (dwell["dwell_seq"], dwell["stop_id"], dwell["stop_name"],
               dwell["arrival_utc"], dwell["confirmed_utc"], dwell["last_observed_utc"],
               dwell["departure_utc"] if dwell["departure_utc"] is not None else "-",
               dwell["end_reason"], dwell["support_points"], dwell["first_sample_id"],
               dwell["last_inside_sample_id"],
               dwell["first_outside_sample_id"] or "-"))
    _out(detail, args.json, plain)
    return 0


def cmd_dwell_dwells(args, store: Store) -> int:
    rows = _dwell(store).dwells(args.job_id, args.run_id, args.stop_id)
    plain = ["job=%d: %d dwell fact(s)" % (args.job_id, len(rows))]
    for r in rows:
        plain.append(
            "  seq=%d run=%s %s/%s stop=%s(%s) arrival=%s confirm=%s last_obs=%s "
            "departure=%s reason=%s support=%d"
            % (r["dwell_seq"], r["run_id"], r["feed_key"], r["edition"],
               r["stop_id"], r["stop_name"], r["arrival_utc"], r["confirmed_utc"],
               r["last_observed_utc"],
               r["departure_utc"] if r["departure_utc"] is not None else "-",
               r["end_reason"], r["support_points"]))
    _out(rows, args.json, plain)
    return 0


def cmd_dwell_evidence(args, store: Store) -> int:
    detail = _dwell(store).evidence(args.job_id, args.dwell_seq)
    fact, points = detail["fact"], detail["evidence"]
    plain = [
        "job=%d dwell_seq=%d run=%s stop=%s(%s) %s/%s"
        % (fact["job_id"], fact["dwell_seq"], fact["run_id"], fact["stop_id"],
           fact["stop_name"], fact["feed_key"], fact["edition"]),
        "arrival=%s confirm=%s last_observed=%s departure=%s reason=%s support_points=%d"
        % (fact["arrival_utc"], fact["confirmed_utc"], fact["last_observed_utc"],
           fact["departure_utc"] if fact["departure_utc"] is not None else "-",
           fact["end_reason"], fact["support_points"]),
        "first_sample=%s last_inside_sample=%s first_outside_sample=%s"
        % (fact["first_sample_id"], fact["last_inside_sample_id"],
           fact["first_outside_sample_id"] or "-"),
    ]
    for p in points:
        plain.append("  #%-2d %-13s sample=%-6s t=%-10d lat=%.7f lon=%.7f line=%-3d dist=%.4fm"
                     % (p["evidence_seq"], p["point_role"], p["sample_id"],
                        p["observed_utc"], p["lat"], p["lon"], p["source_line"],
                        p["distance_m"]))
    _out(detail, args.json, plain)
    return 0


# ---------------------------------------------------------------------------
# Anonymized GPS egress (whole-sequence k-anonymity)
# ---------------------------------------------------------------------------

def _anon(store: Store):
    from .anon import AnonStore
    return AnonStore(store)


def _load_levels(args) -> list[dict]:
    """Levels come from one JSON config: either a bare array or {"levels": [...]}."""
    from .anon import AnonError
    try:
        data = json.loads(Path(args.levels_file).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnonError("levels file not found: %s" % args.levels_file) from exc
    except json.JSONDecodeError as exc:
        raise AnonError("levels file is not valid JSON: %s" % exc) from exc
    if isinstance(data, dict) and "levels" in data:
        data = data["levels"]
    if not isinstance(data, list):
        raise AnonError("levels file must contain a JSON array of "
                        "{cell_microdeg, bucket_s} objects")
    return data


def cmd_anon_process(args, store: Store) -> int:
    from .anon import AnonError, AnonStore
    try:
        manifest = Path(args.manifest).read_bytes()
    except FileNotFoundError as exc:
        raise AnonError("manifest file not found: %s" % args.manifest) from exc
    result = _anon(store).process(manifest, args.k, _load_levels(args))
    plain = [
        "anon_job=%d k=%d selected_level=%d cell_microdeg=%d bucket_s=%s outcome=%s"
        % (result["anon_job_id"], result["k"], result["selected_level_index"],
           result["cell_microdeg"], result["bucket_s"], result["final_outcome"]),
        "members=%d published_groups=%d published_trips=%d dropped_trips=%d "
        "distinct_vehicles=%d"
        % (result["member_count"], result["published_groups"],
           result["published_trips"], result["dropped_trips"],
           result["distinct_vehicles"]),
    ]
    for level in result["levels"]:
        plain.append(
            "  level=%d cell=%ds bucket=%ss groups=%d rare=%d retained_trips=%d "
            "dropped_trips=%d retained_vehicles=%d rare_vehicles=%d -> %s"
            % (level["level_index"], level["cell_microdeg"], level["bucket_s"],
               level["total_groups"], level["rare_groups"], level["retained_trips"],
               level["dropped_trips"], level["retained_vehicles"],
               level["rare_vehicles"], level["outcome"]))
    _out(result, args.json, plain)
    return 0


def cmd_anon_jobs(args, store: Store) -> int:
    rows = _anon(store).jobs()
    plain = [
        "anon_job=%d k=%d level=%d groups=%d trips=%d dropped=%d outcome=%s"
        % (r["anon_job_id"], r["k"], r["selected_level_index"],
           r["published_groups"], r["published_trips"], r["dropped_trips"],
           r["final_outcome"]) for r in rows] or ["no anonymization jobs recorded"]
    _out(rows, args.json, plain)
    return 0


def cmd_anon_job(args, store: Store) -> int:
    detail = _anon(store).job(args.anon_job_id)
    plain = [
        "anon_job=%d k=%d selected_level=%d outcome=%s members=%d"
        % (detail["anon_job_id"], detail["k"], detail["selected_level_index"],
           detail["final_outcome"], detail["member_count"]),
    ]
    for level in detail["levels"]:
        plain.append(
            "  level=%d cell_microdeg=%d bucket_s=%d groups=%d rare=%d "
            "retained_trips=%d dropped_trips=%d retained_vehicles=%d "
            "rare_vehicles=%d -> %s"
            % (level["level_index"], level["cell_microdeg"], level["bucket_s"],
               level["total_groups"], level["rare_groups"], level["retained_trips"],
               level["dropped_trips"], level["retained_vehicles"],
               level["rare_vehicles"], level["outcome"]))
    for group in detail["groups"]:
        plain.append(
            "  group internal=%s public=%s trips=%d vehicles=%d status=%s%s"
            % (group["internal_group_no"],
               group["public_group_no"] if group["public_group_no"] is not None else "-",
               group["trip_count"], group["vehicle_count"], group["status"],
               (" " + group["drop_reason"]) if group["drop_reason"] else ""))
    _out(detail, args.json, plain)
    return 0


def cmd_anon_export(args, store: Store) -> int:
    # External reader: fixed columns only, freshly built payloads, no internal
    # row object is reused and no internal identifier leaves the store.
    payload = _anon(store).public_export(args.anon_job_id)
    plain = [
        "anon_job=%d k=%d selected_level=%d columns=%s"
        % (payload["anon_job_id"], payload["k"],
           payload["selected_level_index"], ",".join(payload["columns"])),
    ]
    for group in payload["groups"]:
        plain.append("  public_group_no=%d trips=%d vehicles=%d sequence_len=%d"
                     % (group["public_group_no"], group["trip_count"],
                        group["vehicle_count"], len(group["sequence"])))
    _out(payload, args.json, plain)
    return 0


def cmd_anon_group(args, store: Store) -> int:
    detail = _anon(store).group_detail(args.anon_job_id, args.public_group_no)
    group = detail["group"]
    plain = [
        "anon_job=%d public_group_no=%d internal_group_no=%d trips=%d "
        "distinct_vehicles=%d status=%s"
        % (detail["job_id"], detail["public_group_no"], detail["internal_group_no"],
           group["trip_count"], group["vehicle_count"], group["status"]),
        "coarse sequence (bucket, lat_cell, lon_cell):",
    ]
    for ordinal, triple in enumerate(detail["sequence"], start=1):
        plain.append("  #%-3d %s" % (ordinal, triple))
    for member in detail["members"]:
        plain.append("  member manifest_line=%d job_id=%d run_id=%s "
                     "vehicle_unit=%s status=%s raw_samples=%d"
                     % (member["manifest_line"], member["job_id"], member["run_id"],
                        member["vehicle_unit"], member["status"],
                        len(member["raw_samples"])))
    _out(detail, args.json, plain)
    return 0


def cmd_anon_member(args, store: Store) -> int:
    detail = _anon(store).member_outcome(args.anon_job_id, args.job_id, args.run_id)
    plain = [
        "anon_job=%d member job_id=%d run_id=%s vehicle_unit=%s status=%s"
        % (args.anon_job_id, args.job_id, args.run_id,
           detail["vehicle_unit"], detail["status"]),
        "explanation: " + detail["explanation"],
    ]
    for level in detail["level_ledger"]:
        plain.append(
            "  level=%d groups=%d rare=%d retained_trips=%d dropped_trips=%d -> %s"
            % (level["level_index"], level["total_groups"], level["rare_groups"],
               level["retained_trips"], level["dropped_trips"], level["outcome"]))
    _out(detail, args.json, plain)
    return 0


# ---------------------------------------------------------------------------
# Weighted two-dimension hierarchical rollup (self-contained result DB)
# ---------------------------------------------------------------------------

def _frac_text(frac: dict | None) -> str:
    if frac is None:
        return "null"
    if frac["den"] == "1":
        return frac["num"]
    return "%s/%s" % (frac["num"], frac["den"])


def _coord_plain(name: str, coord: dict) -> str:
    if coord["kind"] == "all":
        return "%s=*" % name
    if coord["kind"] == "null":
        return "%s=NULL" % name
    return '%s="%s"' % (name, coord["value"])


def _metrics_plain(metrics: dict) -> str:
    return ("row_weight=%s value_weight=%s amount_sum=%s amount_mean=%s "
            "vehicle_count=%d"
            % (_frac_text(metrics["row_weight"]), _frac_text(metrics["value_weight"]),
               _frac_text(metrics["amount_sum"]), _frac_text(metrics["amount_mean"]),
               metrics["vehicle_count"]))


def _rollup_coord(args, prefix: str):
    from .rollup import Coord
    kind = getattr(args, "%s_kind" % prefix)
    value = getattr(args, "%s_value" % prefix)
    if kind is None:
        if value is not None:
            raise GtfsError("--%s-value requires --%s-kind" % (prefix, prefix))
        return None
    if kind == "member" and not value:
        raise GtfsError("--%s-kind member requires --%s-value" % (prefix, prefix))
    if kind in ("null", "all") and value is not None:
        raise GtfsError("--%s-kind %s takes no --%s-value" % (prefix, kind, prefix))
    return Coord(kind, value)


def cmd_rollup_build(args) -> int:
    from .rollup import build
    result = build(args.config, args.facts, args.out,
                   batch_size=args.batch_size, cache_kib=args.cache_kib)
    _out(result, args.json, [
        "built result=%s facts=%d grains=%d cells=%d contributions=%d"
        % (result["result"], result["facts"], result["grains"],
           result["cells"], result["contributions"]),
        "config sha256=%s" % result["config_sha256"],
        "facts sha256=%s" % result["facts_sha256"],
        "batch_size=%d cache_kib=%d" % (result["batch_size"], result["cache_kib"]),
    ])
    return 0


def cmd_rollup_info(args) -> int:
    from .rollup import RollupResult
    with RollupResult(args.result) as result:
        info = result.info()
    plain = [
        "result=%s status=%s created_at=%s"
        % (info["result"], info["status"], info["created_at"]),
        "facts=%d cells=%d contributions=%d grains=%d"
        % (info["fact_rows"], info["cell_rows"], info["contribution_rows"],
           info["grain_count"]),
        "config=%s sha256=%s" % (info["config_file"], info["config_sha256"]),
        "facts=%s sha256=%s" % (info["facts_file"], info["facts_sha256"]),
        "dimensions=%s batch_size=%d cache_kib=%d"
        % (",".join(info["dimensions"]), info["batch_size"], info["cache_kib"]),
    ]
    for grain in info["grains"]:
        plain.append("  grain=%s levels=%s cells=%d"
                     % (grain["name"], grain["levels"], grain["cells"]))
    _out(info, args.json, plain)
    return 0


def cmd_rollup_grains(args) -> int:
    from .rollup import RollupResult
    with RollupResult(args.result) as result:
        grains = result.grains()
    plain = ["grain=%s levels=%s cells=%d"
             % (g["name"], g["levels"], g["cells"]) for g in grains]
    _out(grains, args.json, plain)
    return 0


def cmd_rollup_cells(args) -> int:
    from .rollup import RollupResult
    d1 = _rollup_coord(args, "d1")
    d2 = _rollup_coord(args, "d2")
    with RollupResult(args.result) as result:
        cells = result.cells(args.grain, d1=d1, d2=d2,
                             limit=args.limit, offset=args.offset)
    plain = ["grain=%s cells=%d" % (args.grain, len(cells))]
    for cell in cells:
        coords = " ".join(_coord_plain(name, coord)
                          for name, coord in cell["coords"].items())
        plain.append("[%s] %s" % (coords, _metrics_plain(cell["metrics"])))
    _out(cells, args.json, plain)
    return 0


def cmd_rollup_drill(args) -> int:
    from .rollup import RollupResult
    d1 = _rollup_coord(args, "d1")
    d2 = _rollup_coord(args, "d2")
    if d1 is None or d2 is None:
        raise GtfsError("drill needs --d1-kind and --d2-kind to name one cell")
    with RollupResult(args.result) as result:
        detail = result.drill(args.grain, d1, d2,
                              offset=args.offset, limit=args.limit)
    coords = " ".join(_coord_plain(name, coord)
                      for name, coord in detail["coords"].items())
    plain = [
        "grain=%s cell=[%s] contributions=%d (page offset=%d limit=%d)"
        % (detail["grain"], coords, detail["total_contributions"],
           detail["offset"], detail["limit"]),
        "metrics: " + _metrics_plain(detail["metrics"]),
    ]
    for row in detail["rows"]:
        dims = " ".join("%s=%s" % (name, json.dumps(value, ensure_ascii=False))
                        for name, value in row["dimensions"].items())
        plain.append("  line=%d %s vehicle_unit=%s amount=%s coeff=%s"
                     % (row["line_no"], dims,
                        json.dumps(row["vehicle_unit"], ensure_ascii=False),
                        row["amount"], _frac_text(row["coefficient"])))
    _out(detail, args.json, plain)
    return 0


def cmd_rollup_fact(args) -> int:
    from .rollup import RollupResult
    with RollupResult(args.result) as result:
        fact = result.fact(args.line)
    dims = " ".join("%s=%s" % (name, json.dumps(value, ensure_ascii=False))
                    for name, value in fact["dimensions"].items())
    _out(fact, args.json, [
        "line=%d %s vehicle_unit=%s amount=%s"
        % (fact["line_no"], dims,
           json.dumps(fact["vehicle_unit"], ensure_ascii=False), fact["amount"]),
    ])
    return 0


# ---------------------------------------------------------------------------
# Realtime channel (needs gtfs-realtime-bindings; imported lazily)
# ---------------------------------------------------------------------------

def _rt(store: Store):
    from .realtime import RealtimeStore
    return RealtimeStore(store)


def cmd_rt_register(args, store: Store) -> int:
    result = _rt(store).register_source(args.source, args.feed_key, args.edition)
    if result["already_registered"]:
        _out(result, args.json, [
            "source=%s already registered with binding %s/%s; unchanged"
            % (result["source_key"], result["feed_key"], result["edition"])])
    else:
        _out(result, args.json, [
            "registered source=%s bound to %s/%s (binding is permanent)"
            % (result["source_key"], result["feed_key"], result["edition"])])
    return 0


def cmd_rt_sources(args, store: Store) -> int:
    rows = _rt(store).sources()
    plain = ["%s bound=%s/%s head=%s baseline=%s entities=%d receptions=%d"
             % (r["source_key"], r["feed_key"], r["edition"],
                r["head_timestamp"] if r["head_timestamp"] is not None else "-",
                "yes" if r["baseline_seen"] else "no", r["entities"], r["receptions"])
             for r in rows] or ["no realtime sources registered"]
    _out(rows, args.json, plain)
    return 0


def _diag_lines(diagnostics: list[dict[str, Any]]) -> list[str]:
    lines = []
    for d in diagnostics:
        where = d.get("entity_id") or "-"
        field = d.get("field") or "-"
        lines.append("  %s | entity=%s | field=%s | %s"
                     % (d["category"], where, field, d["message"]))
    return lines


def cmd_rt_feed(args, store: Store) -> int:
    blob = Path(args.message).read_bytes()
    result = _rt(store).feed(args.source, blob)
    category = result["category"]
    if category == "applied":
        plain = ["applied source=%s reception=%d %s ts=%d upserts=%d withdrawals=%d "
                 "stop_events=%d"
                 % (result["source_key"], result["reception_id"], result["incrementality"],
                    result["header_timestamp"], result["entities_upserted"],
                    result["entities_withdrawn"], result["stop_events"])]
        code = 0
    elif category == "duplicate":
        plain = ["duplicate source=%s reception=%d: identical to reception #%d "
                 "(%s); stored conclusion stands, nothing applied again"
                 % (result["source_key"], result["reception_id"],
                    result["original_reception_id"], result["original_category"])]
        code = 0
    elif category == "late":
        plain = ["late source=%s reception=%d ts=%s: behind the source head; "
                 "recorded only" % (result["source_key"], result["reception_id"],
                                    result["header_timestamp"])]
        code = 0
    else:  # rejected / conflict
        heading = ("CONFLICT" if category == "conflict" else "REJECTED")
        plain = ["%s source=%s reception=%d (%d problem(s)); current set unchanged"
                 % (heading, result["source_key"], result["reception_id"],
                    len(result["diagnostics"]))]
        plain += _diag_lines(result["diagnostics"])
        code = 1
    _out(result, args.json, plain)
    return code


def _event_text(event: dict[str, Any] | None) -> str:
    if event is None:
        return "-"
    def val(key: str) -> str:
        return "-" if event[key] is None else str(event[key])
    return "time=%s delay=%s unc=%s" % (val("time"), val("delay"), val("uncertainty"))


def cmd_rt_notices(args, store: Store) -> int:
    result = _rt(store).notices(args.source, args.now, args.max_age,
                                include_expired=args.include_expired)
    notices = result["notices"]
    if args.entity is not None:
        notices = [n for n in notices if n["entity_id"] == args.entity]
        result = dict(result, notices=notices)
    plain = ["source=%s now=%d max_age=%d include_expired=%s head=%s: %d notice(s)"
             % (result["source_key"], result["now"], result["max_age"],
                "yes" if result["include_expired"] else "no",
                result["head_timestamp"] if result["head_timestamp"] is not None else "-",
                len(notices))]
    for n in notices:
        plain.append(
            "%s trip=%s date=%s start=%s rel=%s entity_ts=%s replaced@%d reception=#%d "
            "fresh=%s expires@%d"
            % (n["entity_id"], n["trip_id"], n["service_date"],
               n["start_time"] or "-", n["trip_schedule_relationship"],
               n["entity_timestamp"] if n["entity_timestamp"] is not None else "-",
               n["replaced_at_header_timestamp"], n["reception_id"],
               "yes" if n["fresh"] else "NO(expired)", n["expires_at"]))
        for s in n["stop_events"]:
            plain.append("  seq %-4d stop=%s static=%s %-9s arr[%s] dep[%s]"
                         % (s["stop_sequence"], s["stop_id"] or "-",
                            s["static_stop_id"] or "-", s["schedule_relationship"],
                            _event_text(s["arrival"]), _event_text(s["departure"])))
    _out(result, args.json, plain)
    return 0


def cmd_rt_receptions(args, store: Store) -> int:
    rows = _rt(store).receptions(args.source, args.limit)
    plain = ["source=%s: %d reception(s)" % (args.source, len(rows))]
    for r in rows:
        plain.append("#%d %s %-9s %-13s ts=%s sha=%s… size=%d"
                     % (r["id"], r["received_at"], r["category"],
                        r["header_incrementality"] or "-",
                        r["header_timestamp"] if r["header_timestamp"] is not None else "-",
                        r["sha256"][:12], r["size_bytes"]))
        plain += _diag_lines(r["diagnostics"])
    _out(rows, args.json, plain)
    return 0


def cmd_rt_reception(args, store: Store) -> int:
    rt = _rt(store)
    detail = rt.reception_detail(args.id, include_blob=args.blob is not None)
    if args.blob is not None:
        Path(args.blob).write_bytes(detail.pop("blob"))
    plain = ["reception #%d source=%s category=%s applied=%s"
             % (detail["id"], detail["source_key"], detail["category"], detail["applied"]),
             "received_at=%s sha256=%s size=%d header_ts=%s incrementality=%s"
             % (detail["received_at"], detail["sha256"], detail["size_bytes"],
                detail["header_timestamp"], detail["header_incrementality"])]
    if detail["original_reception_id"] is not None:
        plain.append("original_reception=#%d" % detail["original_reception_id"])
    if args.blob is not None:
        plain.append("blob written to %s" % args.blob)
    plain += _diag_lines(detail["diagnostics"])
    _out(detail, args.json, plain)
    return 0


# ---------------------------------------------------------------------------
# Offline J1939 TP reassembly
# ---------------------------------------------------------------------------

def _j1939(store: Store):
    from .j1939 import J1939Store
    return J1939Store(store)


def cmd_j1939_import(args, store: Store) -> int:
    result = _j1939(store).import_capture(args.capture, args.data_gap_ns,
                                          args.control_gap_ns)
    plain = [
        "j1939_batch=%d source=%s sha256=%s frames=%d"
        % (result["batch_id"], result["source_file"],
           result["source_sha256"][:16], result["total_frames"]),
        "gaps data_gap_ns=%d control_gap_ns=%d"
        % (result["data_gap_ns"], result["control_gap_ns"]),
        "transfers total=%d completed=%d messages=%d problem_events=%d"
        % (result["total_transfers"], result["completed_transfers"],
           result["published_messages"], result["problem_events"]),
        "state counts: " + ", ".join(
            "%s=%d" % kv for kv in sorted(result["state_counts"].items())),
    ]
    if result["all_succeeded"]:
        plain.append("outcome: all transfers completed")
        code = 0
    else:
        plain.append("outcome: NOT all transfers succeeded; inspect with "
                     "j1939-transfer and j1939-events")
        code = 1
    _out(result, args.json, plain)
    return code


def cmd_j1939_batches(args, store: Store) -> int:
    rows = _j1939(store).batches()
    plain = ["batch=%d frames=%d transfers=%d/%d messages=%d problems=%d "
             "all_succeeded=%s"
             % (r["batch_id"], r["total_frames"], r["completed_transfers"],
                r["total_transfers"], r["published_messages"],
                r["problem_events"], r["all_succeeded"])
             for r in rows] or ["no j1939 batches imported"]
    _out(rows, args.json, plain)
    return 0


def cmd_j1939_messages(args, store: Store) -> int:
    rows = _j1939(store).messages(args.batch, args.bus)
    plain = ["#%d batch=%d %s %d->%d pgn=0x%05X mode=%s %d bytes %d..%d"
             % (r["message_id"], r["batch_id"], r["bus"], r["src_addr"],
                r["dst_addr"], r["app_pgn"], r["mode"], r["length_bytes"],
                r["start_ns"], r["end_ns"]) for r in rows] \
        or ["no reassembled messages"]
    if not args.json:
        for line in plain:
            print(line)
        return 0
    _out(rows, args.json)
    return 0


def cmd_j1939_message(args, store: Store) -> int:
    detail = _j1939(store).message(args.message_id)
    plain = [
        "message #%d batch=%d %s %d->%d pgn=0x%05X mode=%s length=%d"
        % (detail["message_id"], detail["batch_id"], detail["bus"],
           detail["src_addr"], detail["dst_addr"], detail["app_pgn"],
           detail["mode"], detail["length_bytes"]),
        "start_ns=%d end_ns=%d" % (detail["start_ns"], detail["end_ns"]),
        "payload_hex=%s" % detail["payload_hex"],
        "byte sources:",
    ]
    for s in detail["sources"]:
        plain.append("  packet=%d frame_id=%d line=%d payload_offset=%d "
                     "frame_offset=%d data_len=%d data_hex=%s"
                     % (s["packet_seq"], s["frame_id"], s["source_line"],
                        s["payload_offset"], s["frame_offset"], s["data_len"],
                        s["data_hex"]))
    _out(detail, args.json, plain)
    return 0


def cmd_j1939_transfers(args, store: Store) -> int:
    rows = _j1939(store).transfers(args.batch, args.status)
    plain = ["#%d batch=%d %s %d->%d mode=%s pgn=0x%05X state=%s %d/%d packets"
             % (r["transfer_id"], r["batch_id"], r["bus"], r["src_addr"],
                r["dst_addr"], r["mode"], r["app_pgn"], r["state"],
                r["next_seq"] - 1, r["total_packets"]) for r in rows] \
        or ["no transfers recorded"]
    _out(rows, args.json, plain)
    return 0


def cmd_j1939_transfer(args, store: Store) -> int:
    detail = _j1939(store).transfer(args.transfer_id)
    plain = [
        "transfer #%d batch=%d %s %d->%d mode=%s app_pgn=0x%05X state=%s"
        % (detail["transfer_id"], detail["batch_id"], detail["bus"],
           detail["src_addr"], detail["dst_addr"], detail["mode"],
           detail["app_pgn"], detail["state"]),
        "announced %d byte(s) in %d packet(s); received %d; missing=%s"
        % (detail["total_bytes"], detail["total_packets"],
           detail["total_packets"] - len(detail["missing_packet_seqs"]),
           detail["missing_packet_seqs"] or "none"),
        "start_ns=%s end_ns=%s deadline_ns=%s failure_code=%s abort_reason=%s"
        % (detail["start_ns"], detail["end_ns"], detail["deadline_ns"],
           detail["failure_code"] or "-",
           detail["abort_reason"] if detail["abort_reason"] is not None else "-"),
        "controls (RTS/CTS/ACK raw eight bytes):",
    ]
    for c in detail["controls"]:
        plain.append("  %s line=%d can_id=%s data=%s"
                     % (c["control_type"], c["source_line"],
                        c["can_id_text"], c["data_hex"]))
    plain.append("packet sources:")
    for p in detail["packets"]:
        plain.append("  packet=%d line=%d data=%s"
                     % (p["packet_seq"], p["source_line"], p["data_hex"]))
    plain.append("event ledger:")
    for e in detail["events"]:
        plain.append("  #%d %s line=%s: %s"
                     % (e["event_seq"], e["event_code"],
                        e["source_line"] if e["source_line"] is not None else "-",
                        e["message"]))
    _out(detail, args.json, plain)
    return 0


def cmd_j1939_events(args, store: Store) -> int:
    rows = _j1939(store).events(args.batch)
    plain = ["#%d transfer=%s line=%s %s: %s"
             % (r["event_seq"], r["transfer_id"] if r["transfer_id"] is not None
                else "-", r["source_line"] if r["source_line"] is not None
                else "-", r["event_code"], r["message"]) for r in rows] \
        or ["no protocol events recorded"]
    _out(rows, args.json, plain)
    return 0


def cmd_j1939_trace(args, store: Store) -> int:
    result = _j1939(store).trace_bytes(args.message_id, args.start, args.end)
    plain = ["trace message #%d payload [%d, %d) of %d byte(s): %d frame hit(s)"
             % (result["message_id"], result["start"], result["end"],
                result["length_bytes"], len(result["hits"]))]
    for h in result["hits"]:
        plain.append("  packet=%d line=%d %s can_id=%s payload[%d:%d] -> "
                     "frame_offset[%d:%d] bytes=%s"
                     % (h["packet_seq"], h["source_line"], h["bus"],
                        h["can_id_text"], h["payload_start"], h["payload_end"],
                        h["frame_offset_start"], h["frame_offset_end"],
                        h["frame_bytes_hex"]))
    _out(result, args.json, plain)
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

    p = sub.add_parser("fare-install",
                       help="validate and atomically install a fare rule package")
    p.add_argument("--package", required=True, help="path to the fare .zip package")
    p.set_defaults(func=cmd_fare_install)

    p = sub.add_parser("fare-packages", help="list installed fare packages")
    p.add_argument("--feed-key")
    p.add_argument("--edition")
    p.set_defaults(func=cmd_fare_packages)

    p = sub.add_parser("fare-products", help="read fare products and coverage sets")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--fare-version", required=True)
    p.set_defaults(func=cmd_fare_products)

    p = sub.add_parser("fare-rules", help="read selection rules of a fare package")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--fare-version", required=True)
    p.set_defaults(func=cmd_fare_rules)

    p = sub.add_parser("fare-zones", help="read stop-to-zone mappings of a fare package")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--fare-version", required=True)
    p.set_defaults(func=cmd_fare_zones)

    p = sub.add_parser("fare-rejections", help="show fare package refusal reasons")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--fare-version")
    p.set_defaults(func=cmd_fare_rejections)

    p = sub.add_parser("fare-settle", help="clear one journey batch CSV against a fare package")
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.add_argument("--fare-version", required=True)
    p.add_argument("--batch", required=True, help="CSV file of journey legs")
    p.set_defaults(func=cmd_fare_settle)

    p = sub.add_parser("rt-register",
                       help="bind a realtime source to one ready edition (permanent)")
    p.add_argument("--source", required=True)
    p.add_argument("--feed-key", required=True)
    p.add_argument("--edition", required=True)
    p.set_defaults(func=cmd_rt_register)

    p = sub.add_parser("rt-sources", help="list realtime sources and their heads")
    p.set_defaults(func=cmd_rt_sources)

    p = sub.add_parser("rt-feed", help="feed one downloaded FeedMessage (.pb) to a source")
    p.add_argument("--source", required=True)
    p.add_argument("--message", required=True, help="path to the .pb file")
    p.set_defaults(func=cmd_rt_feed)

    p = sub.add_parser("rt-notices", help="read current notices of a source")
    p.add_argument("--source", required=True)
    p.add_argument("--now", required=True, type=int,
                   help="caller clock, epoch seconds")
    p.add_argument("--max-age", required=True, type=int,
                   help="positive freshness horizon in seconds")
    p.add_argument("--include-expired", action="store_true",
                   help="also show expired notices (now must not be behind the head)")
    p.add_argument("--entity", help="restrict to one entity id")
    p.set_defaults(func=cmd_rt_notices)

    p = sub.add_parser("rt-receptions", help="show the reception ledger of a source")
    p.add_argument("--source", required=True)
    p.add_argument("--limit", type=int, default=None,
                   help="show only the most recent N receptions")
    p.set_defaults(func=cmd_rt_receptions)

    p = sub.add_parser("rt-reception", help="show one reception; optionally dump its blob")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--blob", help="write the original message bytes to this path")
    p.set_defaults(func=cmd_rt_reception)

    # Offline GPS dwell processing -------------------------------------
    p = sub.add_parser("dwell-process",
                       help="process one GPS sample file with run bindings into dwell facts")
    p.add_argument("--samples", required=True, help="CSV: run_id,sample_id,observed_utc,lat,lon")
    p.add_argument("--bindings", required=True,
                   help="CSV: run_id,feed_key,edition,trip_id")
    p.add_argument("--enter-m", required=True, type=int, dest="enter_m")
    p.add_argument("--exit-m", required=True, type=int, dest="exit_m")
    p.add_argument("--min-dwell-s", required=True, type=int, dest="min_dwell_s")
    p.add_argument("--max-gap-s", required=True, type=int, dest="max_gap_s")
    p.set_defaults(func=cmd_dwell_process)

    p = sub.add_parser("dwell-jobs", help="list recorded dwell jobs")
    p.set_defaults(func=cmd_dwell_jobs)

    p = sub.add_parser("dwell-job", help="show one dwell job: bindings and per-run outcomes")
    p.add_argument("--job-id", required=True, type=int, dest="job_id")
    p.set_defaults(func=cmd_dwell_job)

    p = sub.add_parser("dwell-run", help="show one run: diagnostics, index stats, samples, dwells")
    p.add_argument("--job-id", required=True, type=int, dest="job_id")
    p.add_argument("--run-id", required=True, dest="run_id")
    p.set_defaults(func=cmd_dwell_run)

    p = sub.add_parser("dwell-dwells", help="list dwell facts of a job, optionally per run/stop")
    p.add_argument("--job-id", required=True, type=int, dest="job_id")
    p.add_argument("--run-id", dest="run_id")
    p.add_argument("--stop-id", dest="stop_id")
    p.set_defaults(func=cmd_dwell_dwells)

    p = sub.add_parser("dwell-evidence",
                       help="expand one dwell fact into its raw supporting samples and lines")
    p.add_argument("--job-id", required=True, type=int, dest="job_id")
    p.add_argument("--dwell-seq", required=True, type=int, dest="dwell_seq")
    p.set_defaults(func=cmd_dwell_evidence)

    # Anonymized GPS egress --------------------------------------------
    p = sub.add_parser("anon-process",
                       help="coarsen registered GPS runs into k-supported cell sequences")
    p.add_argument("--manifest", required=True,
                   help="CSV: job_id,run_id,vehicle_unit (each instance once)")
    p.add_argument("--k", required=True, type=int, help="minimum distinct vehicles (>= 2)")
    p.add_argument("--levels-file", required=True, dest="levels_file",
                   help="JSON array of {cell_microdeg, bucket_s}, fine to coarse")
    p.set_defaults(func=cmd_anon_process)

    p = sub.add_parser("anon-jobs", help="list anonymization jobs (internal audit)")
    p.set_defaults(func=cmd_anon_jobs)

    p = sub.add_parser("anon-job", help="show one anonymization job ledger (internal audit)")
    p.add_argument("--anon-job-id", required=True, type=int, dest="anon_job_id")
    p.set_defaults(func=cmd_anon_job)

    p = sub.add_parser("anon-export",
                       help="public export: fixed columns, retained groups only")
    p.add_argument("--anon-job-id", required=True, type=int, dest="anon_job_id")
    p.set_defaults(func=cmd_anon_export)

    p = sub.add_parser("anon-group",
                       help="internal: expand one public group to members and raw rows")
    p.add_argument("--anon-job-id", required=True, type=int, dest="anon_job_id")
    p.add_argument("--public-group-no", required=True, type=int, dest="public_group_no")
    p.set_defaults(func=cmd_anon_group)

    p = sub.add_parser("anon-member",
                       help="internal: explain why one registered member was kept or dropped")
    p.add_argument("--anon-job-id", required=True, type=int, dest="anon_job_id")
    p.add_argument("--job-id", required=True, type=int, dest="job_id")
    p.add_argument("--run-id", required=True, dest="run_id")
    p.set_defaults(func=cmd_anon_member)

    # Offline J1939 TP reassembly --------------------------------------
    p = sub.add_parser("j1939-import",
                       help="reassemble one CAN capture CSV into J1939 TP messages")
    p.add_argument("--capture", required=True,
                   help="UTF-8 CSV: bus,timestamp_ns,can_id,data_hex")
    p.add_argument("--data-gap-ns", required=True, type=int, dest="data_gap_ns",
                   help="positive inter-packet deadline in nanoseconds")
    p.add_argument("--control-gap-ns", required=True, type=int,
                   dest="control_gap_ns",
                   help="positive RTS/CTS/ACK deadline in nanoseconds")
    p.set_defaults(func=cmd_j1939_import)

    p = sub.add_parser("j1939-batches", help="list imported J1939 capture batches")
    p.set_defaults(func=cmd_j1939_batches)

    p = sub.add_parser("j1939-messages", help="list reassembled application messages")
    p.add_argument("--batch", type=int)
    p.add_argument("--bus")
    p.set_defaults(func=cmd_j1939_messages)

    p = sub.add_parser("j1939-message", help="show one message, payload and byte sources")
    p.add_argument("--message-id", required=True, type=int, dest="message_id")
    p.set_defaults(func=cmd_j1939_message)

    p = sub.add_parser("j1939-transfers", help="list TP transfers and their states")
    p.add_argument("--batch", type=int)
    p.add_argument("--status", help="filter one terminal/active state")
    p.set_defaults(func=cmd_j1939_transfers)

    p = sub.add_parser("j1939-transfer",
                       help="inspect one transfer: events, controls, packets, gaps")
    p.add_argument("--transfer-id", required=True, type=int, dest="transfer_id")
    p.set_defaults(func=cmd_j1939_transfer)

    p = sub.add_parser("j1939-events", help="list protocol diagnostics of a batch")
    p.add_argument("--batch", type=int)
    p.set_defaults(func=cmd_j1939_events)

    p = sub.add_parser("j1939-trace",
                       help="trace a [start,end) payload interval back to DT frames")
    p.add_argument("--message-id", required=True, type=int, dest="message_id")
    p.add_argument("--start", required=True, type=int,
                   help="inclusive payload byte offset")
    p.add_argument("--end", required=True, type=int,
                   help="exclusive payload byte offset")
    p.set_defaults(func=cmd_j1939_trace)

    # Weighted two-dimension hierarchical rollup ---------------------------
    # These commands use their own result database (--out / --result) and
    # never touch the main --db store.
    p = sub.add_parser("rollup-build",
                       help="build one rollup result DB from a config and a JSONL fact file")
    p.add_argument("--config", required=True, help="rollup config JSON (read-only input)")
    p.add_argument("--facts", required=True, help="JSONL fact file (read-only input)")
    p.add_argument("--out", required=True,
                   help="result SQLite path; must not exist (never overwritten)")
    p.add_argument("--batch-size", type=int, default=4096, dest="batch_size",
                   help="facts per transaction / accumulator flush (default: 4096)")
    p.add_argument("--cache-kib", type=int, default=16384, dest="cache_kib",
                   help="SQLite page cache for the build, in KiB (default: 16384)")
    p.set_defaults(func=cmd_rollup_build, uses_store=False)

    p = sub.add_parser("rollup-info", help="show build metadata and grains of a result DB")
    p.add_argument("--result", required=True, help="rollup result SQLite path")
    p.set_defaults(func=cmd_rollup_info, uses_store=False)

    p = sub.add_parser("rollup-grains", help="list the grains stored in a result DB")
    p.add_argument("--result", required=True, help="rollup result SQLite path")
    p.set_defaults(func=cmd_rollup_grains, uses_store=False)

    p = sub.add_parser("rollup-cells", help="read occupied cells of one grain")
    p.add_argument("--result", required=True, help="rollup result SQLite path")
    p.add_argument("--grain", required=True, help="grain name")
    p.add_argument("--d1-kind", choices=["member", "null", "all"], dest="d1_kind",
                   help="filter dimension 1 by coordinate kind")
    p.add_argument("--d1-value", dest="d1_value",
                   help="member id for --d1-kind member (e.g. ALL or 007)")
    p.add_argument("--d2-kind", choices=["member", "null", "all"], dest="d2_kind",
                   help="filter dimension 2 by coordinate kind")
    p.add_argument("--d2-value", dest="d2_value",
                   help="member id for --d2-kind member")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_rollup_cells, uses_store=False)

    p = sub.add_parser("rollup-drill",
                       help="page one cell's contribution ledger by source line")
    p.add_argument("--result", required=True, help="rollup result SQLite path")
    p.add_argument("--grain", required=True, help="grain name")
    p.add_argument("--d1-kind", required=True, choices=["member", "null", "all"],
                   dest="d1_kind")
    p.add_argument("--d1-value", dest="d1_value")
    p.add_argument("--d2-kind", required=True, choices=["member", "null", "all"],
                   dest="d2_kind")
    p.add_argument("--d2-value", dest="d2_value")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_rollup_drill, uses_store=False)

    p = sub.add_parser("rollup-fact", help="show one stored fact by physical line number")
    p.add_argument("--result", required=True, help="rollup result SQLite path")
    p.add_argument("--line", required=True, type=int, help="1-based physical line number")
    p.set_defaults(func=cmd_rollup_fact, uses_store=False)
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
        if not getattr(args, "uses_store", True):
            return args.func(args)
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
