"""Installable fare rule packages: ZIP/JSON intake and exhaustive validation.

A fare package is a ZIP of four top-level JSON members:

* ``manifest.json``  -- binding identity (feed_key, edition, fare_version)
* ``stop_zones.json`` -- stop_id -> zone_id mapping (one zone per stop)
* ``products.json``  -- fare products with fixed-point money and coverage sets
* ``rules.json``     -- priority-based product selection rules

This is the project's own fare contract, not the GTFS Fares standard.  The
module is deliberately free of SQLite access: all structural and semantic
checks run on plain data, so the same logic is unit-testable in isolation.
Cross checks against the bound static edition (stop/route existence) live in
``gtfs_store.fares`` because they need database access.

Null semantics matter here: JSON ``null`` in a rule condition means "any";
a JSON empty string ``""`` is a literal value that never matches a real
route or zone id and is never treated as a wildcard.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from . import archive
from .errors import RejectError

MANIFEST_FILE = "manifest.json"
STOP_ZONES_FILE = "stop_zones.json"
PRODUCTS_FILE = "products.json"
RULES_FILE = "rules.json"
FARE_FILES = (MANIFEST_FILE, STOP_ZONES_FILE, PRODUCTS_FILE, RULES_FILE)

# CNY/USD keep two decimal places; JPY keeps zero.  Amounts are decimal
# fixed-point strings only: the grammar below refuses exponents, NaN,
# Infinity, signs, implicit rounding and precision beyond the currency.
_CURRENCY_SCALE = {"CNY": 2, "USD": 2, "JPY": 0}
_AMOUNT_GRAMMAR = {
    2: re.compile(r"^[0-9]+\.[0-9]{2}$"),
    0: re.compile(r"^[0-9]+$"),
}

_MANIFEST_KEYS = {"feed_key", "edition", "fare_version"}
_STOP_ZONE_KEYS = {"stop_id", "zone_id"}
_PRODUCT_KEYS = {
    "product_id", "amount", "currency", "validity_seconds", "max_legs", "covers",
}
_RULE_KEYS = {"rule_id", "priority", "route_id", "from_zone", "to_zone", "product_id"}


@dataclass
class FarePackage:
    feed_key: str
    edition: str
    fare_version: str
    stop_zones: list[dict[str, str]]
    products: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    problems: list[RejectError] = field(default_factory=list)
    archive_sha256: str = ""
    content_sha256: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def product(self, product_id: str) -> dict[str, Any] | None:
        for product in self.products:
            if product["product_id"] == product_id:
                return product
        return None

    def zone_ids(self) -> set[str]:
        return {row["zone_id"] for row in self.stop_zones}


# ---------------------------------------------------------------------------
# Archive reading (same structural safety rules as static packages)
# ---------------------------------------------------------------------------

def read_fare_package(path: str, raw_archive: bytes | None = None) -> dict[str, bytes]:
    """Read a fare ZIP, enforcing the same path/size/duplicate safety rules."""
    if raw_archive is not None:
        data = raw_archive
    else:
        with open(path, "rb") as fh:
            data = fh.read()
    if not data:
        raise RejectError("archive", "archive is empty", file_name=path)
    if len(data) > archive.MAX_ARCHIVE_BYTES:
        raise RejectError("size", "archive is %d bytes, limit is %d"
                          % (len(data), archive.MAX_ARCHIVE_BYTES), file_name=path)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise RejectError("archive", "not a readable ZIP archive: %s" % exc,
                          file_name=path) from exc
    members: dict[str, bytes] = {}
    total = 0
    with zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            reason = archive._unsafe_reason(name)
            if reason:
                raise RejectError("unsafe_path", "%s: %r" % (reason, name), file_name=path)
            if name in members:
                raise RejectError("duplicate_member", "duplicate member %r" % name,
                                  file_name=path, record_id=name)
            if name not in FARE_FILES:
                raise RejectError(
                    "unsupported_member",
                    "member %r is outside the fare package file set (%s)"
                    % (name, ", ".join(FARE_FILES)), file_name=path)
            if info.flag_bits & 0x1:
                raise RejectError("unsupported_member", "encrypted member %r" % name,
                                  file_name=path, record_id=name)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise RejectError("unsupported_member",
                                  "member %r uses unsupported compression" % name,
                                  file_name=path, record_id=name)
            if info.file_size > archive.MAX_MEMBER_BYTES:
                raise RejectError("size", "member %r exceeds the uncompressed size limit" % name,
                                  file_name=path, record_id=name)
            total += info.file_size
            if total > archive.MAX_TOTAL_UNCOMPRESSED:
                raise RejectError("size", "uncompressed members exceed the total size limit",
                                  file_name=path)
        missing = [name for name in FARE_FILES if name not in zf.namelist()]
        if missing:
            raise RejectError("missing_file", "required fare member(s) absent: %s"
                              % ", ".join(missing), file_name=path)
        for name in FARE_FILES:
            with zf.open(name, "r") as fh:
                members[name] = fh.read(archive.MAX_MEMBER_BYTES + 1)
            if len(members[name]) > archive.MAX_MEMBER_BYTES:
                raise RejectError("size", "member %r exceeds the uncompressed size limit" % name,
                                  file_name=path, record_id=name)
    return members


def _reject(problems: list[RejectError], category: str, message: str, *,
            file_name: str, field: str | None = None, record_id: str | None = None) -> None:
    problems.append(RejectError(category, message, file_name=file_name,
                                field=field, record_id=record_id))


class _DuplicateKeyError(ValueError):
    pass


def _load_json(member: str, data: bytes, problems: list[RejectError]) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _reject(problems, "encoding", "file is not valid UTF-8 (byte offset %d)" % exc.start,
                file_name=member)
        return None

    def reject_duplicates(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                raise _DuplicateKeyError(str(key))
            seen.add(key)
        return dict(pairs)

    try:
        return json.loads(text, object_pairs_hook=reject_duplicates)
    except _DuplicateKeyError as exc:
        _reject(problems, "duplicate_key", "duplicate JSON object key %r" % exc.args[0],
                file_name=member, field=str(exc.args[0]))
    except json.JSONDecodeError as exc:
        _reject(problems, "structure", "malformed JSON: %s" % exc.msg, file_name=member)
    return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _check_keys(obj: dict[str, Any], expected: set[str], member: str,
                problems: list[RejectError], record_id: str | None) -> bool:
    unknown = set(obj) - expected
    if unknown:
        _reject(problems, "unsupported_semantics",
                "unknown field(s) %s" % ", ".join(sorted(unknown)),
                file_name=member, field=sorted(unknown)[0], record_id=record_id)
        return False
    return True


# ---------------------------------------------------------------------------
# Money: strict decimal fixed-point, integer minor units internally
# ---------------------------------------------------------------------------

def parse_amount(raw: Any, currency: str) -> tuple[int, str] | None:
    """Return (minor units, canonical text) or None when the value is illegal.

    Nothing here rounds: a value with too many fractional digits is rejected.
    """
    if not isinstance(raw, str):
        return None
    if currency not in _CURRENCY_SCALE:
        return None
    scale = _CURRENCY_SCALE[currency]
    if not _AMOUNT_GRAMMAR[scale].match(raw):
        return None
    if scale == 0:
        return int(raw), raw
    whole, frac = raw.split(".")
    minor = int(whole) * 100 + int(frac)
    return minor, "%d.%02d" % (minor // 100, minor % 100)


def format_minor(minor: int, currency: str) -> str:
    if _CURRENCY_SCALE[currency] == 0:
        return str(minor)
    return "%d.%02d" % (minor // 100, minor % 100)


# ---------------------------------------------------------------------------
# Package parsing and structural validation
# ---------------------------------------------------------------------------

def parse_fare_package(members: dict[str, bytes]) -> FarePackage:
    problems: list[RejectError] = []
    manifest = _load_json(MANIFEST_FILE, members[MANIFEST_FILE], problems)
    zones_raw = _load_json(STOP_ZONES_FILE, members[STOP_ZONES_FILE], problems)
    products_raw = _load_json(PRODUCTS_FILE, members[PRODUCTS_FILE], problems)
    rules_raw = _load_json(RULES_FILE, members[RULES_FILE], problems)
    if problems:
        # Without parseable members nothing downstream is trustworthy.
        return FarePackage("", "", "", [], [], [], problems)

    feed_key = edition = fare_version = ""
    if not isinstance(manifest, dict):
        _reject(problems, "structure", "manifest.json must be a JSON object",
                file_name=MANIFEST_FILE)
    else:
        _check_keys(manifest, _MANIFEST_KEYS, MANIFEST_FILE, problems, None)
        for key in _MANIFEST_KEYS:
            if not _is_nonempty_str(manifest.get(key)):
                _reject(problems, "missing_value", "%s must be a non-empty string" % key,
                        file_name=MANIFEST_FILE, field=key)
        if all(_is_nonempty_str(manifest.get(k)) for k in _MANIFEST_KEYS):
            feed_key = manifest["feed_key"]
            edition = manifest["edition"]
            fare_version = manifest["fare_version"]

    stop_zones = _parse_stop_zones(zones_raw, problems)
    products = _parse_products(products_raw, problems)
    rules = _parse_rules(rules_raw, products, {z["zone_id"] for z in stop_zones}, problems)
    return FarePackage(feed_key, edition, fare_version, stop_zones, products, rules, problems)


def _parse_stop_zones(raw: Any, problems: list[RejectError]) -> list[dict[str, str]]:
    member = STOP_ZONES_FILE
    if not isinstance(raw, list):
        _reject(problems, "structure", "stop_zones.json must be a JSON array", file_name=member)
        return []
    rows: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for index, obj in enumerate(raw):
        where = "row %d" % index
        if not isinstance(obj, dict):
            _reject(problems, "structure", "%s must be a JSON object" % where,
                    file_name=member, record_id=where)
            continue
        _check_keys(obj, _STOP_ZONE_KEYS, member, problems, where)
        stop_id = obj.get("stop_id")
        zone_id = obj.get("zone_id")
        if not _is_nonempty_str(stop_id):
            _reject(problems, "missing_value", "stop_id must be a non-empty string",
                    file_name=member, field="stop_id", record_id=where)
            continue
        if not _is_nonempty_str(zone_id):
            _reject(problems, "missing_value", "zone_id must be a non-empty string",
                    file_name=member, field="zone_id", record_id=stop_id)
            continue
        if stop_id in seen:
            _reject(problems, "duplicate_id",
                    "stop_id %r is mapped twice (a stop can only belong to one zone)" % stop_id,
                    file_name=member, field="stop_id", record_id=stop_id)
            continue
        seen[stop_id] = index
        rows.append({"stop_id": stop_id, "zone_id": zone_id})
    if not rows:
        _reject(problems, "empty_file", "stop_zones.json has no usable stop/zone mappings",
                file_name=member)
    return rows


def _parse_products(raw: Any, problems: list[RejectError]) -> list[dict[str, Any]]:
    member = PRODUCTS_FILE
    if not isinstance(raw, list):
        _reject(problems, "structure", "products.json must be a JSON array", file_name=member)
        return []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, obj in enumerate(raw):
        where = "row %d" % index
        if not isinstance(obj, dict):
            _reject(problems, "structure", "%s must be a JSON object" % where,
                    file_name=member, record_id=where)
            continue
        _check_keys(obj, _PRODUCT_KEYS, member, problems, where)
        pid = obj.get("product_id")
        if not _is_nonempty_str(pid):
            _reject(problems, "missing_value", "product_id must be a non-empty string",
                    file_name=member, field="product_id", record_id=where)
            continue
        if pid in seen:
            _reject(problems, "duplicate_id", "product_id %r appears more than once" % pid,
                    file_name=member, field="product_id", record_id=pid)
            continue
        seen[pid] = index
        currency = obj.get("currency")
        if currency not in _CURRENCY_SCALE:
            _reject(problems, "invalid_value",
                    "currency %r is not supported (allowed: CNY, USD, JPY)" % (currency,),
                    file_name=member, field="currency", record_id=pid)
            continue
        parsed_money = parse_amount(obj.get("amount"), currency)
        if parsed_money is None:
            _reject(problems, "invalid_amount",
                    "amount %r is not a valid %s fixed-point string (scale %d); exponents, "
                    "NaN, Infinity and implicit rounding are not accepted"
                    % (obj.get("amount"), currency, _CURRENCY_SCALE[currency]),
                    file_name=member, field="amount", record_id=pid)
            continue
        minor, amount_text = parsed_money
        validity = obj.get("validity_seconds")
        max_legs = obj.get("max_legs")
        # bool is a subclass of int in Python; explicitly refuse JSON booleans.
        if not isinstance(validity, int) or isinstance(validity, bool) or validity <= 0:
            _reject(problems, "invalid_value",
                    "validity_seconds must be a positive integer",
                    file_name=member, field="validity_seconds", record_id=pid)
            continue
        if not isinstance(max_legs, int) or isinstance(max_legs, bool) or max_legs <= 0:
            _reject(problems, "invalid_value", "max_legs must be a positive integer",
                    file_name=member, field="max_legs", record_id=pid)
            continue
        covers = obj.get("covers")
        if not isinstance(covers, list) or not all(_is_nonempty_str(c) for c in covers):
            _reject(problems, "invalid_value",
                    "covers must be an array of non-empty product id strings",
                    file_name=member, field="covers", record_id=pid)
            continue
        if len(set(covers)) != len(covers):
            _reject(problems, "duplicate_id", "covers of product %r contains duplicates" % pid,
                    file_name=member, field="covers", record_id=pid)
            continue
        rows.append({
            "product_id": pid,
            "currency": currency,
            "amount_minor": minor,
            "amount_text": amount_text,
            "amount_raw": obj["amount"],
            "validity_seconds": validity,
            "max_legs": max_legs,
            "covers": list(covers),
        })
    if not rows:
        _reject(problems, "empty_file", "products.json has no usable products", file_name=member)
    # Coverage references are checked once every product id is known.
    known = {row["product_id"] for row in rows}
    for row in rows:
        pid = row["product_id"]
        for ref in row["covers"]:
            if ref not in known:
                _reject(problems, "foreign_key",
                        "product %r covers %r, which is not defined in products.json"
                        % (pid, ref), file_name=member, field="covers", record_id=pid)
        if pid not in row["covers"]:
            _reject(problems, "invalid_value",
                    "coverage set of product %r must include the product itself" % pid,
                    file_name=member, field="covers", record_id=pid)
    return rows


def _parse_rules(raw: Any, products: list[dict[str, Any]], zone_ids: set[str],
                 problems: list[RejectError]) -> list[dict[str, Any]]:
    member = RULES_FILE
    if not isinstance(raw, list):
        _reject(problems, "structure", "rules.json must be a JSON array", file_name=member)
        return []
    product_ids = {p["product_id"] for p in products}
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, obj in enumerate(raw):
        where = "row %d" % index
        if not isinstance(obj, dict):
            _reject(problems, "structure", "%s must be a JSON object" % where,
                    file_name=member, record_id=where)
            continue
        _check_keys(obj, _RULE_KEYS, member, problems, where)
        rid = obj.get("rule_id")
        if not _is_nonempty_str(rid):
            _reject(problems, "missing_value", "rule_id must be a non-empty string",
                    file_name=member, field="rule_id", record_id=where)
            continue
        if rid in seen:
            _reject(problems, "duplicate_id", "rule_id %r appears more than once" % rid,
                    file_name=member, field="rule_id", record_id=rid)
            continue
        seen[rid] = index
        priority = obj.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            _reject(problems, "invalid_value", "priority must be an integer",
                    file_name=member, field="priority", record_id=rid)
            continue
        pid = obj.get("product_id")
        if not _is_nonempty_str(pid):
            _reject(problems, "missing_value", "product_id must be a non-empty string",
                    file_name=member, field="product_id", record_id=rid)
            continue
        if pid not in product_ids:
            _reject(problems, "foreign_key",
                    "rule %r buys product %r, which is not defined in products.json"
                    % (rid, pid), file_name=member, field="product_id", record_id=rid)
        condition: dict[str, Any] = {}
        ok = True
        for field_name in ("route_id", "from_zone", "to_zone"):
            value = obj.get(field_name)
            if value is None:
                condition[field_name] = None
            elif isinstance(value, str):
                # Empty string is a legal literal condition, never a wildcard;
                # it simply cannot match a non-empty real id.
                condition[field_name] = value
            else:
                _reject(problems, "invalid_value",
                        "%s must be null or a string" % field_name,
                        file_name=member, field=field_name, record_id=rid)
                ok = False
        if condition.get("from_zone") and condition["from_zone"] not in zone_ids:
            _reject(problems, "foreign_key",
                    "rule %r from_zone %r does not appear in stop_zones.json"
                    % (rid, condition["from_zone"]),
                    file_name=member, field="from_zone", record_id=rid)
            ok = False
        if condition.get("to_zone") and condition["to_zone"] not in zone_ids:
            _reject(problems, "foreign_key",
                    "rule %r to_zone %r does not appear in stop_zones.json"
                    % (rid, condition["to_zone"]),
                    file_name=member, field="to_zone", record_id=rid)
            ok = False
        if ok:
            rows.append({"rule_id": rid, "priority": priority, "product_id": pid, **condition})
    if not rows:
        _reject(problems, "empty_file", "rules.json has no usable selection rules",
                file_name=member)
    return rows


# ---------------------------------------------------------------------------
# Same-priority overlap detection via condition-set intersection
# ---------------------------------------------------------------------------

def _condition_set(value: str | None, universe: set[str]) -> set[str]:
    """The concrete ids on which a condition holds (null means the universe)."""
    if value is None:
        return set(universe)
    return {value} if value in universe else set()


def find_priority_overlaps(rules: list[dict[str, Any]], route_ids: set[str],
                           zone_ids: set[str]) -> list[dict[str, Any]]:
    """Find same-priority rule pairs whose three conditions can hold together.

    Conditions are intersected as sets; stop pairs are never enumerated.  A
    witness (one concrete route/from-zone/to-zone on which both rules fire)
    is produced for every overlapping pair.  Rules at different priorities
    are never compared: priority order resolves those legitimately.
    """
    if not route_ids or not zone_ids:
        return []
    expanded = []
    for rule in rules:
        expanded.append((
            rule,
            _condition_set(rule["route_id"], route_ids),
            _condition_set(rule["from_zone"], zone_ids),
            _condition_set(rule["to_zone"], zone_ids),
        ))
    conflicts: list[dict[str, Any]] = []
    for i in range(len(expanded)):
        rule_a, routes_a, from_a, to_a = expanded[i]
        for j in range(i + 1, len(expanded)):
            rule_b, routes_b, from_b, to_b = expanded[j]
            if rule_a["priority"] != rule_b["priority"]:
                continue
            route_hits = routes_a & routes_b
            from_hits = from_a & from_b
            to_hits = to_a & to_b
            if route_hits and from_hits and to_hits:
                conflicts.append({
                    "priority": rule_a["priority"],
                    "rule_a": rule_a["rule_id"],
                    "rule_b": rule_b["rule_id"],
                    "witness": {
                        "route_id": sorted(route_hits)[0],
                        "from_zone": sorted(from_hits)[0],
                        "to_zone": sorted(to_hits)[0],
                    },
                })
    return conflicts
