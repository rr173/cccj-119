"""GTFS Realtime intake channel: TripUpdate feeds on top of a static edition.

This module only moves and checks source observations.  It does **no** delay
propagation, **no** predicted-arrival computation and **no** network access:
callers hand over already-downloaded ``FeedMessage`` byte strings.

Concepts
--------

* **Source** (`rt_sources`): a realtime channel bound once to one ready
  static ``(feed_key, edition)``.  The binding cannot change afterwards;
  different sources may reuse the same entity ids without interfering.
* **Reception ledger** (`rt_receptions`): every handed-in message, with the
  original BLOB, its SHA-256 digest, the header timestamp, the processing
  category and diagnostics.  Current entities point back at the reception
  that produced them.
* **Success ledger** (`rt_head_ledger`): one row per
  ``(source_key, header_timestamp)`` that was actually applied.  Redelivered
  bytes are answered from this ledger (``duplicate``); the same timestamp
  with different bytes is a ``conflict``.  Rejected messages never enter it,
  so a corrected message may reuse the timestamp.
* **Current set** (`rt_entities` / `rt_stop_events`): the latest view of the
  source.  ``FULL_DATASET`` replaces it wholesale (missing entities are
  withdrawn, an empty message withdraws everything); ``DIFFERENTIAL``
  replaces listed entities and withdraws ``is_deleted`` ones, and requires a
  baseline full dataset first.

Field presence (time / delay / uncertainty) is taken from protobuf
``HasField``: an absent field is stored as NULL, an explicitly given ``0``
is stored as ``0``.  Nothing is computed, copied or filled in.  Subfields
this channel does not carry (``TripUpdate.vehicle``, ``TripUpdate.delay``,
…) are ignored rather than stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from typing import Any

from . import rtbind
from .errors import GtfsError
from .model import format_date, parse_date, parse_time
from .store import Store

RT_DDL = """
CREATE TABLE IF NOT EXISTS rt_sources (
    source_key TEXT PRIMARY KEY,
    feed_key TEXT NOT NULL,
    edition TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    head_timestamp INTEGER,
    head_incrementality TEXT,
    head_reception_id INTEGER,
    baseline_seen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rt_receptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    received_at TEXT NOT NULL,
    blob BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    header_timestamp INTEGER,
    header_incrementality TEXT,
    category TEXT NOT NULL CHECK (category IN
        ('applied', 'duplicate', 'conflict', 'late', 'rejected')),
    applied INTEGER NOT NULL DEFAULT 0,
    diagnostics_json TEXT NOT NULL,
    original_reception_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rt_receptions_source
    ON rt_receptions (source_key, id);

CREATE TABLE IF NOT EXISTS rt_head_ledger (
    source_key TEXT NOT NULL,
    header_timestamp INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    reception_id INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (source_key, header_timestamp)
);

CREATE TABLE IF NOT EXISTS rt_entities (
    source_key TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    start_time TEXT NOT NULL DEFAULT '',
    trip_schedule_relationship TEXT NOT NULL
        CHECK (trip_schedule_relationship IN ('SCHEDULED', 'CANCELED')),
    entity_timestamp INTEGER,
    head_timestamp INTEGER NOT NULL,
    reception_id INTEGER NOT NULL,
    PRIMARY KEY (source_key, entity_id)
);

-- A run instance (trip, service date, start time) may be represented by at
-- most one current entity per source.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rt_entities_instance
    ON rt_entities (source_key, trip_id, service_date, start_time);

CREATE TABLE IF NOT EXISTS rt_stop_events (
    source_key TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT,
    schedule_relationship TEXT NOT NULL
        CHECK (schedule_relationship IN ('SCHEDULED', 'SKIPPED', 'NO_DATA')),
    has_arrival INTEGER NOT NULL DEFAULT 0,
    arr_time INTEGER,
    arr_delay INTEGER,
    arr_uncertainty INTEGER,
    has_departure INTEGER NOT NULL DEFAULT 0,
    dep_time INTEGER,
    dep_delay INTEGER,
    dep_uncertainty INTEGER,
    PRIMARY KEY (source_key, entity_id, stop_sequence)
);
"""

# Processing categories for the reception ledger.
APPLIED = "applied"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
LATE = "late"
REJECTED = "rejected"

_FULL = "FULL_DATASET"
_DIFFERENTIAL = "DIFFERENTIAL"

_TRIP_RELATIONSHIPS = ("SCHEDULED", "CANCELED")
_STOP_RELATIONSHIPS = ("SCHEDULED", "SKIPPED", "NO_DATA")
_NO_EVENT_RELATIONSHIPS = ("SKIPPED", "NO_DATA")

# FeedEntity payload fields other than trip_update that this channel refuses.
_OTHER_PAYLOADS = ("vehicle", "alert", "shape", "stop", "trip_modifications")


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _problem(category: str, message: str, *, entity_id: str | None = None,
             field: str | None = None) -> dict[str, Any]:
    return {"category": category, "message": message,
            "entity_id": entity_id, "field": field}


class _Header:
    __slots__ = ("timestamp", "incrementality")

    def __init__(self, timestamp: int, incrementality: str) -> None:
        self.timestamp = timestamp
        self.incrementality = incrementality


class _Event:
    """One StopTimeEvent: each of time/delay/uncertainty as (given, value)."""

    __slots__ = ("time", "delay", "uncertainty")

    def __init__(self) -> None:
        self.time = (False, None)
        self.delay = (False, None)
        self.uncertainty = (False, None)


class _Stop:
    __slots__ = ("stop_sequence", "stop_id", "relationship", "arrival", "departure")

    def __init__(self, stop_sequence: int, stop_id: str | None, relationship: str,
                 arrival: _Event | None, departure: _Event | None) -> None:
        self.stop_sequence = stop_sequence
        self.stop_id = stop_id
        self.relationship = relationship
        self.arrival = arrival
        self.departure = departure


class _Entity:
    __slots__ = ("entity_id", "is_deleted", "trip_id", "service_date", "start_time",
                 "trip_relationship", "entity_timestamp", "stops")

    def __init__(self, entity_id: str, is_deleted: bool) -> None:
        self.entity_id = entity_id
        self.is_deleted = is_deleted
        self.trip_id: str | None = None
        self.service_date: str | None = None
        self.start_time: str | None = None
        self.trip_relationship: str | None = None
        self.entity_timestamp: int | None = None
        self.stops: list[_Stop] = []


class RealtimeStore:
    """Realtime channel state living in the same SQLite file as a Store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.conn = store.conn
        self.conn.executescript(RT_DDL)
        self._mod = rtbind.bindings()
        self._pb = self._mod.pb

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_source(self, source_key: str, feed_key: str, edition: str) -> dict[str, Any]:
        """Bind a realtime source to one ready static edition (cannot rebind)."""
        if not source_key:
            raise GtfsError("source_key must not be empty")
        row = self.conn.execute(
            "SELECT status FROM editions WHERE feed_key=? AND edition=?",
            (feed_key, edition)).fetchone()
        if row is None:
            raise GtfsError("feed_key %r has no edition %r to bind a realtime source to"
                            % (feed_key, edition))
        if row["status"] != "ready":
            raise GtfsError("edition %r of feed_key %r is %s; a realtime source needs a "
                            "ready static edition" % (edition, feed_key, row["status"]))
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT feed_key, edition FROM rt_sources WHERE source_key=?",
                (source_key,)).fetchone()
            if existing is not None:
                if existing["feed_key"] == feed_key and existing["edition"] == edition:
                    conn.execute("COMMIT")
                    return {"source_key": source_key, "feed_key": feed_key,
                            "edition": edition, "already_registered": True,
                            "message": "source already registered with this binding; "
                                       "unchanged"}
                conn.execute("ROLLBACK")
                raise GtfsError(
                    "realtime source %r is already bound to %s/%s and cannot be "
                    "rebound to %s/%s" % (source_key, existing["feed_key"],
                                          existing["edition"], feed_key, edition))
            conn.execute(
                "INSERT INTO rt_sources(source_key, feed_key, edition, registered_at) "
                "VALUES(?,?,?,?)", (source_key, feed_key, edition, _utcnow()))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {"source_key": source_key, "feed_key": feed_key, "edition": edition,
                "already_registered": False, "message": "source registered"}

    def sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.source_key, s.feed_key, s.edition, s.registered_at, "
            " s.head_timestamp, s.head_incrementality, s.baseline_seen, "
            " (SELECT COUNT(*) FROM rt_entities e WHERE e.source_key=s.source_key) "
            "   AS entities, "
            " (SELECT COUNT(*) FROM rt_receptions r WHERE r.source_key=s.source_key) "
            "   AS receptions "
            "FROM rt_sources s ORDER BY s.source_key").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["baseline_seen"] = bool(item["baseline_seen"])
            out.append(item)
        return out

    def source_status(self, source_key: str) -> dict[str, Any]:
        src = dict(self._require_source(source_key))
        src["baseline_seen"] = bool(src["baseline_seen"])
        src["entities"] = self.conn.execute(
            "SELECT COUNT(*) FROM rt_entities WHERE source_key=?", (source_key,)).fetchone()[0]
        src["ledger_entries"] = self.conn.execute(
            "SELECT COUNT(*) FROM rt_head_ledger WHERE source_key=?",
            (source_key,)).fetchone()[0]
        return src

    def _require_source(self, source_key: str):
        row = self.conn.execute(
            "SELECT * FROM rt_sources WHERE source_key=?", (source_key,)).fetchone()
        if row is None:
            raise GtfsError("no realtime source %r is registered" % source_key)
        return row

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------

    def feed(self, source_key: str, blob: bytes) -> dict[str, Any]:
        """Take one downloaded FeedMessage blob and fold it into the source.

        The ledger check, the set change, the head move and the reception
        record all commit in one IMMEDIATE transaction, so competing feeders
        serialize and can never blend two messages into one set.
        """
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise GtfsError("feed() expects the raw FeedMessage bytes")
        blob = bytes(blob)
        sha = hashlib.sha256(blob).hexdigest()
        received_at = _utcnow()
        header, entities, problems = self._parse(blob)

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            src = conn.execute(
                "SELECT * FROM rt_sources WHERE source_key=?", (source_key,)).fetchone()
            if src is None:
                raise GtfsError("no realtime source %r is registered" % source_key)

            # Envelope problems: no trustworthy timestamp, nothing to check.
            if header is None:
                return self._finish(conn, source_key, received_at, blob, sha, None,
                                    REJECTED, problems)

            # 1. The digest ledger answers redeliveries before anything else.
            led = conn.execute(
                "SELECT sha256, reception_id FROM rt_head_ledger "
                "WHERE source_key=? AND header_timestamp=?",
                (source_key, header.timestamp)).fetchone()
            if led is not None:
                if led["sha256"] == sha:
                    original = conn.execute(
                        "SELECT category FROM rt_receptions WHERE id=?",
                        (led["reception_id"],)).fetchone()
                    note = _problem(
                        "duplicate_delivery",
                        "identical bytes were already applied as reception #%d; the "
                        "stored conclusion stands and nothing was applied again"
                        % led["reception_id"])
                    return self._finish(
                        conn, source_key, received_at, blob, sha, header, DUPLICATE,
                        [note], original_id=led["reception_id"],
                        extra={"original_reception_id": led["reception_id"],
                               "original_category": original["category"]
                               if original else None})
                note = _problem(
                    "timestamp_conflict",
                    "header timestamp %d is already applied from different bytes "
                    "(ledger sha256 %s…, incoming %s…); the incoming message was "
                    "refused" % (header.timestamp, led["sha256"][:16], sha[:16]))
                return self._finish(conn, source_key, received_at, blob, sha, header,
                                    CONFLICT, [note])

            # 2. Only now compare against the source head.
            head = src["head_timestamp"]
            if head is not None and header.timestamp <= head:
                note = _problem(
                    "stale_message",
                    "header timestamp %d is not newer than the source head %d; "
                    "recorded as late, nothing applied" % (header.timestamp, head))
                return self._finish(conn, source_key, received_at, blob, sha, header,
                                    LATE, [note])

            # 3. A differential needs a baseline full dataset first.
            if header.incrementality == _DIFFERENTIAL and not src["baseline_seen"]:
                note = _problem(
                    "no_baseline",
                    "DIFFERENTIAL message before any FULL_DATASET: there is no "
                    "dependable set to apply it to")
                return self._finish(conn, source_key, received_at, blob, sha, header,
                                    REJECTED, [note])

            # 4. Structural entity problems, then checks against the bound
            #    static edition and the current set.
            if problems:
                return self._finish(conn, source_key, received_at, blob, sha, header,
                                    REJECTED, problems)
            db_problems = self._validate_against_db(src, header, entities)
            if db_problems:
                return self._finish(conn, source_key, received_at, blob, sha, header,
                                    REJECTED, db_problems)

            # 5. Apply: reception row first (entities point at it), then the
            #    set change, the ledger and the head move — one transaction.
            rid = self._record_reception(conn, source_key, received_at, blob, sha,
                                         header, APPLIED, [], applied=True)
            stats = self._apply(conn, source_key, header, entities, received_at,
                                rid, sha)
            diagnostics = [_problem(
                "summary",
                "applied %s at header timestamp %d: %d entity payload(s), "
                "%d withdrawal(s), %d stop event(s)"
                % (header.incrementality, header.timestamp, stats["upserts"],
                   stats["withdrawals"], stats["stop_events"]))]
            conn.execute("UPDATE rt_receptions SET diagnostics_json=? WHERE id=?",
                         (json.dumps(diagnostics, ensure_ascii=False), rid))
            conn.execute("COMMIT")
            return {"source_key": source_key, "category": APPLIED, "reception_id": rid,
                    "sha256": sha, "header_timestamp": header.timestamp,
                    "incrementality": header.incrementality, "applied": True,
                    "diagnostics": diagnostics,
                    "head_timestamp": header.timestamp, "baseline_seen": True,
                    "entities_upserted": stats["upserts"],
                    "entities_withdrawn": stats["withdrawals"],
                    "stop_events": stats["stop_events"]}
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _finish(self, conn, source_key: str, received_at: str, blob: bytes, sha: str,
                header: _Header | None, category: str, diagnostics: list[dict[str, Any]],
                *, original_id: int | None = None,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a non-applied reception, commit, and build the result."""
        rid = self._record_reception(conn, source_key, received_at, blob, sha, header,
                                     category, diagnostics, applied=False,
                                     original_id=original_id)
        conn.execute("COMMIT")
        result = {"source_key": source_key, "category": category, "reception_id": rid,
                  "sha256": sha,
                  "header_timestamp": header.timestamp if header else None,
                  "incrementality": header.incrementality if header else None,
                  "applied": False, "diagnostics": diagnostics}
        if extra:
            result.update(extra)
        return result

    def _record_reception(self, conn, source_key: str, received_at: str, blob: bytes,
                          sha: str, header: _Header | None, category: str,
                          diagnostics: list[dict[str, Any]], *, applied: bool,
                          original_id: int | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO rt_receptions(source_key, received_at, blob, sha256, size_bytes, "
            "header_timestamp, header_incrementality, category, applied, "
            "diagnostics_json, original_reception_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source_key, received_at, sqlite3.Binary(blob), sha, len(blob),
             header.timestamp if header else None,
             header.incrementality if header else None,
             category, 1 if applied else 0,
             json.dumps(diagnostics, ensure_ascii=False), original_id))
        return cur.lastrowid

    # ------------------------------------------------------------------
    # Parsing (pure protobuf work, no database state)
    # ------------------------------------------------------------------

    def _parse(self, blob: bytes):
        """Return (header, entities, problems).

        ``header`` is set exactly when the envelope is fully valid; entity
        problems then come back in ``problems`` with ``entities=None``.
        """
        pb = self._pb
        msg = pb.FeedMessage()
        try:
            msg.ParseFromString(blob)
        except self._mod.DecodeError as exc:
            return None, None, [_problem(
                "encoding", "bytes are not a valid GTFS Realtime FeedMessage: %s" % exc)]
        problems: list[dict[str, Any]] = []
        if not msg.HasField("header"):
            return None, None, [_problem("missing_header", "FeedMessage.header is required")]
        h = msg.header
        if not h.HasField("gtfs_realtime_version"):
            problems.append(_problem("missing_version",
                                     "header.gtfs_realtime_version is required",
                                     field="gtfs_realtime_version"))
        elif h.gtfs_realtime_version != "2.0":
            problems.append(_problem(
                "unsupported_version",
                "header.gtfs_realtime_version must be '2.0', got %r"
                % h.gtfs_realtime_version, field="gtfs_realtime_version"))
        if not h.HasField("incrementality"):
            problems.append(_problem("missing_incrementality",
                                     "header.incrementality must be set explicitly",
                                     field="incrementality"))
        if not h.HasField("timestamp"):
            problems.append(_problem("missing_timestamp", "header.timestamp is required",
                                     field="timestamp"))
        elif h.timestamp <= 0:
            problems.append(_problem("non_positive_timestamp",
                                     "header.timestamp must be positive, got %d"
                                     % h.timestamp, field="timestamp"))
        if problems:
            return None, None, problems
        header = _Header(h.timestamp, pb.FeedHeader.Incrementality.Name(h.incrementality))
        entities, ent_problems = self._parse_entities(msg, header)
        if ent_problems:
            return header, None, ent_problems
        return header, entities, []

    def _parse_entities(self, msg, header: _Header):
        problems: list[dict[str, Any]] = []
        entities: list[_Entity] = []
        seen: set[str] = set()
        for index, e in enumerate(msg.entity):
            where = "entity[%d]" % index
            if not e.HasField("id"):
                problems.append(_problem("missing_entity_id", "%s has no id" % where,
                                         field="id"))
                continue
            entity_id = e.id
            if entity_id == "":
                problems.append(_problem("empty_entity_id", "%s has an empty id" % where,
                                         field="id"))
                continue
            if entity_id in seen:
                problems.append(_problem(
                    "duplicate_entity_id",
                    "entity id %r appears more than once in the message" % entity_id,
                    entity_id=entity_id, field="id"))
                continue
            seen.add(entity_id)
            is_deleted = e.HasField("is_deleted") and e.is_deleted
            payloads = [name for name in ("trip_update",) + _OTHER_PAYLOADS
                        if e.HasField(name)]
            if is_deleted:
                if payloads:
                    problems.append(_problem(
                        "deleted_with_payload",
                        "entity %r has is_deleted=true and still carries %s"
                        % (entity_id, "+".join(payloads)),
                        entity_id=entity_id, field="is_deleted"))
                    continue
                if header.incrementality == _FULL:
                    problems.append(_problem(
                        "deleted_in_full_dataset",
                        "entity %r uses is_deleted inside a FULL_DATASET; withdrawal "
                        "by is_deleted is only valid in a DIFFERENTIAL message"
                        % entity_id, entity_id=entity_id, field="is_deleted"))
                    continue
                entities.append(_Entity(entity_id, True))
                continue
            if not payloads:
                problems.append(_problem(
                    "missing_payload",
                    "entity %r carries no trip_update (only trip_update entities are "
                    "accepted)" % entity_id, entity_id=entity_id))
                continue
            if payloads != ["trip_update"]:
                category = "mixed_entity" if "trip_update" in payloads \
                    else "unsupported_entity"
                problems.append(_problem(
                    category,
                    "entity %r carries %s; only trip_update entities are accepted"
                    % (entity_id, "+".join(payloads)),
                    entity_id=entity_id, field="+".join(payloads)))
                continue
            ent, ent_problems = self._parse_trip_update(entity_id, e.trip_update)
            problems.extend(ent_problems)
            if ent is not None:
                entities.append(ent)
        return entities, problems

    def _parse_trip_update(self, entity_id: str, tu):
        pb = self._pb
        problems: list[dict[str, Any]] = []
        if not tu.HasField("trip"):
            return None, [_problem("missing_trip",
                                   "entity %r trip_update has no TripDescriptor"
                                   % entity_id, entity_id=entity_id, field="trip")]
        t = tu.trip
        trip_id = None
        if not t.HasField("trip_id"):
            problems.append(_problem("missing_trip_id",
                                     "entity %r TripDescriptor has no trip_id"
                                     % entity_id, entity_id=entity_id, field="trip_id"))
        elif t.trip_id == "":
            problems.append(_problem("empty_trip_id",
                                     "entity %r TripDescriptor has an empty trip_id"
                                     % entity_id, entity_id=entity_id, field="trip_id"))
        else:
            trip_id = t.trip_id
        service_date = None
        if not t.HasField("start_date"):
            problems.append(_problem(
                "missing_start_date",
                "entity %r TripDescriptor has no start_date; a run instance needs an "
                "explicit service date" % entity_id, entity_id=entity_id,
                field="start_date"))
        else:
            day = parse_date(t.start_date)
            if day is None:
                problems.append(_problem(
                    "invalid_start_date",
                    "entity %r start_date %r is not a real YYYYMMDD calendar date"
                    % (entity_id, t.start_date), entity_id=entity_id,
                    field="start_date"))
            else:
                service_date = format_date(day)
        start_time = None
        if t.HasField("start_time"):
            if parse_time(t.start_time) is None:
                problems.append(_problem(
                    "invalid_start_time",
                    "entity %r start_time %r is not HH:MM:SS" % (entity_id, t.start_time),
                    entity_id=entity_id, field="start_time"))
            else:
                start_time = t.start_time
        trip_rel = "SCHEDULED"
        if t.HasField("schedule_relationship"):
            name = pb.TripDescriptor.ScheduleRelationship.Name(t.schedule_relationship)
            if name not in _TRIP_RELATIONSHIPS:
                problems.append(_problem(
                    "unsupported_trip_relationship",
                    "entity %r TripDescriptor.schedule_relationship=%s is not "
                    "supported (only SCHEDULED and CANCELED)" % (entity_id, name),
                    entity_id=entity_id, field="schedule_relationship"))
            else:
                trip_rel = name
        entity_timestamp = tu.timestamp if tu.HasField("timestamp") else None
        if trip_rel == "CANCELED" and len(tu.stop_time_update) > 0:
            problems.append(_problem(
                "canceled_with_stop_events",
                "entity %r is CANCELED but carries %d stop_time_update(s); a "
                "cancellation notice has no stop events"
                % (entity_id, len(tu.stop_time_update)),
                entity_id=entity_id, field="stop_time_update"))
        stops: list[_Stop] = []
        seen_seq: set[int] = set()
        for stu in tu.stop_time_update:
            if not stu.HasField("stop_sequence"):
                problems.append(_problem(
                    "missing_stop_sequence",
                    "entity %r has a stop_time_update without stop_sequence"
                    % entity_id, entity_id=entity_id, field="stop_sequence"))
                continue
            seq = stu.stop_sequence
            if seq in seen_seq:
                problems.append(_problem(
                    "duplicate_stop_sequence",
                    "entity %r lists stop_sequence %d twice" % (entity_id, seq),
                    entity_id=entity_id, field="stop_sequence"))
                continue
            seen_seq.add(seq)
            stop_rel = "SCHEDULED"
            if stu.HasField("schedule_relationship"):
                name = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                    stu.schedule_relationship)
                if name not in _STOP_RELATIONSHIPS:
                    problems.append(_problem(
                        "unsupported_stop_relationship",
                        "entity %r stop_sequence %d schedule_relationship=%s is not "
                        "supported (only SCHEDULED, SKIPPED, NO_DATA)"
                        % (entity_id, seq, name),
                        entity_id=entity_id, field="schedule_relationship"))
                    continue
                stop_rel = name
            has_arrival = stu.HasField("arrival")
            has_departure = stu.HasField("departure")
            if stop_rel in _NO_EVENT_RELATIONSHIPS and (has_arrival or has_departure):
                problems.append(_problem(
                    "event_on_non_scheduled_stop",
                    "entity %r stop_sequence %d is %s but carries arrival/departure "
                    "events" % (entity_id, seq, stop_rel),
                    entity_id=entity_id,
                    field="arrival" if has_arrival else "departure"))
                continue
            arrival = self._parse_event(stu.arrival, entity_id, seq, "arrival",
                                        problems) if has_arrival else None
            departure = self._parse_event(stu.departure, entity_id, seq, "departure",
                                          problems) if has_departure else None
            stops.append(_Stop(seq, stu.stop_id if stu.HasField("stop_id") else None,
                               stop_rel, arrival, departure))
        if problems:
            return None, problems
        ent = _Entity(entity_id, False)
        ent.trip_id = trip_id
        ent.service_date = service_date
        ent.start_time = start_time
        ent.trip_relationship = trip_rel
        ent.entity_timestamp = entity_timestamp
        ent.stops = stops
        return ent, []

    def _parse_event(self, ev, entity_id: str, seq: int, which: str,
                     problems: list[dict[str, Any]]) -> _Event:
        out = _Event()
        if ev.HasField("time"):
            out.time = (True, ev.time)
        if ev.HasField("delay"):
            out.delay = (True, ev.delay)
        if ev.HasField("uncertainty"):
            if ev.uncertainty < 0:
                problems.append(_problem(
                    "negative_uncertainty",
                    "entity %r stop_sequence %d %s.uncertainty is %d; uncertainty "
                    "must not be negative" % (entity_id, seq, which, ev.uncertainty),
                    entity_id=entity_id, field="%s.uncertainty" % which))
            else:
                out.uncertainty = (True, ev.uncertainty)
        return out

    # ------------------------------------------------------------------
    # Validation against the bound static edition and the current set
    # (runs inside the feed transaction)
    # ------------------------------------------------------------------

    def _validate_against_db(self, src, header: _Header, entities: list[_Entity]):
        problems: list[dict[str, Any]] = []
        feed_key, edition = src["feed_key"], src["edition"]
        updates = [e for e in entities if not e.is_deleted]
        trip_cache: dict[str, dict[int, str]] = {}
        for ent in updates:
            if ent.trip_id not in trip_cache:
                exists = self.conn.execute(
                    "SELECT 1 FROM trips WHERE feed_key=? AND edition=? AND trip_id=?",
                    (feed_key, edition, ent.trip_id)).fetchone()
                if exists is None:
                    trip_cache[ent.trip_id] = None
                else:
                    rows = self.conn.execute(
                        "SELECT stop_sequence, stop_id FROM stop_times "
                        "WHERE feed_key=? AND edition=? AND trip_id=?",
                        (feed_key, edition, ent.trip_id)).fetchall()
                    trip_cache[ent.trip_id] = {
                        r["stop_sequence"]: r["stop_id"] for r in rows}
            static_stops = trip_cache[ent.trip_id]
            if static_stops is None:
                problems.append(_problem(
                    "unknown_trip",
                    "entity %r refers to trip %r, which is not in the bound static "
                    "edition %s/%s" % (ent.entity_id, ent.trip_id, feed_key, edition),
                    entity_id=ent.entity_id, field="trip_id"))
                continue
            for stop in ent.stops:
                if stop.stop_sequence not in static_stops:
                    problems.append(_problem(
                        "unknown_stop_sequence",
                        "entity %r stop_sequence %d does not exist in trip %r of the "
                        "bound edition" % (ent.entity_id, stop.stop_sequence,
                                           ent.trip_id),
                        entity_id=ent.entity_id, field="stop_sequence"))
                elif (stop.stop_id is not None
                        and stop.stop_id != static_stops[stop.stop_sequence]):
                    problems.append(_problem(
                        "stop_id_mismatch",
                        "entity %r stop_sequence %d is stop %r in the bound edition, "
                        "not %r" % (ent.entity_id, stop.stop_sequence,
                                    static_stops[stop.stop_sequence], stop.stop_id),
                        entity_id=ent.entity_id, field="stop_id"))
        problems.extend(self._instance_conflicts(src, header, entities))
        return problems

    def _instance_conflicts(self, src, header: _Header, entities: list[_Entity]):
        """Ownership of run instances after this message, checked as a set.

        Entity order inside the message is irrelevant: deletions and replaced
        entities free their instances first, then every payload claims its
        instance exactly once.
        """
        problems: list[dict[str, Any]] = []
        owner: dict[tuple[str, str, str], str] = {}
        if header.incrementality != _FULL:
            for row in self.conn.execute(
                    "SELECT entity_id, trip_id, service_date, start_time FROM "
                    "rt_entities WHERE source_key=?", (src["source_key"],)):
                owner[(row["trip_id"], row["service_date"], row["start_time"])] = \
                    row["entity_id"]
            freed = {e.entity_id for e in entities}  # deleted and replaced alike
            for key, holder in list(owner.items()):
                if holder in freed:
                    del owner[key]
        for ent in entities:
            if ent.is_deleted:
                continue
            key = (ent.trip_id, ent.service_date, ent.start_time or "")
            holder = owner.get(key)
            if holder is not None and holder != ent.entity_id:
                problems.append(_problem(
                    "instance_conflict",
                    "run instance (trip=%s, date=%s, start_time=%s) is already "
                    "represented by entity %r; entity %r cannot take it over in the "
                    "same set" % (ent.trip_id, ent.service_date,
                                  ent.start_time or "-", holder, ent.entity_id),
                    entity_id=ent.entity_id, field="trip_id"))
            else:
                owner[key] = ent.entity_id
        return problems

    # ------------------------------------------------------------------
    # Application (inside the feed transaction)
    # ------------------------------------------------------------------

    def _apply(self, conn, source_key: str, header: _Header, entities: list[_Entity],
               received_at: str, reception_id: int, sha: str) -> dict[str, int]:
        deletes = [e.entity_id for e in entities if e.is_deleted]
        updates = [e for e in entities if not e.is_deleted]
        withdrawals = 0
        if header.incrementality == _FULL:
            existing = {r[0] for r in conn.execute(
                "SELECT entity_id FROM rt_entities WHERE source_key=?", (source_key,))}
            withdrawals = len(existing - {e.entity_id for e in updates})
            conn.execute("DELETE FROM rt_stop_events WHERE source_key=?", (source_key,))
            conn.execute("DELETE FROM rt_entities WHERE source_key=?", (source_key,))
        else:
            # Deletions and replacements free their rows first; only then are
            # the new payloads inserted, so entity order cannot matter.
            for entity_id in deletes:
                withdrawals += conn.execute(
                    "DELETE FROM rt_entities WHERE source_key=? AND entity_id=?",
                    (source_key, entity_id)).rowcount
                conn.execute(
                    "DELETE FROM rt_stop_events WHERE source_key=? AND entity_id=?",
                    (source_key, entity_id))
            for ent in updates:
                conn.execute(
                    "DELETE FROM rt_stop_events WHERE source_key=? AND entity_id=?",
                    (source_key, ent.entity_id))
                conn.execute(
                    "DELETE FROM rt_entities WHERE source_key=? AND entity_id=?",
                    (source_key, ent.entity_id))
        stop_events = 0
        for ent in updates:
            conn.execute(
                "INSERT INTO rt_entities(source_key, entity_id, trip_id, service_date, "
                "start_time, trip_schedule_relationship, entity_timestamp, "
                "head_timestamp, reception_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (source_key, ent.entity_id, ent.trip_id, ent.service_date,
                 ent.start_time or "", ent.trip_relationship, ent.entity_timestamp,
                 header.timestamp, reception_id))
            for stop in ent.stops:
                arr, dep = stop.arrival, stop.departure
                conn.execute(
                    "INSERT INTO rt_stop_events(source_key, entity_id, stop_sequence, "
                    "stop_id, schedule_relationship, has_arrival, arr_time, arr_delay, "
                    "arr_uncertainty, has_departure, dep_time, dep_delay, "
                    "dep_uncertainty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (source_key, ent.entity_id, stop.stop_sequence, stop.stop_id,
                     stop.relationship,
                     1 if arr is not None else 0,
                     arr.time[1] if arr is not None and arr.time[0] else None,
                     arr.delay[1] if arr is not None and arr.delay[0] else None,
                     arr.uncertainty[1]
                     if arr is not None and arr.uncertainty[0] else None,
                     1 if dep is not None else 0,
                     dep.time[1] if dep is not None and dep.time[0] else None,
                     dep.delay[1] if dep is not None and dep.delay[0] else None,
                     dep.uncertainty[1]
                     if dep is not None and dep.uncertainty[0] else None))
                stop_events += 1
        conn.execute(
            "INSERT INTO rt_head_ledger(source_key, header_timestamp, sha256, "
            "reception_id, applied_at) VALUES(?,?,?,?,?)",
            (source_key, header.timestamp, sha, reception_id, received_at))
        conn.execute(
            "UPDATE rt_sources SET head_timestamp=?, head_incrementality=?, "
            "head_reception_id=?, baseline_seen=1 WHERE source_key=?",
            (header.timestamp, header.incrementality, reception_id, source_key))
        return {"upserts": len(updates), "withdrawals": withdrawals,
                "stop_events": stop_events}

    # ------------------------------------------------------------------
    # Reading current notices
    # ------------------------------------------------------------------

    def notices(self, source_key: str, now: int, max_age: int, *,
                include_expired: bool = False) -> dict[str, Any]:
        """Read current notices; by default only fresh ones.

        An entity is fresh while ``now < entity_head_timestamp + max_age``
        (the boundary ``now == ts + max_age`` is expired).  With
        ``include_expired=True`` everything is returned and flagged, but
        ``now`` must not be behind the source head: this is not a historical
        view.
        """
        if isinstance(now, bool) or not isinstance(now, int):
            raise GtfsError("now must be an integer number of seconds")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
            raise GtfsError("max_age must be a positive integer number of seconds")
        src = self._require_source(source_key)
        head = src["head_timestamp"]
        if include_expired and head is not None and now < head:
            raise GtfsError(
                "now=%d is behind the source head timestamp=%d; the include-expired "
                "read does not provide a historical view" % (now, head))
        entities = self.conn.execute(
            "SELECT e.*, r.sha256 AS reception_sha256 FROM rt_entities e "
            "JOIN rt_receptions r ON r.id = e.reception_id "
            "WHERE e.source_key=? ORDER BY e.entity_id", (source_key,)).fetchall()
        stops = self.conn.execute(
            "SELECT * FROM rt_stop_events WHERE source_key=? "
            "ORDER BY entity_id, stop_sequence", (source_key,)).fetchall()
        stops_by_entity: dict[str, list] = {}
        for row in stops:
            stops_by_entity.setdefault(row["entity_id"], []).append(row)
        static_cache: dict[str, dict[int, str]] = {}
        notices = []
        for ent in entities:
            expires_at = ent["head_timestamp"] + max_age
            fresh = now < expires_at
            if not include_expired and not fresh:
                continue
            if ent["trip_id"] not in static_cache:
                rows = self.conn.execute(
                    "SELECT stop_sequence, stop_id FROM stop_times WHERE feed_key=? "
                    "AND edition=? AND trip_id=?",
                    (src["feed_key"], src["edition"], ent["trip_id"])).fetchall()
                static_cache[ent["trip_id"]] = {
                    r["stop_sequence"]: r["stop_id"] for r in rows}
            static_stops = static_cache[ent["trip_id"]]
            notices.append({
                "entity_id": ent["entity_id"],
                "trip_id": ent["trip_id"],
                "service_date": ent["service_date"],
                "start_time": ent["start_time"] or None,
                "trip_schedule_relationship": ent["trip_schedule_relationship"],
                "entity_timestamp": ent["entity_timestamp"],
                "replaced_at_header_timestamp": ent["head_timestamp"],
                "reception_id": ent["reception_id"],
                "reception_sha256": ent["reception_sha256"],
                "fresh": fresh,
                "expires_at": expires_at,
                "stop_events": [self._stop_dict(s, static_stops)
                                for s in stops_by_entity.get(ent["entity_id"], [])],
            })
        return {"source_key": source_key, "now": now, "max_age": max_age,
                "include_expired": include_expired,
                "head_timestamp": head, "notices": notices}

    @staticmethod
    def _event_dict(has_event: int, time_val, delay_val, uncertainty_val):
        if not has_event:
            return None
        return {"time": time_val, "delay": delay_val, "uncertainty": uncertainty_val}

    def _stop_dict(self, row, static_stops: dict[int, str]) -> dict[str, Any]:
        return {
            "stop_sequence": row["stop_sequence"],
            "stop_id": row["stop_id"],
            "static_stop_id": static_stops.get(row["stop_sequence"]),
            "schedule_relationship": row["schedule_relationship"],
            "arrival": self._event_dict(row["has_arrival"], row["arr_time"],
                                        row["arr_delay"], row["arr_uncertainty"]),
            "departure": self._event_dict(row["has_departure"], row["dep_time"],
                                          row["dep_delay"], row["dep_uncertainty"]),
        }

    # ------------------------------------------------------------------
    # Reception ledger views
    # ------------------------------------------------------------------

    def receptions(self, source_key: str, limit: int | None = None) -> list[dict[str, Any]]:
        self._require_source(source_key)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, source_key, received_at, sha256, size_bytes, header_timestamp, "
            "header_incrementality, category, applied, diagnostics_json, "
            "original_reception_id FROM rt_receptions WHERE source_key=? ORDER BY id",
            (source_key,)).fetchall()]
        if limit is not None:
            rows = rows[-limit:]
        for row in rows:
            row["applied"] = bool(row["applied"])
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
        return rows

    def reception_detail(self, reception_id: int, *,
                         include_blob: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM rt_receptions WHERE id=?", (reception_id,)).fetchone()
        if row is None:
            raise GtfsError("no reception with id %d" % reception_id)
        out = dict(row)
        out["applied"] = bool(out["applied"])
        out["diagnostics"] = json.loads(out.pop("diagnostics_json"))
        blob = bytes(out.pop("blob"))
        if include_blob:
            out["blob"] = blob
        return out

    def ledger(self, source_key: str) -> list[dict[str, Any]]:
        """The success ledger: one row per applied (source, header timestamp)."""
        self._require_source(source_key)
        rows = self.conn.execute(
            "SELECT source_key, header_timestamp, sha256, reception_id, applied_at "
            "FROM rt_head_ledger WHERE source_key=? ORDER BY header_timestamp",
            (source_key,)).fetchall()
        return [dict(r) for r in rows]
