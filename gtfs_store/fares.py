"""Offline fare clearing: installable fare packages and per-leg settlement.

The module deliberately does three things only:

1. Install and version fare rule packages (``fare_install``), each bound to
   one exact static ``(feed_key, edition)`` and carrying a fare version.
2. Resolve already-travelled legs against the static tables (trip -> route,
   stop sequences -> stops -> fare zones) and the package's selection rules.
3. Replay the deterministic ticket state machine (coverage set, validity
   window, segment quota) to produce per-leg receivables with the reason for
   every purchase.

It never plans a route, never searches for the cheapest product, never
guesses which trip a passenger took, never touches a payment system and
never rewrites the input timestamps from schedules or realtime data.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Any

from .errors import GtfsError, RejectError
from .farepack import (
    FarePackage,
    find_priority_overlaps,
    format_minor,
    parse_fare_package,
    read_fare_package,
)
from .model import read_csv_bytes
import re

FARE_SCHEMA_VERSION = "1"

FARE_DDL = """
CREATE TABLE IF NOT EXISTS fare_packages (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'rejected')),
    currency_set TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, fare_version)
);

CREATE TABLE IF NOT EXISTS fare_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS fare_stop_zones (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, fare_version, stop_id)
);

CREATE TABLE IF NOT EXISTS fare_products (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
    product_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    amount_text TEXT NOT NULL,
    validity_seconds INTEGER NOT NULL,
    max_legs INTEGER NOT NULL,
    PRIMARY KEY (feed_key, edition, fare_version, product_id)
);

CREATE TABLE IF NOT EXISTS fare_product_covers (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
    product_id TEXT NOT NULL,
    covered_product_id TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, fare_version, product_id, covered_product_id)
);

CREATE TABLE IF NOT EXISTS fare_rules (
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    fare_version TEXT NOT NULL,
    rule_seq INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    priority INTEGER NOT NULL,
    route_id TEXT,
    from_zone TEXT,
    to_zone TEXT,
    product_id TEXT NOT NULL,
    PRIMARY KEY (feed_key, edition, fare_version, rule_seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fare_rules_id
    ON fare_rules (feed_key, edition, fare_version, rule_id);
"""

SETTLE_COLUMNS = (
    "journey_id", "leg_index", "trip_id", "board_sequence", "alight_sequence",
    "board_utc", "alight_utc",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_sha(members: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(members):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(members[name]).digest())
    return digest.hexdigest()


class FareStore:
    """Fare package storage and clearing on top of an existing static Store."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn: sqlite3.Connection = store.conn
        self.conn.executescript(FARE_DDL)
        stored = self.conn.execute(
            "SELECT value FROM meta WHERE key='fare_schema_version'").fetchone()
        if stored is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('fare_schema_version', ?)",
                (FARE_SCHEMA_VERSION,))
        elif stored[0] != FARE_SCHEMA_VERSION:
            raise GtfsError(
                "fare tables were built with schema version %s, this code expects %s"
                % (stored[0], FARE_SCHEMA_VERSION))

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def fare_install(self, package_path: str, raw_bytes: bytes | None = None) -> dict[str, Any]:
        """Validate and atomically install one fare package.

        Same version + same content is an idempotent success.  Same version +
        different content is a hard conflict and never overwrites the stored
        copy.  A rejected package leaves every previously installed version
        untouched.
        """
        if raw_bytes is None:
            with open(package_path, "rb") as fh:
                raw_bytes = fh.read()
        archive_sha = _sha256(raw_bytes)
        members = read_fare_package(package_path, raw_bytes)
        content_sha = _content_sha(members)
        package = parse_fare_package(members)
        package.archive_sha256 = archive_sha
        package.content_sha256 = content_sha

        if package.feed_key and package.edition and package.fare_version:
            existing = self.conn.execute(
                "SELECT content_sha256, status FROM fare_packages WHERE feed_key=? "
                "AND edition=? AND fare_version=?",
                (package.feed_key, package.edition, package.fare_version)).fetchone()
            if existing is not None:
                if existing["content_sha256"] == content_sha:
                    return {
                        "feed_key": package.feed_key, "edition": package.edition,
                        "fare_version": package.fare_version, "status": existing["status"],
                        "archive_sha256": archive_sha, "content_sha256": content_sha,
                        "resend": True,
                        "message": "identical fare package already stored; nothing changed",
                        "problems": self._stored_fare_problems(
                            package.feed_key, package.edition,
                            package.fare_version, content_sha),
                    }
                raise GtfsError(
                    "fare_version %r for %s/%s already exists with different contents "
                    "(stored %s, incoming %s); changing a version is not allowed"
                    % (package.fare_version, package.feed_key, package.edition,
                       existing["content_sha256"], content_sha))

        problems = list(package.problems)
        binding_ok = False
        if package.feed_key and package.edition:
            edition_row = self.conn.execute(
                "SELECT status FROM editions WHERE feed_key=? AND edition=?",
                (package.feed_key, package.edition)).fetchone()
            if edition_row is None:
                problems.append(RejectError(
                    "foreign_key",
                    "bound static edition %s/%s is not loaded"
                    % (package.feed_key, package.edition),
                    file_name="manifest.json", field="edition"))
            elif edition_row["status"] != "ready":
                problems.append(RejectError(
                    "invalid_binding",
                    "bound static edition %s/%s is not ready"
                    % (package.feed_key, package.edition),
                    file_name="manifest.json", field="edition"))
            else:
                binding_ok = True

        if binding_ok and package.stop_zones and package.rules:
            static_stops = {r[0] for r in self.conn.execute(
                "SELECT stop_id FROM stops WHERE feed_key=? AND edition=?",
                (package.feed_key, package.edition))}
            static_routes = {r[0] for r in self.conn.execute(
                "SELECT route_id FROM routes WHERE feed_key=? AND edition=?",
                (package.feed_key, package.edition))}
            for row in package.stop_zones:
                if row["stop_id"] not in static_stops:
                    problems.append(RejectError(
                        "foreign_key",
                        "mapped stop %r does not exist in the bound static edition"
                        % row["stop_id"],
                        file_name="stop_zones.json", field="stop_id",
                        record_id=row["stop_id"]))
            for rule in package.rules:
                if rule["route_id"] is not None and rule["route_id"] not in static_routes:
                    problems.append(RejectError(
                        "foreign_key",
                        "rule %r references route %r, which does not exist in the bound "
                        "static edition" % (rule["rule_id"], rule["route_id"]),
                        file_name="rules.json", field="route_id",
                        record_id=rule["rule_id"]))
            if static_routes and package.zone_ids():
                for conflict in find_priority_overlaps(
                        package.rules, static_routes, package.zone_ids()):
                    problems.append(RejectError(
                        "rule_overlap",
                        "rules %r and %r share priority %d and can both match "
                        "route=%s from_zone=%s to_zone=%s; same-priority ties are not "
                        "resolved by file order"
                        % (conflict["rule_a"], conflict["rule_b"], conflict["priority"],
                           conflict["witness"]["route_id"],
                           conflict["witness"]["from_zone"], conflict["witness"]["to_zone"]),
                        file_name="rules.json",
                        record_id="%s/%s" % (conflict["rule_a"], conflict["rule_b"]),
                        context=conflict))

        result_base = {
            "feed_key": package.feed_key, "edition": package.edition,
            "fare_version": package.fare_version,
            "archive_sha256": archive_sha, "content_sha256": content_sha,
        }
        if problems:
            self._record_fare_rejection(package, problems)
            return {**result_base, "status": "rejected",
                    "problems": [p.to_dict() for p in problems],
                    "message": "fare package rejected; no fare version was made available"}
        self._install_fare(package)
        currencies = sorted({p["currency"] for p in package.products})
        return {**result_base, "status": "ready", "currencies": currencies,
                "stop_zone_rows": len(package.stop_zones),
                "products": len(package.products), "rules": len(package.rules),
                "message": "fare package installed"}

    def _stored_fare_problems(self, feed_key: str, edition: str, fare_version: str,
                              sha: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT category, file_name, line_number, record_id, field, message "
            "FROM fare_rejections WHERE feed_key=? AND edition=? AND fare_version=? "
            "AND content_sha256=? ORDER BY seq",
            (feed_key, edition, fare_version, sha)).fetchall()
        return [{"category": r["category"], "file": r["file_name"], "line": r["line_number"],
                 "record_id": r["record_id"], "field": r["field"], "message": r["message"]}
                for r in rows]

    def _record_fare_rejection(self, package: FarePackage,
                               problems: list[RejectError]) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            taken = self.conn.execute(
                "SELECT 1 FROM fare_packages WHERE feed_key=? AND edition=? AND fare_version=?",
                (package.feed_key, package.edition, package.fare_version)).fetchone()
            if not taken:
                self.conn.execute(
                    "INSERT INTO fare_packages(feed_key, edition, fare_version, "
                    "archive_sha256, content_sha256, status, currency_set, installed_at) "
                    "VALUES(?,?,?,?,?, 'rejected', ?, ?)",
                    (package.feed_key, package.edition, package.fare_version,
                     package.archive_sha256, package.content_sha256, "", now))
            for seq, problem in enumerate(problems):
                self.conn.execute(
                    "INSERT INTO fare_rejections(feed_key, edition, fare_version, "
                    "content_sha256, seq, category, file_name, line_number, record_id, "
                    "field, message, context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (package.feed_key, package.edition, package.fare_version,
                     package.content_sha256, seq, problem.category, problem.file_name,
                     problem.line_number, problem.record_id, problem.field, problem.message,
                     json.dumps(problem.context, ensure_ascii=False, sort_keys=True)
                     if problem.context else None))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _install_fare(self, package: FarePackage) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM fare_packages WHERE feed_key=? AND edition=? "
                         "AND fare_version=?",
                         (package.feed_key, package.edition, package.fare_version))
            conn.execute(
                "INSERT INTO fare_packages(feed_key, edition, fare_version, archive_sha256, "
                "content_sha256, status, currency_set, installed_at) "
                "VALUES(?,?,?,?,?, 'ready', ?, ?)",
                (package.feed_key, package.edition, package.fare_version,
                 package.archive_sha256, package.content_sha256,
                 ",".join(sorted({p["currency"] for p in package.products})), now))
            conn.executemany(
                "INSERT INTO fare_stop_zones VALUES(?,?,?,?,?)",
                [(package.feed_key, package.edition, package.fare_version,
                  row["stop_id"], row["zone_id"]) for row in package.stop_zones])
            conn.executemany(
                "INSERT INTO fare_products VALUES(?,?,?,?,?,?,?,?,?)",
                [(package.feed_key, package.edition, package.fare_version,
                  p["product_id"], p["currency"], p["amount_minor"], p["amount_text"],
                  p["validity_seconds"], p["max_legs"]) for p in package.products])
            conn.executemany(
                "INSERT INTO fare_product_covers VALUES(?,?,?,?,?)",
                [(package.feed_key, package.edition, package.fare_version,
                  p["product_id"], covered)
                 for p in package.products for covered in p["covers"]])
            conn.executemany(
                "INSERT INTO fare_rules VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(package.feed_key, package.edition, package.fare_version, seq,
                  r["rule_id"], r["priority"], r["route_id"], r["from_zone"],
                  r["to_zone"], r["product_id"])
                 for seq, r in enumerate(package.rules)])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Read-only package/stop queries
    # ------------------------------------------------------------------

    def fare_packages(self, feed_key: str | None = None,
                      edition: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fare_packages WHERE 1=1"
        args: list[Any] = []
        if feed_key is not None:
            sql += " AND feed_key=?"
            args.append(feed_key)
        if edition is not None:
            sql += " AND edition=?"
            args.append(edition)
        sql += " ORDER BY feed_key, edition, fare_version"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def fare_products(self, feed_key: str, edition: str,
                      fare_version: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT p.*, GROUP_CONCAT(c.covered_product_id, '|') AS covers_text "
               "FROM fare_products p LEFT JOIN fare_product_covers c "
               "ON p.feed_key=c.feed_key AND p.edition=c.edition "
               "AND p.fare_version=c.fare_version AND p.product_id=c.product_id "
               "WHERE p.feed_key=? AND p.edition=?")
        args: list[Any] = [feed_key, edition]
        if fare_version is not None:
            sql += " AND p.fare_version=?"
            args.append(fare_version)
        sql += " GROUP BY p.feed_key, p.edition, p.fare_version, p.product_id ORDER BY p.product_id"
        rows = []
        for row in self.conn.execute(sql, args):
            item = dict(row)
            item["covers"] = sorted((item.pop("covers_text") or "").split("|")) if item["covers_text"] else []
            rows.append(item)
        return rows

    def fare_stop_zones(self, feed_key: str, edition: str,
                        fare_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT stop_id, zone_id FROM fare_stop_zones WHERE feed_key=? AND edition=? "
            "AND fare_version=? ORDER BY stop_id",
            (feed_key, edition, fare_version))]

    def fare_rules(self, feed_key: str, edition: str,
                   fare_version: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT rule_id, priority, route_id, from_zone, to_zone, product_id "
            "FROM fare_rules WHERE feed_key=? AND edition=? AND fare_version=? "
            "ORDER BY rule_seq", (feed_key, edition, fare_version))]

    def fare_rejections(self, feed_key: str, edition: str,
                        fare_version: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT fare_version, seq, category, file_name, line_number, record_id, "
               "field, message, context FROM fare_rejections WHERE feed_key=? AND edition=?")
        args: list[Any] = [feed_key, edition]
        if fare_version is not None:
            sql += " AND fare_version=?"
            args.append(fare_version)
        sql += " ORDER BY fare_version, seq"
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ------------------------------------------------------------------
    # Clearing
    # ------------------------------------------------------------------

    def settle(self, feed_key: str, edition: str, fare_version: str,
               batch_bytes: bytes) -> dict[str, Any]:
        """Clear one CSV batch against one fixed fare package and static edition.

        The package and all static rows are snapshotted up front; nothing
        installed later can change the result of this call.
        """
        package_row = self.conn.execute(
            "SELECT status, content_sha256 FROM fare_packages WHERE feed_key=? AND edition=? "
            "AND fare_version=?",
            (feed_key, edition, fare_version)).fetchone()
        if package_row is None:
            raise GtfsError("fare package %s/%s/%s is not installed"
                            % (feed_key, edition, fare_version))
        if package_row["status"] != "ready":
            raise GtfsError("fare package %s/%s/%s is not ready"
                            % (feed_key, edition, fare_version))
        edition_row = self.conn.execute(
            "SELECT status FROM editions WHERE feed_key=? AND edition=?",
            (feed_key, edition)).fetchone()
        if edition_row is None or edition_row["status"] != "ready":
            raise GtfsError("static edition %s/%s is not available for clearing"
                            % (feed_key, edition))

        snapshot = self._load_fare_snapshot(feed_key, edition, fare_version)
        content_sha = package_row["content_sha256"]
        raw_rows, batch_problems = _read_batch(batch_bytes)
        journeys: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in raw_rows:
            journeys[row["journey_id"]].append(row)

        results = []
        for journey_id in sorted(journeys):
            results.append(self._clear_journey(journey_id, journeys[journey_id], snapshot))
        succeeded = [r for r in results if r["status"] == "settled"]
        return {
            "feed_key": feed_key,
            "edition": edition,
            "fare_version": fare_version,
            "content_sha256": content_sha,
            "batch_rows": len(raw_rows),
            "batch_problems": batch_problems,
            "journeys": results,
            "summary": {
                "total": len(results),
                "settled": len(succeeded),
                "rejected": len(results) - len(succeeded),
                "by_currency": _sum_by_currency(succeeded),
            },
        }

    def _load_fare_snapshot(self, feed_key: str, edition: str,
                            fare_version: str) -> dict[str, Any]:
        conn = self.conn
        zones = {r["stop_id"]: r["zone_id"] for r in conn.execute(
            "SELECT stop_id, zone_id FROM fare_stop_zones WHERE feed_key=? AND edition=? "
            "AND fare_version=?", (feed_key, edition, fare_version))}
        products: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
                "SELECT * FROM fare_products WHERE feed_key=? AND edition=? AND fare_version=?",
                (feed_key, edition, fare_version)):
            products[r["product_id"]] = dict(r)
        for r in conn.execute(
                "SELECT product_id, covered_product_id FROM fare_product_covers "
                "WHERE feed_key=? AND edition=? AND fare_version=?",
                (feed_key, edition, fare_version)):
            products[r["product_id"]].setdefault("covers_set", set()).add(
                r["covered_product_id"])
        rules = [dict(r) for r in conn.execute(
            "SELECT rule_id, priority, route_id, from_zone, to_zone, product_id "
            "FROM fare_rules WHERE feed_key=? AND edition=? AND fare_version=? "
            "ORDER BY rule_seq", (feed_key, edition, fare_version))]
        trips = {r["trip_id"]: r["route_id"] for r in conn.execute(
            "SELECT trip_id, route_id FROM trips WHERE feed_key=? AND edition=?",
            (feed_key, edition))}
        stop_times: dict[tuple[str, int], str] = {}
        for r in conn.execute(
                "SELECT trip_id, stop_sequence, stop_id FROM stop_times "
                "WHERE feed_key=? AND edition=?", (feed_key, edition)):
            stop_times[(r["trip_id"], r["stop_sequence"])] = r["stop_id"]
        stops = {r["stop_id"]: dict(r) for r in conn.execute(
            "SELECT stop_id, stop_name FROM stops WHERE feed_key=? AND edition=?",
            (feed_key, edition))}
        return {"zones": zones, "products": products, "rules": rules,
                "trips": trips, "stop_times": stop_times, "stops": stops}

    def _clear_journey(self, journey_id: str, input_rows: list[dict[str, Any]],
                       snap: dict[str, Any]) -> dict[str, Any]:
        ordered, order_problems = _order_legs(journey_id, input_rows)
        if order_problems:
            return {"journey_id": journey_id, "status": "rejected",
                    "currency": None, "total_minor": None, "total_text": None,
                    "legs": [], "reasons": order_problems}

        legs: list[dict[str, Any]] = []
        reasons: list[dict[str, Any]] = []
        for row in ordered:
            leg = self._resolve_leg(row, snap)
            legs.append(leg)
            reasons.extend(leg.pop("problems"))
        if reasons:
            return {"journey_id": journey_id, "status": "rejected",
                    "currency": None, "total_minor": None, "total_text": None,
                    "legs": [], "reasons": reasons}

        currencies = {leg["base_product"]["currency"] for leg in legs}
        if len(currencies) > 1:
            return {
                "journey_id": journey_id, "status": "rejected", "currency": None,
                "total_minor": None, "total_text": None, "legs": [],
                "reasons": [{
                    "code": "mixed_currency",
                    "message": "base products of this journey use more than one currency "
                               "(%s); amounts are neither added nor converted"
                               % ", ".join(sorted(currencies)),
                    "leg_indexes": [leg["leg_index"] for leg in legs],
                }],
            }

        _simulate_ticket_machine(legs, snap)
        currency = next(iter(currencies))
        total = sum(leg["charge_minor"] for leg in legs)
        return {
            "journey_id": journey_id, "status": "settled", "currency": currency,
            "total_minor": total, "total_text": format_minor(total, currency),
            "legs": legs, "reasons": [],
        }

    def _resolve_leg(self, row: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
        problems: list[dict[str, Any]] = []
        leg_index = row["leg_index"]

        def problem(code: str, message: str) -> None:
            problems.append({"code": code, "message": message, "leg_index": leg_index,
                             "input_line": row["input_line"]})

        trip_id = row["trip_id"]
        route_id = snap["trips"].get(trip_id)
        if route_id is None:
            problem("unknown_trip", "trip_id %r does not exist in the bound static edition"
                    % trip_id)
        board_key = (trip_id, row["board_sequence"])
        alight_key = (trip_id, row["alight_sequence"])
        board_stop = snap["stop_times"].get(board_key)
        alight_stop = snap["stop_times"].get(alight_key)
        if board_stop is None:
            problem("unknown_stop_sequence",
                    "trip %r has no stop_sequence %d" % (trip_id, row["board_sequence"]))
        if alight_stop is None:
            problem("unknown_stop_sequence",
                    "trip %r has no stop_sequence %d" % (trip_id, row["alight_sequence"]))
        if board_stop is not None and alight_stop is not None \
                and row["board_sequence"] >= row["alight_sequence"]:
            problem("bad_sequence_order",
                    "board stop_sequence %d must be strictly below alight stop_sequence %d"
                    % (row["board_sequence"], row["alight_sequence"]))
        if row["board_utc"] > row["alight_utc"]:
            problem("bad_time_order",
                    "alight_utc %d must not be earlier than board_utc %d"
                    % (row["alight_utc"], row["board_utc"]))
        board_zone = snap["zones"].get(board_stop) if board_stop else None
        alight_zone = snap["zones"].get(alight_stop) if alight_stop else None
        if board_stop is not None and board_zone is None:
            problem("stop_without_zone",
                    "board stop %r has no fare zone mapping in this package" % board_stop)
        if alight_stop is not None and alight_zone is None:
            problem("stop_without_zone",
                    "alight stop %r has no fare zone mapping in this package" % alight_stop)

        rule = None
        if route_id is not None and board_zone is not None and alight_zone is not None:
            rule = _match_rule(snap["rules"], route_id, board_zone, alight_zone)
            if rule is None:
                problem("no_rule",
                        "no selection rule matches route=%s from_zone=%s to_zone=%s"
                        % (route_id, board_zone, alight_zone))
        base_product = None
        if rule is not None:
            base_product = snap["products"].get(rule["product_id"])
            if base_product is None:  # defensive: install validation should prevent this
                problem("unknown_product",
                        "matched rule %r points to missing product %r"
                        % (rule["rule_id"], rule["product_id"]))
        return {
            "leg_index": leg_index,
            "input_line": row["input_line"],
            "trip_id": trip_id,
            "route_id": route_id,
            "board_sequence": row["board_sequence"],
            "alight_sequence": row["alight_sequence"],
            "board_stop_id": board_stop,
            "alight_stop_id": alight_stop,
            "board_stop_name": (snap["stops"].get(board_stop) or {}).get("stop_name"),
            "alight_stop_name": (snap["stops"].get(alight_stop) or {}).get("stop_name"),
            "board_zone": board_zone,
            "alight_zone": alight_zone,
            "board_utc": row["board_utc"],
            "alight_utc": row["alight_utc"],
            "matched_rule_id": rule["rule_id"] if rule else None,
            "base_product_id": base_product["product_id"] if base_product else None,
            "base_product": (
                {"product_id": base_product["product_id"],
                 "currency": base_product["currency"],
                 "amount_minor": base_product["amount_minor"],
                 "amount_text": base_product["amount_text"]}
                if base_product else None),
            "problems": problems,
        }


def _match_rule(rules: list[dict[str, Any]], route_id: str, from_zone: str,
                to_zone: str) -> dict[str, Any] | None:
    """Highest-priority matching rule; exact raw-string conditions, null = any."""
    best: dict[str, Any] | None = None
    for rule in rules:
        if rule["route_id"] is not None and rule["route_id"] != route_id:
            continue
        if rule["from_zone"] is not None and rule["from_zone"] != from_zone:
            continue
        if rule["to_zone"] is not None and rule["to_zone"] != to_zone:
            continue
        if best is None or rule["priority"] > best["priority"]:
            best = rule
    return best


def _simulate_ticket_machine(legs: list[dict[str, Any]], snap: dict[str, Any]) -> None:
    """Replay the contract state machine, mutating each leg with the outcome.

    Only the most recently purchased ticket is ever considered.  Free
    transfers neither extend the original expiry nor restore quota.
    """
    active: dict[str, Any] | None = None
    for leg in legs:
        base = snap["products"][leg["base_product_id"]]
        if active is None:
            reasons = ["first_purchase"]
            active = _purchase(leg, base)
            _record_purchase(leg, base, reasons, active)
            continue

        active_product = snap["products"][active["product_id"]]
        not_covered = base["product_id"] not in active_product["covers_set"]
        expired = not (leg["board_utc"] < active["purchased_at"]
                       + active_product["validity_seconds"])
        quota_exhausted = not (active["used_legs"] < active_product["max_legs"])
        if not_covered or expired or quota_exhausted:
            reasons = []
            if not_covered:
                reasons.append("product_not_covered")
            if expired:
                reasons.append("expired")
            if quota_exhausted:
                reasons.append("quota_exhausted")
            active = _purchase(leg, base)
            _record_purchase(leg, base, reasons, active)
        else:
            active["used_legs"] += 1
            leg.update({
                "paid_product_id": active["product_id"],
                "source_leg_index": active["source_leg_index"],
                "purchase_reasons": [],
                "charge_minor": 0,
                "charge_text": format_minor(0, base["currency"]),
                "ticket_expires_utc": active["purchased_at"]
                + active_product["validity_seconds"],
                "used_legs_after": active["used_legs"],
            })


def _purchase(leg: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    return {"product_id": base["product_id"], "purchased_at": leg["board_utc"],
            "used_legs": 1, "source_leg_index": leg["leg_index"]}


def _record_purchase(leg: dict[str, Any], base: dict[str, Any],
                     reasons: list[str], active: dict[str, Any]) -> None:
    leg.update({
        "paid_product_id": base["product_id"],
        "source_leg_index": active["source_leg_index"],
        "purchase_reasons": reasons,
        "charge_minor": base["amount_minor"],
        "charge_text": base["amount_text"],
        "ticket_expires_utc": active["purchased_at"] + base["validity_seconds"],
        "used_legs_after": 1,
    })


def _sum_by_currency(succeeded: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for journey in succeeded:
        entry = totals.setdefault(journey["currency"], {"minor": 0, "journeys": 0})
        entry["minor"] += journey["total_minor"]
        entry["journeys"] += 1
    for currency, entry in totals.items():
        entry["text"] = format_minor(entry["minor"], currency)
    return totals


# ---------------------------------------------------------------------------
# Batch CSV parsing
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^-?[0-9]+$")


def _parse_integer(raw: str) -> int | None:
    if not _INT_RE.match(raw):
        return None
    return int(raw)


def _read_batch(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    try:
        header, rows = read_csv_bytes(data)
    except RejectError as exc:
        return [], [{"code": "bad_batch", "message": exc.message, "input_line": None}]
    missing = [c for c in SETTLE_COLUMNS if c not in header]
    if missing:
        problems.append({"code": "bad_batch",
                         "message": "batch CSV is missing column(s): %s" % ", ".join(missing),
                         "input_line": 1})
        return [], problems
    parsed: list[dict[str, Any]] = []
    seen_lines: set[int] = set()
    for raw, line, _width in rows:
        if line in seen_lines:
            continue
        seen_lines.add(line)
        journey_id = raw.get("journey_id", "")
        if journey_id == "":
            problems.append({"code": "bad_batch", "message": "journey_id must not be empty",
                             "input_line": line})
            continue
        values: dict[str, Any] = {"journey_id": journey_id, "input_line": line}
        bad = False
        for column in ("leg_index", "board_sequence", "alight_sequence",
                       "board_utc", "alight_utc"):
            number = _parse_integer(raw.get(column, ""))
            if number is None:
                problems.append({"code": "bad_batch",
                                 "message": "%s must be an integer, got %r"
                                            % (column, raw.get(column, "")),
                                 "input_line": line, "journey_id": journey_id})
                bad = True
            values[column] = number
        trip_id = raw.get("trip_id", "")
        if trip_id == "":
            problems.append({"code": "bad_batch", "message": "trip_id must not be empty",
                             "input_line": line, "journey_id": journey_id})
            bad = True
        values["trip_id"] = trip_id
        if bad:
            continue
        parsed.append(values)
    return parsed, problems


def _order_legs(journey_id: str,
                rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                      list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        index = row["leg_index"]
        if index in by_index:
            problems.append({
                "code": "duplicate_leg_index",
                "message": "journey %r has leg_index %d on input lines %d and %d"
                           % (journey_id, index, by_index[index]["input_line"],
                              row["input_line"]),
                "leg_index": index, "input_line": row["input_line"]})
            continue
        if index < 1:
            problems.append({
                "code": "bad_leg_index",
                "message": "leg_index must start at 1, got %d" % index,
                "leg_index": index, "input_line": row["input_line"]})
            continue
        by_index[index] = row
    if problems:
        return [], problems
    expected = list(range(1, len(by_index) + 1))
    if sorted(by_index) != expected:
        problems.append({
            "code": "non_contiguous_leg_index",
            "message": "leg_index values of journey %r must be 1..%d with no gaps, got %s"
                       % (journey_id, len(by_index), sorted(by_index)),
            "input_line": None})
        return [], problems
    ordered = [by_index[i] for i in expected]
    for previous, current in zip(ordered, ordered[1:]):
        if current["board_utc"] < previous["alight_utc"]:
            problems.append({
                "code": "bad_chain_order",
                "message": "leg %d boards at %d, before leg %d alights at %d"
                           % (current["leg_index"], current["board_utc"],
                              previous["leg_index"], previous["alight_utc"]),
                "leg_index": current["leg_index"], "input_line": current["input_line"]})
    if problems:
        return [], problems
    return ordered, []
