"""Three-way merge of relational JSONL dictionaries into a self-contained
SQLite work library, then a decided finalization into an output JSONL.

The operator owns three UTF-8 JSONL files -- a shared base draft and two
independent revisions (left/right) -- plus the list of primary-key fields.
The job splits into two explicit operations:

* :func:`compare` parses the three inputs once, persists every row (with its
  physical source line number) and the key definition into a brand-new work
  database, computes the three-way merge on disk and records the auto-accepted
  rows, the auto-merged field suggestions and the *real* conflicts.  The work
  library is self-contained: the original input files can be moved away,
  review and finalization still work.
* :func:`finalize` reopens a work library plus a JSONL decision file, checks
  every decision (identity, category, state fingerprint and payload) and only
  writes the final dictionary when every conflict is correctly resolved; an
  empty decision file is legal exactly when there are no open conflicts.

This is a relational merge, not a text diff and not a Git wrapper.  It never
reads schedules, realtime notices, GPS, CAN captures or rollup cells, and it
does not maintain a long-running revision service.  One compare = one new
work library (an existing target is never overwritten).

Value model
-----------
Each non-key field has states that never collapse into one another: present
string, present integer (any size, bool is tagged separately from int),
present boolean, present JSON null, or *absent* (the field name is not on the
row).  Absence is the absence of an EAV row, so it stays distinct from
``null``, ``""``, ``0`` and ``false`` through SQLite.

Conflict identity
-----------------
Each conflict id is a SHA-256 hex digest over a canonical encoding of the
*states that cause the conflict* (category, key, optional field, and the
base/left/right states with presence and typed values).  The three states are
encoded as an unordered collection, so swapping left and right leaves every
digest untouched.  Source line numbers are stored for display but take no
part in the identity, so reordering an input file changes line numbers
without changing conflict ids.  Every decision carries the same digest as
``state_fingerprint``; a decision whose id exists but whose fingerprint no
longer matches is rejected as stale, and decisions only carry over between
work libraries when both identity and states are byte-identical.

Memory contract
---------------
Inputs stream one physical line at a time into bounded batches (even the
verbatim snapshots are written as fixed-size chunks); Python never holds all
rows, all keys or all conflicts, and duplicate-key detection relies on the
SQLite unique index instead of a Python key set.  The merge walks the
distinct key tuples with an ORDER BY cursor (disk temp b-trees,
``temp_store=FILE``) and keeps only the current key's three field maps plus
one insert batch.  Review is LIMIT/OFFSET paging over indexed tables and
finalization streams the decisions and the output rows.  See
docs/DICTMERGE.md for the table layout, type encoding and complexity notes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from .errors import GtfsError

SCHEMA_VERSION = "1"

#: Rows per transaction while loading and merging.
DEFAULT_BATCH_SIZE = 4096
#: SQLite page cache (KiB) handed to every connection of the work library.
DEFAULT_CACHE_KIB = 16384
#: Byte size of each verbatim input snapshot chunk (bounds snapshot memory).
SNAPSHOT_CHUNK = 1 << 20

_TMP_SUFFIX = ".tmp-"

# Conflict categories ---------------------------------------------------------
FIELD_CONFLICT = "field_conflict"
ADD_COLLISION = "add_collision"
DELETE_MODIFY = "delete_modify"

# Presence / value type tags --------------------------------------------------
MISSING = "missing"   # field absent from the row (distinct from JSON null)
V_NULL = "null"
V_STR = "str"
V_INT = "int"
V_BOOL = "bool"
SIDE_TAGS = ("base", "left", "right")

# Outcome of a row in the merged materialization -----------------------------
OUT_KEEP = "keep"
OUT_DELETE = "delete"


class DictMergeError(GtfsError):
    """An input, work-library, decision or finalization problem."""

    exit_code = 1


# ---------------------------------------------------------------------------
# Strict JSONL parsing
# ---------------------------------------------------------------------------

class _DupKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DupKey(key)
        obj[key] = value
    return obj


def scalar_type(value: Any) -> str:
    """Type tag for an allowed non-key value; rejects floats/lists/objects."""
    if value is None:
        return V_NULL
    if type(value) is bool:
        return V_BOOL
    if type(value) is int:
        return V_INT
    if type(value) is str:
        return V_STR
    raise DictMergeError("value must be a string, integer, boolean or null; "
                         "decimals, arrays and objects are not accepted")


def iter_jsonl(path: Path, label: str) -> Iterator[tuple[int, dict[str, Any]]]:
    """Stream (1-based physical line number, strict object) from one JSONL."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError as exc:
        raise DictMergeError("%s file not found: %s" % (label, path)) from exc
    with handle:
        for line_no, raw in enumerate(handle, start=1):
            where = "%s line %d" % (label, line_no)
            if not raw.strip():
                raise DictMergeError("%s: empty line is not a record" % where)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DictMergeError("%s: line is not valid UTF-8: %s"
                                     % (where, exc)) from exc
            try:
                obj = json.loads(text, object_pairs_hook=_strict_object)
            except _DupKey as exc:
                raise DictMergeError("%s: duplicate JSON member %r"
                                     % (where, exc.key)) from exc
            except json.JSONDecodeError as exc:
                raise DictMergeError("%s: invalid JSON: %s" % (where, exc)) from exc
            if type(obj) is not dict:
                raise DictMergeError("%s: each line must be a JSON object" % where)
            yield line_no, obj


def validate_record(obj: dict[str, Any], key_fields: Sequence[str],
                    label: str, line_no: int) -> None:
    """Key components: present, non-empty strings, compared literally.

    No trimming, no leading-zero stripping; the component boundary stays
    meaningful because each component is stored in its own column.
    """
    where = "%s line %d" % (label, line_no)
    for field in key_fields:
        if field not in obj:
            raise DictMergeError("%s: missing primary-key field %r" % (where, field))
        value = obj[field]
        if type(value) is not str:
            raise DictMergeError("%s field %r: primary-key component must be a "
                                 "string" % (where, field))
        if value == "":
            raise DictMergeError("%s field %r: primary-key component must be "
                                 "non-empty" % (where, field))
    for field, value in obj.items():
        if field not in key_fields:
            scalar_type(value)


# ---------------------------------------------------------------------------
# Typed value encoding (the states stay distinguishable in SQLite)
# ---------------------------------------------------------------------------

def encode_value(value: Any) -> tuple[str, str | None]:
    """Python scalar -> (val_type, val_text).  Missing is handled separately."""
    tag = scalar_type(value)
    if tag == V_NULL:
        return V_NULL, None
    if tag == V_BOOL:
        return V_BOOL, "true" if value else "false"
    if tag == V_INT:
        return V_INT, str(value)          # arbitrary precision, exact text
    return V_STR, value


def decode_value(val_type: str, val_text: str | None) -> Any:
    if val_type == V_NULL:
        return None
    if val_type == V_BOOL:
        return val_text == "true"
    if val_type == V_INT:
        return int(val_text)              # exact, even beyond 64/128 bits
    if val_type == V_STR:
        return val_text
    raise DictMergeError("internal: unknown value type %r" % val_type)


def canonical_state(val_type: str, val_text: str | None) -> dict[str, Any]:
    """Canonical JSON-able encoding of one field state (missing included)."""
    if val_type == MISSING:
        return {"present": False}
    if val_type == V_NULL:
        return {"present": True, "type": V_NULL, "value": None}
    if val_type == V_BOOL:
        return {"present": True, "type": V_BOOL,
                "value": val_text == "true"}
    if val_type == V_INT:
        return {"present": True, "type": V_INT, "value": val_text}
    return {"present": True, "type": V_STR, "value": val_text}


def _canonical_row(field_map: dict[str, tuple[str, str | None]]) -> list[list[Any]]:
    return [[name, canonical_state(*field_map[name])]
            for name in sorted(field_map)]


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _state_sort_key(state: Any) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def field_conflict_id(key: Sequence[str], field: str,
                      states: dict[str, tuple[str, str | None]]) -> str:
    """Identity bound to the three field states, not to line numbers.

    The states are sorted into an unordered collection so swapping left and
    right leaves the digest untouched.
    """
    payload = {
        "category": FIELD_CONFLICT,
        "key": list(key),
        "field": field,
        "states": sorted((canonical_state(*state) for state in states.values()),
                         key=_state_sort_key),
    }
    return _digest(payload)


def row_conflict_id(category: str, key: Sequence[str],
                    states: dict[str, dict[str, tuple[str, str | None]] | None]
                    ) -> str:
    """Identity for an add-collision / delete-vs-modify whole-row conflict.

    Presence plus full field maps, again as an unordered collection: which
    revision is called left is irrelevant to the identity.
    """
    encoded = []
    for field_map in states.values():
        if field_map is None:
            encoded.append({"present": False})
        else:
            encoded.append({"present": True,
                            "fields": _canonical_row(field_map)})
    payload = {"category": category, "key": list(key),
               "states": sorted(encoded, key=_state_sort_key)}
    return _digest(payload)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _build_ddl(key_fields: Sequence[str]) -> str:
    key_cols = ",\n    ".join("%s TEXT NOT NULL" % _quote_ident(name)
                              for name in key_fields)
    key_col_list = ", ".join(_quote_ident(name) for name in key_fields)
    return f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The exact key definition this work library was built with.
CREATE TABLE key_def (
    ordinal INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- Verbatim input metadata.  The bytes themselves live in input_chunk, split
-- into fixed-size chunks so loading never holds a whole input in Python.
CREATE TABLE input_snapshot (
    side TEXT PRIMARY KEY CHECK (side IN ('base', 'left', 'right')),
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL
);
CREATE TABLE input_chunk (
    side TEXT NOT NULL REFERENCES input_snapshot(side),
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (side, seq)
);

-- One row per (side, primary key).  Absence of a row = that side deleted /
-- never had the key.  Key components occupy separate columns: the component
-- boundary stays meaningful, nothing is concatenated.
CREATE TABLE keys (
    key_id INTEGER PRIMARY KEY,
    side TEXT NOT NULL CHECK (side IN ('base', 'left', 'right')),
    {key_cols},
    UNIQUE (side, {key_col_list})
);

CREATE TABLE fields (
    field_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- Physical source line numbers, one per present (side, key) record.
CREATE TABLE records (
    side TEXT NOT NULL,
    key_id INTEGER NOT NULL REFERENCES keys(key_id),
    line_no INTEGER NOT NULL,
    PRIMARY KEY (side, key_id)
);

-- EAV field values.  Absence is the absence of a (key_id, field_id) row,
-- which keeps "field deleted" distinct from an explicit null.
CREATE TABLE values_eav (
    side TEXT NOT NULL,
    key_id INTEGER NOT NULL REFERENCES keys(key_id),
    field_id INTEGER NOT NULL REFERENCES fields(field_id),
    val_type TEXT NOT NULL CHECK (val_type IN ('null', 'str', 'int', 'bool')),
    val_text TEXT,
    PRIMARY KEY (side, key_id, field_id)
);
CREATE INDEX idx_values_key ON values_eav (key_id, field_id);

-- One row per distinct primary key across the three files; review order is
-- always the key columns in SQLite BINARY (UTF-8 byte == code point) order.
CREATE TABLE merged_rows (
    key_id INTEGER PRIMARY KEY REFERENCES keys(key_id),
    {key_cols},
    status TEXT NOT NULL CHECK (status IN ('auto', 'conflict')),
    outcome TEXT NOT NULL CHECK (outcome IN ('keep', 'delete')),
    conflict_count INTEGER NOT NULL
);
CREATE INDEX idx_merged_status ON merged_rows (status, {key_col_list});

-- Auto-accepted field materialization plus per-field suggestions for rows
-- that still carry a conflict (non-conflicting fields are merged anyway).
CREATE TABLE merged_values (
    key_id INTEGER NOT NULL REFERENCES merged_rows(key_id),
    field_id INTEGER NOT NULL REFERENCES fields(field_id),
    val_type TEXT NOT NULL CHECK (val_type IN ('null', 'str', 'int', 'bool')),
    val_text TEXT,
    auto_source TEXT NOT NULL
        CHECK (auto_source IN ('base', 'left', 'right', 'both')),
    PRIMARY KEY (key_id, field_id)
);

CREATE TABLE conflicts (
    conflict_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK (category IN
        ('field_conflict', 'add_collision', 'delete_modify')),
    key_id INTEGER NOT NULL REFERENCES merged_rows(key_id),
    field_id INTEGER REFERENCES fields(field_id)
);
CREATE INDEX idx_conflicts_key ON conflicts (key_id);
-- At most one field conflict per (key, field); whole-row conflicts carry a
-- null field, so the uniqueness target is a partial index.
CREATE UNIQUE INDEX idx_conflicts_field_unique ON conflicts (key_id, field_id)
    WHERE field_id IS NOT NULL;

-- Applied decisions stay in the work library as an audit trail.
CREATE TABLE decisions (
    conflict_id TEXT PRIMARY KEY REFERENCES conflicts(conflict_id),
    ordinal INTEGER NOT NULL,
    choice TEXT NOT NULL,
    custom_json TEXT,
    fingerprint TEXT NOT NULL
);
"""


def _connect(path: str | os.PathLike[str], *, writable: bool,
             cache_kib: int = DEFAULT_CACHE_KIB) -> sqlite3.Connection:
    # Plain path (no file: URI) so absolute and relative paths behave the
    # same.  Read-only callers open with mode=ro through a URI built from an
    # absolute POSIX path, which always has the required file:/// shape.
    if writable:
        conn = sqlite3.connect(str(path), isolation_level=None)
    else:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-%d" % cache_kib)
    if writable:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_work(path: str | os.PathLike[str]) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_file():
        raise DictMergeError("work library not found: %s" % p)
    try:
        conn = _connect(p, writable=False)
    except sqlite3.DatabaseError as exc:
        raise DictMergeError("%s is not a valid work library: %s" % (p, exc)) \
            from exc
    try:
        status = conn.execute("SELECT value FROM meta WHERE key='status'") \
            .fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DictMergeError("%s is not a valid work library: %s" % (p, exc)) \
            from exc
    if status is None or status["value"] != "complete":
        conn.close()
        raise DictMergeError("%s is not a complete work library; review "
                             "refuses to open a partial build" % p)
    return conn


def _key_fields_of(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM key_def ORDER BY ordinal")]


# ---------------------------------------------------------------------------
# Compare (operation 1)
# ---------------------------------------------------------------------------

def compare(base_path: str | os.PathLike[str],
            left_path: str | os.PathLike[str],
            right_path: str | os.PathLike[str],
            key_fields: Sequence[str],
            out_path: str | os.PathLike[str],
            *, batch_size: int = DEFAULT_BATCH_SIZE,
            cache_kib: int = DEFAULT_CACHE_KIB) -> dict[str, Any]:
    """Build the work library for one (base, left, right) triple.

    Refuses when the output path already exists.  Any parse/validation error
    aborts the whole run and removes the temporary database: a failed compare
    never leaves a reviewable library behind.
    """
    base_path, left_path, right_path, out_path = (
        Path(base_path), Path(left_path), Path(right_path), Path(out_path))
    if not key_fields or any(type(name) is not str or name == ""
                             for name in key_fields):
        raise DictMergeError("key_fields must be one or more non-empty strings")
    if len(set(key_fields)) != len(key_fields):
        raise DictMergeError("key_fields must not contain duplicates")
    if batch_size < 1:
        raise DictMergeError("batch_size must be >= 1")
    if cache_kib < 128:
        raise DictMergeError("cache_kib must be >= 128")
    if out_path.exists():
        raise DictMergeError("work library %s already exists; refusing to "
                             "overwrite it" % out_path)
    for label, path in (("base", base_path), ("left", left_path),
                        ("right", right_path)):
        if not path.is_file():
            raise DictMergeError("%s file not found: %s" % (label, path))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in out_path.parent.glob(out_path.name + _TMP_SUFFIX + "*"):
        try:
            stale.unlink()
        except OSError:
            pass
    tmp_path = out_path.with_name(out_path.name + _TMP_SUFFIX + str(os.getpid()))

    conn = sqlite3.connect(str(tmp_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    line_counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    try:
        conn.execute("PRAGMA journal_mode=OFF")    # temp file: discarded on failure
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-%d" % cache_kib)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_build_ddl(key_fields))
        conn.executemany("INSERT INTO key_def(ordinal, name) VALUES (?, ?)",
                         list(enumerate(key_fields)))
        for side, path in (("base", base_path), ("left", left_path),
                           ("right", right_path)):
            n, digest = _load_side(conn, side, path, key_fields, batch_size)
            line_counts[side] = n
            hashes[side] = digest
        stats = _merge_all(conn, key_fields, batch_size)
        import datetime as dt
        meta = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"),
            "key_fields": json.dumps(list(key_fields), ensure_ascii=False),
            "base_file": base_path.name,
            "left_file": left_path.name,
            "right_file": right_path.name,
            "base_sha256": hashes["base"],
            "left_sha256": hashes["left"],
            "right_sha256": hashes["right"],
            "batch_size": str(batch_size),
            "cache_kib": str(cache_kib),
            "line_count_base": str(line_counts["base"]),
            "line_count_left": str(line_counts["left"]),
            "line_count_right": str(line_counts["right"]),
            **{k: str(v) for k, v in stats.items()},
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                         sorted(meta.items()))
    except BaseException:
        conn.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    conn.close()
    # Atomic publish; hard-link fails rather than replacing an existing file.
    try:
        os.link(tmp_path, out_path)
    except FileExistsError as exc:
        tmp_path.unlink()
        raise DictMergeError("work library %s already exists; refusing to "
                             "overwrite it" % out_path) from exc
    tmp_path.unlink()
    return info(out_path)


def _stream_snapshot(path: Path, side: str, conn: sqlite3.Connection
                     ) -> str:
    """Hash and persist one input in fixed-size chunks (bounded memory)."""
    import codecs

    # Parent row first so the chunk foreign key is always satisfied.
    conn.execute(
        "INSERT INTO input_snapshot(side, file_name, sha256, byte_length) "
        "VALUES (?, ?, '', 0)", (side, path.name))
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    total = 0
    seq = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(SNAPSHOT_CHUNK)
            if not chunk:
                text = decoder.decode(b"", final=True)
                if text:
                    conn.execute(
                        "INSERT INTO input_chunk(side, seq, content) "
                        "VALUES (?, ?, ?)", (side, seq, text))
                break
            digest.update(chunk)
            total += len(chunk)
            # Incremental decode keeps a multi-byte character split across
            # two byte chunks from raising or corrupting the snapshot.
            text = decoder.decode(chunk)
            if text:
                conn.execute(
                    "INSERT INTO input_chunk(side, seq, content) "
                    "VALUES (?, ?, ?)", (side, seq, text))
            seq += 1
    conn.execute(
        "UPDATE input_snapshot SET sha256=?, byte_length=? WHERE side=?",
        (digest.hexdigest(), total, side))
    return digest.hexdigest()


def _load_side(conn: sqlite3.Connection, side: str, path: Path,
               key_fields: Sequence[str], batch_size: int) -> tuple[int, int]:
    """Stream one JSONL into keys/records/values_eav in bounded transactions.

    Duplicate primary keys are rejected by the SQL unique index, not by a
    Python set, so the full key universe never accumulates in memory.
    """
    digest = _stream_snapshot(path, side, conn)
    key_cols = [_quote_ident(name) for name in key_fields]
    insert_key = ("INSERT INTO keys(side, %s) VALUES (%s)"
                  % (", ".join(key_cols), ", ".join("?" * (len(key_cols) + 1))))
    # Field ids are schema-width metadata (not row data); cached to avoid a
    # lookup per cell.
    field_cache: dict[str, int] = {
        r["name"]: r["field_id"]
        for r in conn.execute("SELECT field_id, name FROM fields")}
    val_batch: list[tuple[Any, ...]] = []
    line_count = 0
    conn.execute("BEGIN")
    try:
        for line_no, obj in iter_jsonl(path, side):
            validate_record(obj, key_fields, side, line_no)
            key_values = tuple(obj[name] for name in key_fields)
            try:
                cur = conn.execute(insert_key, (side, *key_values))
            except sqlite3.IntegrityError as exc:
                raise DictMergeError(
                    "%s line %d: duplicate primary key %r"
                    % (side, line_no, list(key_values))) from exc
            key_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO records(side, key_id, line_no) VALUES (?, ?, ?)",
                (side, key_id, line_no))
            for field, value in obj.items():
                if field in key_fields:
                    continue
                field_id = field_cache.get(field)
                if field_id is None:
                    conn.execute("INSERT OR IGNORE INTO fields(name) VALUES (?)",
                                 (field,))
                    field_id = int(conn.execute(
                        "SELECT field_id FROM fields WHERE name=?",
                        (field,)).fetchone()["field_id"])
                    field_cache[field] = field_id
                val_type, val_text = encode_value(value)
                val_batch.append((side, key_id, field_id, val_type, val_text))
            line_count += 1
            if line_count % batch_size == 0:
                conn.executemany(
                    "INSERT INTO values_eav(side, key_id, field_id, val_type, "
                    "val_text) VALUES (?, ?, ?, ?, ?)", val_batch)
                val_batch.clear()
                conn.execute("COMMIT")
                conn.execute("BEGIN")
        if val_batch:
            conn.executemany(
                "INSERT INTO values_eav(side, key_id, field_id, val_type, "
                "val_text) VALUES (?, ?, ?, ?, ?)", val_batch)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return line_count, digest


# ---------------------------------------------------------------------------
# Pure merge rule for one key (exercised directly by the unit tests)
# ---------------------------------------------------------------------------

FieldMap = dict[str, tuple[str, str | None]]
SideMaps = dict[str, FieldMap | None]


def plan_key(key: Sequence[str], maps: SideMaps) -> dict[str, Any]:
    """Apply the three-way merge rule to one primary key.

    Returns ``{"outcome", "merged_fields", "conflicts"}`` where
    ``merged_fields`` maps field name to (val_type, val_text, auto_source).
    """
    base, left, right = maps["base"], maps["left"], maps["right"]
    merged: dict[str, tuple[str, str | None, str]] = {}
    conflicts: list[dict[str, Any]] = []

    if base is None:
        # Key absent from the base draft.
        if left is not None and right is None:
            for f in sorted(left):
                merged[f] = (*left[f], "left")
        elif right is not None and left is None:
            for f in sorted(right):
                merged[f] = (*right[f], "right")
        elif left is not None and right is not None:
            if left == right:
                for f in sorted(left):
                    merged[f] = (*left[f], "both")
            else:
                conflicts.append({
                    "category": ADD_COLLISION,
                    "id": row_conflict_id(ADD_COLLISION, key, maps),
                    "field": None})
                # Two different new objects are never fused; but fields that
                # happen to agree still carry an automatic suggestion.
                for f in sorted(set(left) | set(right)):
                    ls, rs = left.get(f), right.get(f)
                    if ls is not None and ls == rs:
                        merged[f] = (*ls, "both")
                    elif ls is not None and rs is None:
                        merged[f] = (*ls, "left")
                    elif rs is not None and ls is None:
                        merged[f] = (*rs, "right")
        return {"outcome": OUT_KEEP, "merged_fields": merged,
                "conflicts": conflicts}

    # Key exists in the base draft.
    left_deleted = left is None
    right_deleted = right is None
    if left_deleted and right_deleted:
        return {"outcome": OUT_DELETE, "merged_fields": {}, "conflicts": []}
    if left_deleted or right_deleted:
        survivor_side = "right" if left_deleted else "left"
        survivor = right if left_deleted else left
        assert survivor is not None
        if survivor == base:
            # One side deleted, the other kept the base untouched.
            return {"outcome": OUT_DELETE, "merged_fields": {},
                    "conflicts": []}
        conflicts.append({
            "category": DELETE_MODIFY,
            "id": row_conflict_id(DELETE_MODIFY, key, maps),
            "field": None})
        for f in sorted(set(base) | set(survivor)):
            bs, ss = base.get(f), survivor.get(f)
            if ss is None:
                continue  # survivor-side deletion stays an open suggestion
            merged[f] = (*ss, "base" if bs == ss else survivor_side)
        return {"outcome": OUT_KEEP, "merged_fields": merged,
                "conflicts": conflicts}

    # Both sides kept the row: every non-key field is compared independently
    # with absence as a first-class comparable state.
    for field in sorted(set(base) | set(left) | set(right)):
        bs = base.get(field, (MISSING, None))
        ls = left.get(field, (MISSING, None))
        rs = right.get(field, (MISSING, None))
        states = {"base": bs, "left": ls, "right": rs}
        if ls == rs:
            if ls[0] == MISSING:
                continue  # both dropped it: the field stays absent
            merged[field] = (*ls, "both" if ls != bs else "base")
        elif ls == bs:
            if rs[0] == MISSING:
                continue  # only right deleted the field -> stays absent
            merged[field] = (*rs, "right")
        elif rs == bs:
            if ls[0] == MISSING:
                continue  # only left deleted the field -> stays absent
            merged[field] = (*ls, "left")
        else:
            conflicts.append({
                "category": FIELD_CONFLICT,
                "id": field_conflict_id(key, field, states),
                "field": field})
    return {"outcome": OUT_KEEP, "merged_fields": merged,
            "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Disk-driven merge
# ---------------------------------------------------------------------------

def _field_map(conn: sqlite3.Connection, key_id: int) -> FieldMap:
    out: FieldMap = {}
    for r in conn.execute(
            "SELECT f.name AS name, v.val_type AS val_type, "
            "v.val_text AS val_text FROM values_eav v "
            "JOIN fields f ON f.field_id = v.field_id WHERE v.key_id=?",
            (key_id,)):
        out[r["name"]] = (r["val_type"], r["val_text"])
    return out


def _merge_all(conn: sqlite3.Connection, key_fields: Sequence[str],
               batch_size: int) -> dict[str, int]:
    """Walk distinct key tuples in code-point order and materialize the merge."""
    key_col_list = ", ".join(_quote_ident(name) for name in key_fields)
    total = auto_rows = conflict_rows = 0
    field_conflicts = row_conflicts = 0
    row_batch: list[tuple[Any, ...]] = []
    val_batch: list[tuple[Any, ...]] = []
    conflict_batch: list[tuple[Any, ...]] = []
    conflict_ordinal = 0
    # The DISTINCT/ORDER BY sort runs inside SQLite with disk temp storage;
    # Python holds only the current row.
    order_cur = conn.execute(
        "SELECT DISTINCT %s FROM keys ORDER BY %s"
        % (key_col_list, key_col_list))
    for order_row in order_cur:
        key_values = tuple(order_row[idx] for idx in range(len(key_fields)))
        side_ids = _side_key_ids(conn, key_fields, key_values)
        maps: SideMaps = {side: (_field_map(conn, kid) if kid is not None else None)
                          for side, kid in side_ids.items()}
        plan = plan_key(key_values, maps)
        canonical_id = min(kid for kid in side_ids.values()
                           if kid is not None)
        total += 1
        if plan["conflicts"]:
            conflict_rows += 1
            status = "conflict"
        else:
            auto_rows += 1
            status = "auto"
        row_batch.append((canonical_id, *key_values, status, plan["outcome"],
                          len(plan["conflicts"])))
        for field, (val_type, val_text, source) in plan["merged_fields"].items():
            field_id = _field_id(conn, field)
            val_batch.append((canonical_id, field_id, val_type, val_text, source))
        for conflict in plan["conflicts"]:
            conflict_ordinal += 1
            if conflict["category"] == FIELD_CONFLICT:
                field_conflicts += 1
                field_id = _field_id(conn, conflict["field"])
            else:
                row_conflicts += 1
                field_id = None
            conflict_batch.append((conflict["id"], conflict_ordinal,
                                   conflict["category"], canonical_id, field_id))
        if len(row_batch) >= batch_size:
            _flush_merge(conn, row_batch, val_batch, conflict_batch, key_fields)
    _flush_merge(conn, row_batch, val_batch, conflict_batch, key_fields)
    return {
        "total_keys": total,
        "auto_rows": auto_rows,
        "conflict_rows": conflict_rows,
        "field_conflicts": field_conflicts,
        "row_conflicts": row_conflicts,
        "conflicts_total": conflict_ordinal,
    }


def _flush_merge(conn: sqlite3.Connection, row_batch: list, val_batch: list,
                 conflict_batch: list, key_fields: Sequence[str]) -> None:
    if not (row_batch or val_batch or conflict_batch):
        return
    key_cols = ", ".join(_quote_ident(name) for name in key_fields)
    conn.execute("BEGIN")
    try:
        if row_batch:
            conn.executemany(
                "INSERT INTO merged_rows(key_id, %s, status, outcome, "
                "conflict_count) VALUES (%s)"
                % (key_cols, ", ".join("?" * (len(key_fields) + 4))), row_batch)
        if val_batch:
            conn.executemany(
                "INSERT INTO merged_values(key_id, field_id, val_type, "
                "val_text, auto_source) VALUES (?, ?, ?, ?, ?)", val_batch)
        if conflict_batch:
            conn.executemany(
                "INSERT INTO conflicts(conflict_id, ordinal, category, key_id, "
                "field_id) VALUES (?, ?, ?, ?, ?)", conflict_batch)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    row_batch.clear()
    val_batch.clear()
    conflict_batch.clear()


def _side_key_ids(conn: sqlite3.Connection, key_fields: Sequence[str],
                  key_values: Sequence[str]) -> dict[str, int | None]:
    where = " AND ".join("%s = ?" % _quote_ident(name) for name in key_fields)
    out: dict[str, int | None] = {}
    for side in SIDE_TAGS:
        row = conn.execute(
            "SELECT key_id FROM keys WHERE side=? AND %s" % where,
            (side, *key_values)).fetchone()
        out[side] = None if row is None else int(row["key_id"])
    return out


def _field_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT field_id FROM fields WHERE name=?",
                       (name,)).fetchone()
    if row is None:
        raise DictMergeError("internal: unknown field %r" % name)
    return int(row["field_id"])


# ---------------------------------------------------------------------------
# Review API (read-only, paginated in the merged key order)
# ---------------------------------------------------------------------------

class WorkLibrary:
    """Context-managed handle over a completed work library."""

    def __init__(self, path: str | os.PathLike[str], *, writable: bool = False,
                 cache_kib: int = DEFAULT_CACHE_KIB) -> None:
        self.path = Path(path)
        self.writable = writable
        self._conn = _connect(self.path, writable=writable, cache_kib=cache_kib)
        try:
            status = self._conn.execute(
                "SELECT value FROM meta WHERE key='status'").fetchone()
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            raise DictMergeError("%s is not a valid work library: %s"
                                 % (self.path, exc)) from exc
        if status is None or status["value"] != "complete":
            self._conn.close()
            raise DictMergeError("%s is not a complete work library" % self.path)
        self.key_fields = _key_fields_of(self._conn)

    def __enter__(self) -> "WorkLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _key_values(self, row: sqlite3.Row) -> list[str]:
        # Access by quoted column name: key names may contain spaces or dots,
        # and positional indexing would collide with merged_rows.key_id.
        return [row[_quote_ident(name).strip('"')] for name in self.key_fields]

    def _key_where(self) -> str:
        return " AND ".join("%s = ?" % _quote_ident(n) for n in self.key_fields)

    def _side_key_id(self, side: str, key_values: Sequence[str]) -> int | None:
        row = self._conn.execute(
            "SELECT key_id FROM keys WHERE side=? AND %s" % self._key_where(),
            (side, *key_values)).fetchone()
        return None if row is None else int(row["key_id"])

    def _merged_row(self, key_id: int) -> dict[str, Any]:
        mr = self._conn.execute(
            "SELECT * FROM merged_rows WHERE key_id=?", (key_id,)).fetchone()
        fields = {}
        for r in self._conn.execute(
                "SELECT f.name AS name, m.val_type AS val_type, "
                "m.val_text AS val_text, m.auto_source AS auto_source "
                "FROM merged_values m JOIN fields f ON f.field_id=m.field_id "
                "WHERE m.key_id=? ORDER BY f.name", (key_id,)):
            fields[r["name"]] = {
                "state": canonical_state(r["val_type"], r["val_text"]),
                "value": decode_value(r["val_type"], r["val_text"]),
                "auto_source": r["auto_source"],
            }
        return {
            "key": self._key_values(mr),
            "status": mr["status"],
            "outcome": mr["outcome"],
            "conflict_count": mr["conflict_count"],
            "fields": fields,
        }

    def info(self) -> dict[str, Any]:
        return _info_conn(self._conn, self.path, self.key_fields)

    # -- paging -------------------------------------------------------------

    def list_auto(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if limit < 1 or offset < 0:
            raise DictMergeError("limit must be >= 1 and offset >= 0")
        total = int(self._conn.execute(
            "SELECT COUNT(*) AS c FROM merged_rows WHERE status='auto'")
            .fetchone()["c"])
        key_cols = ", ".join(_quote_ident(n) for n in self.key_fields)
        rows = self._conn.execute(
            "SELECT key_id FROM merged_rows WHERE status='auto' "
            "ORDER BY %s LIMIT ? OFFSET ?" % key_cols,
            (limit, offset)).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "rows": [self._merged_row(r["key_id"]) for r in rows]}

    def list_conflicts(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if limit < 1 or offset < 0:
            raise DictMergeError("limit must be >= 1 and offset >= 0")
        total = int(self._conn.execute(
            "SELECT COUNT(*) AS c FROM conflicts").fetchone()["c"])
        open_count = int(self._conn.execute(
            "SELECT COUNT(*) AS c FROM conflicts c LEFT JOIN decisions d "
            "ON d.conflict_id=c.conflict_id WHERE d.conflict_id IS NULL")
            .fetchone()["c"])
        rows = self._conn.execute(
            "SELECT conflict_id FROM conflicts ORDER BY ordinal "
            "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"total": total, "open": open_count, "limit": limit,
                "offset": offset,
                "conflicts": [self.conflict(r["conflict_id"]) for r in rows]}

    def conflict(self, conflict_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT c.*, f.name AS field_name FROM conflicts c "
            "LEFT JOIN fields f ON f.field_id=c.field_id "
            "WHERE c.conflict_id=?", (conflict_id,)).fetchone()
        if row is None:
            raise DictMergeError("unknown conflict id %r" % conflict_id)
        mr = self._conn.execute(
            "SELECT * FROM merged_rows WHERE key_id=?",
            (row["key_id"],)).fetchone()
        key = self._key_values(mr)
        return {
            "conflict_id": row["conflict_id"],
            "ordinal": row["ordinal"],
            "category": row["category"],
            "key": key,
            "field": row["field_name"],
            "states": self._states_for_key(key),
            "suggestion": self._merged_row(row["key_id"]),
        }

    def _states_for_key(self, key_values: Sequence[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for side in SIDE_TAGS:
            side_key_id = self._side_key_id(side, key_values)
            if side_key_id is None:
                out[side] = {"present": False, "line_no": None, "fields": None}
                continue
            line_row = self._conn.execute(
                "SELECT line_no FROM records WHERE side=? AND key_id=?",
                (side, side_key_id)).fetchone()
            fields = {}
            for vr in self._conn.execute(
                    "SELECT f.name AS name, v.val_type AS val_type, "
                    "v.val_text AS val_text FROM values_eav v "
                    "JOIN fields f ON f.field_id=v.field_id "
                    "WHERE v.side=? AND v.key_id=? ORDER BY f.name",
                    (side, side_key_id)):
                fields[vr["name"]] = canonical_state(vr["val_type"],
                                                     vr["val_text"])
            out[side] = {"present": True,
                         "line_no": int(line_row["line_no"]),
                         "fields": fields}
        return out

    def key_detail(self, key_values: Sequence[str]) -> dict[str, Any]:
        if len(key_values) != len(self.key_fields):
            raise DictMergeError("expected %d key components, got %d"
                                 % (len(self.key_fields), len(key_values)))
        row = self._conn.execute(
            "SELECT key_id FROM merged_rows WHERE %s" % self._key_where(),
            tuple(key_values)).fetchone()
        if row is None:
            raise DictMergeError("unknown key %r" % (list(key_values),))
        detail = self._merged_row(int(row["key_id"]))
        detail["conflicts"] = [dict(r) for r in self._conn.execute(
            "SELECT conflict_id, category FROM conflicts WHERE key_id=? "
            "ORDER BY ordinal", (row["key_id"],)).fetchall()]
        detail["states"] = self._states_for_key(key_values)
        return detail

    def snapshot(self, side: str) -> dict[str, str]:
        if side not in SIDE_TAGS:
            raise DictMergeError("side must be one of %s" % (SIDE_TAGS,))
        meta = self._conn.execute(
            "SELECT file_name, sha256, byte_length FROM input_snapshot "
            "WHERE side=?", (side,)).fetchone()
        if meta is None:
            raise DictMergeError("internal: missing snapshot for side %s" % side)
        chunks = "".join(r["content"] for r in self._conn.execute(
            "SELECT content FROM input_chunk WHERE side=? ORDER BY seq",
            (side,)))
        return {"file_name": meta["file_name"], "sha256": meta["sha256"],
                "byte_length": int(meta["byte_length"]), "content": chunks}


def info(work_path: str | os.PathLike[str]) -> dict[str, Any]:
    conn = _open_work(work_path)
    try:
        return _info_conn(conn, Path(work_path), _key_fields_of(conn))
    finally:
        conn.close()


def _info_conn(conn: sqlite3.Connection, path: Path,
               key_fields: Sequence[str]) -> dict[str, Any]:
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
    counts = {
        "total_keys": int(meta.get("total_keys", "0")),
        "auto_rows": int(meta.get("auto_rows", "0")),
        "conflict_rows": int(meta.get("conflict_rows", "0")),
        "conflicts_total": int(meta.get("conflicts_total", "0")),
        "field_conflicts": int(meta.get("field_conflicts", "0")),
        "row_conflicts": int(meta.get("row_conflicts", "0")),
        "open_conflicts": int(conn.execute(
            "SELECT COUNT(*) AS c FROM conflicts c LEFT JOIN decisions d "
            "ON d.conflict_id=c.conflict_id WHERE d.conflict_id IS NULL")
            .fetchone()["c"]),
        "decided_conflicts": int(conn.execute(
            "SELECT COUNT(*) AS c FROM decisions").fetchone()["c"]),
    }
    return {
        "work_library": str(path),
        "schema_version": meta.get("schema_version"),
        "status": meta.get("status"),
        "created_at": meta.get("created_at"),
        "key_fields": list(key_fields),
        "inputs": {side: {"file": meta.get("%s_file" % side),
                          "sha256": meta.get("%s_sha256" % side),
                          "lines": int(meta.get("line_count_%s" % side, "0"))}
                   for side in SIDE_TAGS},
        "batch_size": int(meta.get("batch_size", str(DEFAULT_BATCH_SIZE))),
        "cache_kib": int(meta.get("cache_kib", str(DEFAULT_CACHE_KIB))),
        "counts": counts,
        "finalized": meta.get("finalized_at") is not None,
    }


def review_conflicts(work_path: str | os.PathLike[str], *, limit: int = 50,
                     offset: int = 0) -> dict[str, Any]:
    with WorkLibrary(work_path) as work:
        return work.list_conflicts(limit=limit, offset=offset)


def review_auto(work_path: str | os.PathLike[str], *, limit: int = 50,
                offset: int = 0) -> dict[str, Any]:
    with WorkLibrary(work_path) as work:
        return work.list_auto(limit=limit, offset=offset)


def review_conflict(work_path: str | os.PathLike[str],
                    conflict_id: str) -> dict[str, Any]:
    with WorkLibrary(work_path) as work:
        return work.conflict(conflict_id)


def review_key(work_path: str | os.PathLike[str],
               key_values: Sequence[str]) -> dict[str, Any]:
    with WorkLibrary(work_path) as work:
        return work.key_detail(key_values)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _custom_field_state(decision: dict[str, Any]) -> tuple[str, str | None]:
    custom = decision.get("custom")
    if type(custom) is not dict:
        raise DictMergeError("decision %s: choice 'custom' needs a 'custom' "
                             "object" % decision.get("conflict_id"))
    if "missing" in custom and "value" in custom:
        raise DictMergeError("decision %s: custom cannot give both 'missing' "
                             "and 'value'" % decision["conflict_id"])
    if custom.get("missing") is True:
        return MISSING, None
    if "value" not in custom:
        raise DictMergeError("decision %s: custom needs 'value' or "
                             "{'missing': true}" % decision["conflict_id"])
    return encode_value(custom["value"])


def _load_decisions(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        raise DictMergeError("decision file not found: %s" % path)
    for line_no, obj in iter_jsonl(path, "decisions"):
        cid = obj.get("conflict_id")
        if type(cid) is not str or cid == "":
            raise DictMergeError("decisions line %d: missing string "
                                 "'conflict_id'" % line_no)
        fp = obj.get("state_fingerprint")
        if type(fp) is not str or fp == "":
            raise DictMergeError("decision %s: missing string "
                                 "'state_fingerprint'" % cid)
        choice = obj.get("choice")
        if type(choice) is not str or choice == "":
            raise DictMergeError("decision %s: missing string 'choice'" % cid)
        yield line_no, obj


def _validate_custom_row(decision: dict[str, Any], key_values: Sequence[str],
                         key_fields: Sequence[str]) -> dict[str, Any]:
    custom = decision.get("custom")
    if type(custom) is not dict or "row" not in custom:
        raise DictMergeError("whole-row custom choice needs custom.row "
                             "(a complete object)")
    row = custom["row"]
    if type(row) is not dict:
        raise DictMergeError("custom.row must be an object")
    for idx, name in enumerate(key_fields):
        if name not in row:
            raise DictMergeError("custom.row must keep key field %r" % name)
        if row[name] != key_values[idx]:
            raise DictMergeError("custom.row must keep the same primary key %r"
                                 % (list(key_values),))
    for field, value in row.items():
        if field not in key_fields:
            scalar_type(value)
    return row


# ---------------------------------------------------------------------------
# Finalize (operation 2)
# ---------------------------------------------------------------------------

def finalize(work_path: str | os.PathLike[str],
             decisions_path: str | os.PathLike[str],
             out_jsonl: str | os.PathLike[str],
             report_path: str | os.PathLike[str] | None = None,
             *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """Apply decisions and write the final dictionary.

    All decision validation runs before any output byte is produced.  The
    output goes to a temp file and is hard-linked into place only after the
    whole stream succeeds, so bad decisions or a write failure leave an
    existing output file's bytes unchanged and the process exits non-zero.
    """
    work_path, decisions_path, out_jsonl = (
        Path(work_path), Path(decisions_path), Path(out_jsonl))
    report = Path(report_path) if report_path is not None else None
    if out_jsonl.exists():
        raise DictMergeError("output %s already exists; refusing to overwrite "
                             "it" % out_jsonl)
    if report is not None and report.exists():
        raise DictMergeError("report %s already exists; refusing to overwrite "
                             "it" % report)
    conn = _connect(work_path, writable=True)
    try:
        already = conn.execute(
            "SELECT value FROM meta WHERE key='finalized_at'").fetchone()
        if already is not None:
            raise DictMergeError("work library %s was already finalized; make "
                                 "a fresh compare instead of re-finalizing"
                                 % work_path)
        key_fields = _key_fields_of(conn)
        conn.execute(
            "CREATE TEMP TABLE decision_stage ("
            "line_no INTEGER NOT NULL, conflict_id TEXT NOT NULL, "
            "choice TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "custom_json TEXT)")
        seen: set[str] = set()
        stage_batch: list[tuple[Any, ...]] = []
        decision_raw: dict[str, dict[str, Any]] = {}
        for line_no, decision in _load_decisions(decisions_path):
            cid = decision["conflict_id"]
            if cid in seen:
                raise DictMergeError("decision %s: duplicate conflict id in "
                                     "decision file" % cid)
            seen.add(cid)
            stage_batch.append((
                line_no, cid, decision["choice"],
                decision["state_fingerprint"],
                json.dumps(decision["custom"], ensure_ascii=False, sort_keys=True)
                if decision.get("custom") is not None else None))
            decision_raw[cid] = decision
            if len(stage_batch) >= batch_size:
                conn.executemany(
                    "INSERT INTO decision_stage VALUES (?, ?, ?, ?, ?)",
                    stage_batch)
                stage_batch.clear()
        if stage_batch:
            conn.executemany("INSERT INTO decision_stage VALUES (?, ?, ?, ?, ?)",
                             stage_batch)

        errors = _validate_decisions(conn, key_fields, decision_raw)
        unknown = conn.execute(
            "SELECT s.conflict_id AS cid FROM decision_stage s "
            "LEFT JOIN conflicts c ON c.conflict_id=s.conflict_id "
            "WHERE c.conflict_id IS NULL LIMIT 10").fetchall()
        if unknown:
            errors.append("unknown conflict id(s): %s"
                          % ", ".join(r["cid"] for r in unknown))
        missing_row = conn.execute(
            "SELECT COUNT(*) AS c FROM conflicts c "
            "LEFT JOIN decision_stage s ON s.conflict_id=c.conflict_id "
            "WHERE s.conflict_id IS NULL").fetchone()
        if missing_row["c"]:
            first = [r["conflict_id"] for r in conn.execute(
                "SELECT c.conflict_id FROM conflicts c "
                "LEFT JOIN decision_stage s ON s.conflict_id=c.conflict_id "
                "WHERE s.conflict_id IS NULL ORDER BY c.ordinal LIMIT 10")]
            errors.append("missing decisions for %d conflict(s), first: %s"
                          % (missing_row["c"], ", ".join(first)))
        if errors:
            raise DictMergeError("decision file rejected: " + "; ".join(errors))

        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO decisions(conflict_id, ordinal, choice, "
                "custom_json, fingerprint) "
                "SELECT s.conflict_id, c.ordinal, s.choice, s.custom_json, "
                "s.fingerprint FROM decision_stage s "
                "JOIN conflicts c ON c.conflict_id=s.conflict_id")
            rows_out, report_entries = _write_outputs(
                conn, key_fields, out_jsonl, report)
            import datetime as dt
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("finalized_at",
                          dt.datetime.now(dt.timezone.utc).isoformat(
                              timespec="seconds")))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("final_output", str(out_jsonl)))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except BaseException:
        conn.close()
        for p in (out_jsonl, report):
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise
    conn.close()
    payload = info(work_path)
    payload["output_rows"] = rows_out
    payload["report_entries"] = report_entries
    return payload


def _validate_decisions(conn: sqlite3.Connection, key_fields: Sequence[str],
                        decision_raw: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    rows = conn.execute(
        "SELECT s.*, c.category AS category, c.field_id AS field_id, "
        "m.key_id AS key_id FROM decision_stage s "
        "JOIN conflicts c ON c.conflict_id=s.conflict_id "
        "JOIN merged_rows m ON m.key_id=c.key_id ORDER BY s.line_no").fetchall()
    for r in rows:
        cid = r["conflict_id"]
        decision = decision_raw[cid]
        if r["fingerprint"] != cid:
            errors.append("decision %s: stale state fingerprint; the conflict "
                          "states changed after this decision was written" % cid)
            continue
        if r["category"] == FIELD_CONFLICT:
            err = _check_field_choice(decision, r["choice"])
        else:
            err = _check_row_choice(conn, r, decision, r["choice"], key_fields)
        if err:
            errors.append("decision %s: %s" % (cid, err))
    return errors


def _check_field_choice(decision: dict[str, Any], choice: str) -> str | None:
    if choice in SIDE_TAGS:
        return None  # all three rows exist for a field conflict
    if choice == "custom":
        try:
            _custom_field_state(decision)
        except DictMergeError as exc:
            return str(exc)
        return None
    return "field conflict choice must be base/left/right/custom, got %r" % choice


def _check_row_choice(conn: sqlite3.Connection, conflict: sqlite3.Row,
                      decision: dict[str, Any], choice: str,
                      key_fields: Sequence[str]) -> str | None:
    mr = conn.execute("SELECT * FROM merged_rows WHERE key_id=?",
                      (conflict["key_id"],)).fetchone()
    key_values = tuple(mr[_quote_ident(n).strip('"')] for n in key_fields)
    if choice == "delete":
        return None
    if choice in SIDE_TAGS:
        where = " AND ".join("%s = ?" % _quote_ident(n) for n in key_fields)
        side_row = conn.execute(
            "SELECT key_id FROM keys WHERE side=? AND %s" % where,
            (choice, *key_values)).fetchone()
        if side_row is None:
            return "cannot choose side %r: that side has no row for this key" \
                % choice
        return None
    if choice == "custom":
        try:
            _validate_custom_row(decision, key_values, key_fields)
        except DictMergeError as exc:
            return str(exc)
        return None
    return "whole-row conflict choice must be base/left/right/delete/custom, " \
        "got %r" % choice


# ---------------------------------------------------------------------------
# Output materialization (streamed, code-point ordered)
# ---------------------------------------------------------------------------

def _side_field_state(conn: sqlite3.Connection, key_fields: Sequence[str],
                      key_values: Sequence[str], side: str,
                      field: str) -> tuple[str, str | None]:
    where = " AND ".join("%s = ?" % _quote_ident(n) for n in key_fields)
    side_row = conn.execute(
        "SELECT key_id FROM keys WHERE side=? AND %s" % where,
        (side, *key_values)).fetchone()
    if side_row is None:
        return MISSING, None
    vr = conn.execute(
        "SELECT v.val_type AS val_type, v.val_text AS val_text "
        "FROM values_eav v JOIN fields f ON f.field_id=v.field_id "
        "WHERE v.side=? AND v.key_id=? AND f.name=?",
        (side, int(side_row["key_id"]), field)).fetchone()
    if vr is None:
        return MISSING, None
    return vr["val_type"], vr["val_text"]


def _effective_rows(conn: sqlite3.Connection, key_fields: Sequence[str]
                    ) -> Iterator[tuple[tuple[str, ...], FieldMap, dict[str, Any]]]:
    """Yield (key, final field map, provenance) in output order."""
    key_col_list = ", ".join(_quote_ident(n) for n in key_fields)
    where = " AND ".join("%s = ?" % _quote_ident(n) for n in key_fields)
    cur = conn.execute(
        "SELECT * FROM merged_rows ORDER BY %s" % key_col_list)
    for mr in cur:
        key_values = tuple(mr[_quote_ident(n).strip('"')]
                           for n in key_fields)
        row_decision = conn.execute(
            "SELECT d.choice AS choice, d.custom_json AS custom_json, "
            "c.category AS category FROM conflicts c "
            "JOIN decisions d ON d.conflict_id=c.conflict_id "
            "WHERE c.key_id=? AND c.field_id IS NULL",
            (mr["key_id"],)).fetchone()
        provenance: dict[str, Any] = {"key": list(key_values), "decisions": []}
        if row_decision is not None:
            provenance["decisions"].append({
                "category": row_decision["category"],
                "choice": row_decision["choice"]})
            if row_decision["choice"] == "delete":
                provenance["outcome"] = OUT_DELETE
                yield key_values, {}, provenance
                continue
            if row_decision["choice"] == "custom":
                custom = json.loads(row_decision["custom_json"])["row"]
                field_map = {f: encode_value(v) for f, v in custom.items()
                             if f not in key_fields}
                provenance["outcome"] = OUT_KEEP
                yield key_values, field_map, provenance
                continue
            side = row_decision["choice"]
            side_row = conn.execute(
                "SELECT key_id FROM keys WHERE side=? AND %s" % where,
                (side, *key_values)).fetchone()
            provenance["outcome"] = OUT_KEEP
            yield key_values, _field_map(conn, int(side_row["key_id"])), \
                provenance
            continue

        field_map: FieldMap = {}
        auto_fields: list[str] = []
        for r in conn.execute(
                "SELECT f.name AS name, m.val_type AS val_type, "
                "m.val_text AS val_text, m.auto_source AS auto_source "
                "FROM merged_values m JOIN fields f ON f.field_id=m.field_id "
                "WHERE m.key_id=? ORDER BY f.name", (mr["key_id"],)):
            field_map[r["name"]] = (r["val_type"], r["val_text"])
            if r["auto_source"] != "base":
                auto_fields.append(r["name"])
        for r in conn.execute(
                "SELECT f.name AS name, d.choice AS choice, "
                "d.custom_json AS custom_json FROM conflicts c "
                "JOIN decisions d ON d.conflict_id=c.conflict_id "
                "JOIN fields f ON f.field_id=c.field_id WHERE c.key_id=?",
                (mr["key_id"],)):
            provenance["decisions"].append({"category": FIELD_CONFLICT,
                                            "field": r["name"],
                                            "choice": r["choice"]})
            if r["choice"] == "custom":
                custom = json.loads(r["custom_json"])
                if custom.get("missing") is True:
                    field_map.pop(r["name"], None)
                else:
                    field_map[r["name"]] = encode_value(custom["value"])
            else:
                state = _side_field_state(conn, key_fields, key_values,
                                          r["choice"], r["name"])
                if state[0] == MISSING:
                    field_map.pop(r["name"], None)
                else:
                    field_map[r["name"]] = state
        if mr["outcome"] == OUT_DELETE:
            provenance["outcome"] = OUT_DELETE
            yield key_values, {}, provenance
            continue
        provenance["outcome"] = OUT_KEEP
        provenance["auto_fields"] = auto_fields
        yield key_values, field_map, provenance


def _write_outputs(conn: sqlite3.Connection, key_fields: Sequence[str],
                   out_path: Path, report_path: Path | None
                   ) -> tuple[int, int]:
    tmp_out = out_path.with_name(out_path.name + _TMP_SUFFIX + str(os.getpid()))
    tmp_report = (report_path.with_name(
        report_path.name + _TMP_SUFFIX + str(os.getpid()))
        if report_path is not None else None)
    rows_out = 0
    report_entries = 0
    report_handle = None
    try:
        with open(tmp_out, "w", encoding="utf-8", newline="\n") as handle:
            if tmp_report is not None:
                report_handle = open(tmp_report, "w", encoding="utf-8",
                                     newline="\n")
            for key_values, field_map, provenance in _effective_rows(
                    conn, key_fields):
                if provenance["outcome"] == OUT_DELETE:
                    if report_handle is not None:
                        report_handle.write(json.dumps(
                            {"key": provenance["key"], "outcome": "deleted"},
                            ensure_ascii=False) + "\n")
                        report_entries += 1
                    continue
                obj = {name: key_values[i]
                       for i, name in enumerate(key_fields)}
                for name in sorted(field_map):
                    val_type, val_text = field_map[name]
                    obj[name] = decode_value(val_type, val_text)
                handle.write(json.dumps(obj, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
                rows_out += 1
                if report_handle is not None and _is_interesting(provenance):
                    report_handle.write(json.dumps(provenance,
                                                   ensure_ascii=False) + "\n")
                    report_entries += 1
    except BaseException:
        if report_handle is not None:
            report_handle.close()
        for p in (tmp_out, tmp_report):
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise
    else:
        if report_handle is not None:
            report_handle.close()
    try:
        os.link(tmp_out, out_path)
    except FileExistsError as exc:
        tmp_out.unlink()
        raise DictMergeError("output %s already exists; refusing to overwrite "
                             "it" % out_path) from exc
    tmp_out.unlink()
    if tmp_report is not None:
        try:
            os.link(tmp_report, report_path)
        except FileExistsError as exc:
            tmp_report.unlink()
            raise DictMergeError("report %s already exists; refusing to "
                                 "overwrite it" % report_path) from exc
        tmp_report.unlink()
    return rows_out, report_entries


def _is_interesting(provenance: dict[str, Any]) -> bool:
    return bool(provenance.get("decisions")) or bool(
        provenance.get("auto_fields"))
