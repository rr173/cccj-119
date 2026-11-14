"""Weighted two-dimension hierarchical rollup into a self-contained SQLite result.

The operator hands over a JSON config (two named weighted hierarchies plus a
set of aggregation grains) and a JSONL fact file (one operating fact per
physical line).  The build streams the facts once and writes a result
database holding every grain's occupied cells, five exact rational metrics
per cell, and the full drill-down ledger (one row per contributing
(cell, source line) pair).  Query entries need only the result database:
the validated config snapshot and the parsed facts are stored inside it.

What this tool deliberately does not do: it never derives facts from trips,
GPS, anonymized paths or CAN sessions, it applies no incremental revisions,
and it is not a checkpoint/resume service.  One build = one input pair =
one new result file; an existing result is never overwritten.

Coordinates
-----------
Each cell coordinate carries an explicit ``kind`` tag:

* ``member`` -- a real member id of the grain's selected level (``"007"``
  and ``"7"`` are different members; ``"ALL"`` is a perfectly legal member
  id and stays a plain ``member``);
* ``null``   -- the source fact had JSON null for that dimension
  (unclassified; it is never folded into the root and never means
  "all members");
* ``all``    -- the grain dropped that dimension (the ``ALL`` selector).

Metrics
-------
Per cell, all exact rationals (``fractions.Fraction``), serialized as
``{"num": <int text>, "den": <positive int text>}`` with zero fixed at 0/1:

* ``row_weight``    -- sum of every contribution coefficient;
* ``value_weight``  -- sum of coefficients whose fact has a non-null amount;
* ``amount_sum``    -- sum of amount * coefficient (null when value_weight
  is 0; an exact zero from cancelling amounts stays 0/1, never null);
* ``amount_mean``   -- amount_sum / value_weight (null together with
  amount_sum);
* ``vehicle_count`` -- exact distinct count of non-null vehicle_unit strings
  with (always positive) coefficient in this cell; coefficients are never
  applied to vehicle counts, and a vehicle seen through many rows, paths or
  sibling cells still counts once here.

Memory contract
---------------
Python never holds all facts, all contribution rows or any vehicle set.
Facts stream through a bounded batch (``batch_size``); per-batch partial
cell sums and staged contribution rows flush to SQLite per batch.  Vehicle
dedup is a single ``COUNT(DISTINCT ...) GROUP BY`` executed inside SQLite
(temp b-trees, ``temp_store=FILE``).  Cell finalization streams one cell at a
time over an ``ORDER BY`` cursor.  Per-dimension coefficient maps are
computed once per (dimension, level) and reused by every grain and fact;
the full Cartesian product of members is never enumerated -- only
combinations that actually occur are written.  See docs/ROLLUP.md for the
on-disk structures and the complexity notes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import GtfsError

SCHEMA_VERSION = "1"

#: Grain level selector meaning "drop this dimension".  Reserved as a level
#: name; member ids may still be literally "ALL" (they live in a different
#: namespace and keep kind="member").
ALL = "ALL"

KIND_MEMBER = "member"
KIND_NULL = "null"
KIND_ALL = "all"
_KINDS = (KIND_MEMBER, KIND_NULL, KIND_ALL)

#: Build-time tunables (both exposed on the CLI).
DEFAULT_BATCH_SIZE = 4096      # facts per transaction / accumulator flush
DEFAULT_CACHE_KIB = 16384      # SQLite page cache for the build connection

_TMP_SUFFIX = ".tmp-"


class RollupError(GtfsError):
    """A rollup config, fact or build problem.  Builds exit non-zero."""

    exit_code = 1


# ---------------------------------------------------------------------------
# JSON loading with duplicate-member detection
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


def _loads_strict(text: str, what: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_strict_object)
    except _DupKey as exc:
        raise RollupError("%s: duplicate JSON member %r" % (what, exc.key)) from exc
    except json.JSONDecodeError as exc:
        raise RollupError("%s: invalid JSON: %s" % (what, exc)) from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RollupError(message)


def _is_int(value: Any) -> bool:
    """JSON integer check that refuses bool (a subclass of int)."""
    return type(value) is int


def _is_nonempty_str(value: Any) -> bool:
    return type(value) is str and value != ""


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class Dimension:
    """One validated weighted hierarchy, levels ordered leaf -> root."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.levels: list[str] = []                 # leaf .. root
        self.members: dict[str, list[str]] = {}     # level -> ordered ids
        self.member_set: dict[str, set[str]] = {}   # level -> id set
        # lower level -> {child id: [(parent id, Fraction)]}
        self.up: dict[str, dict[str, list[tuple[str, Fraction]]]] = {}

    @property
    def leaf_level(self) -> str:
        return self.levels[0]

    @property
    def root_level(self) -> str:
        return self.levels[-1]

    def has_level(self, level: str) -> bool:
        return level in self.members


class Grain:
    def __init__(self, name: str, levels: dict[str, str]) -> None:
        self.name = name
        self.levels = dict(levels)  # dimension name -> level name or ALL


class Config:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.dim_order: list[str] = []
        self.dims: dict[str, Dimension] = {}
        self.dim_fields: dict[str, str] = {}   # dimension name -> JSON field
        self.vehicle_field = ""
        self.amount_field = ""
        self.grains: list[Grain] = []
        # memoized (dimension, level) -> {leaf id: {target id: Fraction}}
        self._maps: dict[tuple[str, str], dict[str, dict[str, Fraction]]] = {}

    # -- coefficient maps -------------------------------------------------

    def level_map(self, dim_name: str, level: str) -> dict[str, dict[str, Fraction]]:
        """leaf id -> {target member id at ``level``: exact coefficient}.

        Computed once per (dimension, level) by folding edge fractions level
        by level; every grain and every fact reuses the same map, so raw
        root paths are enumerated at most once per level, never per fact.
        """
        key = (dim_name, level)
        cached = self._maps.get(key)
        if cached is not None:
            return cached
        dim = self.dims[dim_name]
        _require(dim.has_level(level), "internal: unknown level %s.%s" % (dim_name, level))
        result = _level_map_full(dim, level)
        self._maps[key] = result
        return result


def _level_map_full(dim: Dimension, level: str) -> dict[str, dict[str, Fraction]]:
    """Direct per-leaf fold: leaf -> {member at ``level``: coefficient}."""
    # member at current level -> coefficient, per leaf; folded upward.
    out: dict[str, dict[str, Fraction]] = {}
    cur_level = dim.leaf_level
    acc: dict[str, dict[str, Fraction]] = {
        leaf: {leaf: Fraction(1)} for leaf in dim.members[cur_level]
    }
    if cur_level == level:
        return {leaf: dict(parts) for leaf, parts in acc.items()}
    for next_level in dim.levels[1:]:
        nxt: dict[str, dict[str, Fraction]] = {
            leaf: {} for leaf in dim.members[dim.leaf_level]
        }
        for child, parents in dim.up[cur_level].items():
            for leaf, leaf_parts in acc.items():
                coeff = leaf_parts.get(child)
                if coeff is None:
                    continue
                target = nxt[leaf]
                for parent, edge_frac in parents:
                    merged = coeff * edge_frac
                    if parent in target:
                        target[parent] += merged
                    else:
                        target[parent] = merged
        acc = nxt
        cur_level = next_level
        if cur_level == level:
            break
    return acc


def _validate_dimension(name: str, spec: Any) -> Dimension:
    _require(_is_nonempty_str(name), "dimension name must be a non-empty string")
    _require(type(spec) is dict, "dimension %r: spec must be an object" % name)
    dim = Dimension(name)

    levels = spec.get("levels")
    _require(type(levels) is list, "dimension %r: 'levels' must be an array" % name)
    _require(len(levels) >= 2,
             "dimension %r: need at least two levels (leaf and root)" % name)
    seen_levels: set[str] = set()
    for entry in levels:
        _require(type(entry) is dict, "dimension %r: level entry must be an object" % name)
        lvl_name = entry.get("name")
        _require(_is_nonempty_str(lvl_name),
                 "dimension %r: level name must be a non-empty string" % name)
        _require(lvl_name != ALL,
                 "dimension %r: level name %r is reserved for the ALL selector"
                 % (name, ALL))
        _require(lvl_name not in seen_levels,
                 "dimension %r: duplicate level name %r" % (name, lvl_name))
        seen_levels.add(lvl_name)
        members = entry.get("members")
        _require(type(members) is list and len(members) > 0,
                 "dimension %r level %r: members must be a non-empty array"
                 % (name, lvl_name))
        seen_members: set[str] = set()
        for member in members:
            _require(_is_nonempty_str(member),
                     "dimension %r level %r: member ids must be non-empty strings"
                     % (name, lvl_name))
            _require(member not in seen_members,
                     "dimension %r level %r: duplicate member id %r"
                     % (name, lvl_name, member))
            seen_members.add(member)
        dim.levels.append(lvl_name)
        dim.members[lvl_name] = list(members)
        dim.member_set[lvl_name] = seen_members

    root_members = dim.members[dim.root_level]
    _require(len(root_members) == 1,
             "dimension %r: top level %r must hold exactly one root member, found %d"
             % (name, dim.root_level, len(root_members)))

    links = spec.get("links")
    _require(type(links) is list, "dimension %r: 'links' must be an array" % name)
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for link in links:
        _require(type(link) is dict, "dimension %r: link entry must be an object" % name)
        from_level = link.get("from")
        to_level = link.get("to")
        _require(_is_nonempty_str(from_level) and _is_nonempty_str(to_level),
                 "dimension %r: link 'from'/'to' must be non-empty level names" % name)
        _require(dim.has_level(from_level),
                 "dimension %r: link from unknown level %r" % (name, from_level))
        _require(dim.has_level(to_level),
                 "dimension %r: link to unknown level %r" % (name, to_level))
        from_idx = dim.levels.index(from_level)
        _require(from_idx + 1 < len(dim.levels) and dim.levels[from_idx + 1] == to_level,
                 "dimension %r: link %r -> %r is not between adjacent levels"
                 % (name, from_level, to_level))
        pair = (from_level, to_level)
        _require(pair not in by_pair,
                 "dimension %r: duplicate link block for %r -> %r"
                 % (name, from_level, to_level))
        edges = link.get("edges")
        _require(type(edges) is list,
                 "dimension %r link %r -> %r: 'edges' must be an array"
                 % (name, from_level, to_level))
        by_pair[pair] = edges

    for idx in range(len(dim.levels) - 1):
        pair = (dim.levels[idx], dim.levels[idx + 1])
        _require(pair in by_pair,
                 "dimension %r: missing link block for adjacent levels %r -> %r"
                 % (name, pair[0], pair[1]))

    for (from_level, to_level), edges in by_pair.items():
        child_set = dim.member_set[from_level]
        parent_set = dim.member_set[to_level]
        per_child: dict[str, list[tuple[str, Fraction]]] = {}
        seen_edges: set[tuple[str, str]] = set()
        for edge in edges:
            _require(type(edge) is dict,
                     "dimension %r link %r -> %r: edge must be an object"
                     % (name, from_level, to_level))
            child = edge.get("child")
            parent = edge.get("parent")
            num = edge.get("numerator")
            den = edge.get("denominator")
            _require(type(child) is str and child in child_set,
                     "dimension %r link %r -> %r: unknown child member %r"
                     % (name, from_level, to_level, child))
            _require(type(parent) is str and parent in parent_set,
                     "dimension %r link %r -> %r: unknown parent member %r"
                     % (name, from_level, to_level, parent))
            _require(_is_int(num) and num >= 1,
                     "dimension %r link %r -> %r edge %r -> %r: numerator must be a "
                     "positive integer (no bool/float/zero)"
                     % (name, from_level, to_level, child, parent))
            _require(_is_int(den) and den >= 1,
                     "dimension %r link %r -> %r edge %r -> %r: denominator must be a "
                     "positive integer (no bool/float/zero)"
                     % (name, from_level, to_level, child, parent))
            _require((child, parent) not in seen_edges,
                     "dimension %r link %r -> %r: duplicate edge %r -> %r"
                     % (name, from_level, to_level, child, parent))
            seen_edges.add((child, parent))
            per_child.setdefault(child, []).append((parent, Fraction(num, den)))
        for member in dim.members[from_level]:
            edges_out = per_child.get(member)
            _require(edges_out is not None and len(edges_out) > 0,
                     "dimension %r: member %r of level %r has no parent; every "
                     "non-root member must reach the root" % (name, member, from_level))
            total = sum((frac for _, frac in edges_out), Fraction(0))
            _require(total == 1,
                     "dimension %r: member %r of level %r: outgoing proportions sum "
                     "to %s, must be exactly 1" % (name, member, from_level, total))
        dim.up[from_level] = per_child
    return dim


def validate_config(raw_text: str) -> Config:
    """Parse and validate one config document; raises RollupError."""
    raw = _loads_strict(raw_text, "config")
    _require(type(raw) is dict, "config: top level must be an object")
    config = Config(raw_text)

    dims = raw.get("dimensions")
    _require(type(dims) is dict, "config: 'dimensions' must be an object")
    _require(len(dims) == 2,
             "config: exactly two named dimensions are required, found %d" % len(dims))
    for dim_name, spec in dims.items():
        dim = _validate_dimension(dim_name, spec)
        config.dim_order.append(dim_name)
        config.dims[dim_name] = dim

    fields = raw.get("fields")
    _require(type(fields) is dict, "config: 'fields' must be an object")
    dim_fields = fields.get("dimensions")
    _require(type(dim_fields) is dict,
             "config: 'fields.dimensions' must be an object")
    _require(set(dim_fields.keys()) == set(config.dim_order),
             "config: 'fields.dimensions' must name exactly the two dimensions %s"
             % sorted(config.dim_order))
    for dim_name, field in dim_fields.items():
        _require(_is_nonempty_str(field),
                 "config: field name for dimension %r must be a non-empty string"
                 % dim_name)
        config.dim_fields[dim_name] = field
    vehicle_field = fields.get("vehicle")
    amount_field = fields.get("amount")
    _require(_is_nonempty_str(vehicle_field),
             "config: 'fields.vehicle' must be a non-empty string")
    _require(_is_nonempty_str(amount_field),
             "config: 'fields.amount' must be a non-empty string")
    all_fields = list(dim_fields.values()) + [vehicle_field, amount_field]
    _require(len(set(all_fields)) == 4,
             "config: the two dimension fields, vehicle and amount field names "
             "must all be different, got %s" % all_fields)
    config.vehicle_field = vehicle_field
    config.amount_field = amount_field

    grains = raw.get("grains")
    _require(type(grains) is list and len(grains) > 0,
             "config: 'grains' must be a non-empty array")
    seen_names: set[str] = set()
    seen_combos: set[tuple[str, str]] = set()
    for entry in grains:
        _require(type(entry) is dict, "config: grain entry must be an object")
        grain_name = entry.get("name")
        _require(_is_nonempty_str(grain_name),
                 "config: grain name must be a non-empty string")
        _require(grain_name not in seen_names,
                 "config: duplicate grain name %r" % grain_name)
        seen_names.add(grain_name)
        levels = entry.get("levels")
        _require(type(levels) is dict,
                 "config: grain %r: 'levels' must be an object" % grain_name)
        _require(set(levels.keys()) == set(config.dim_order),
                 "config: grain %r: 'levels' must select exactly the dimensions %s"
                 % (grain_name, sorted(config.dim_order)))
        combo: list[str] = []
        for dim_name in config.dim_order:
            sel = levels[dim_name]
            _require(_is_nonempty_str(sel),
                     "config: grain %r: level selector for dimension %r must be a "
                     "non-empty string" % (grain_name, dim_name))
            if sel != ALL:
                _require(config.dims[dim_name].has_level(sel),
                         "config: grain %r: dimension %r has no level %r"
                         % (grain_name, dim_name, sel))
            combo.append(sel)
        combo_key = tuple(combo)
        _require(combo_key not in seen_combos,
                 "config: duplicate grain level combination %s (grain %r); "
                 "dimension order does not make a new grain"
                 % (dict(zip(config.dim_order, combo)), grain_name))
        seen_combos.add(combo_key)
        config.grains.append(Grain(grain_name, dict(levels)))
    return config


# ---------------------------------------------------------------------------
# Fact parsing
# ---------------------------------------------------------------------------

class FactFields:
    """Resolved per-line field accessors for one config."""

    def __init__(self, config: Config) -> None:
        self.dim1_name, self.dim2_name = config.dim_order
        self.dim1_field = config.dim_fields[self.dim1_name]
        self.dim2_field = config.dim_fields[self.dim2_name]
        self.vehicle_field = config.vehicle_field
        self.amount_field = config.amount_field
        self.leaf1 = config.dims[self.dim1_name].member_set[
            config.dims[self.dim1_name].leaf_level]
        self.leaf2 = config.dims[self.dim2_name].member_set[
            config.dims[self.dim2_name].leaf_level]


def _fact_error(line_no: int, field: str | None, message: str) -> RollupError:
    where = "facts line %d" % line_no
    if field is not None:
        where += " field %r" % field
    return RollupError("%s: %s" % (where, message))


def parse_fact(obj: Any, line_no: int, fields: FactFields) -> tuple[
        str | None, str | None, str | None, int | None]:
    """Validate one decoded fact object; returns (dim1, dim2, vehicle, amount)."""
    if type(obj) is not dict:
        raise _fact_error(line_no, None, "each line must be a JSON object")
    for field in (fields.dim1_field, fields.dim2_field,
                  fields.vehicle_field, fields.amount_field):
        if field not in obj:
            raise _fact_error(line_no, field, "required field is missing")
    dim1 = obj[fields.dim1_field]
    dim2 = obj[fields.dim2_field]
    vehicle = obj[fields.vehicle_field]
    amount = obj[fields.amount_field]

    for value, field, leaf_set in (
            (dim1, fields.dim1_field, fields.leaf1),
            (dim2, fields.dim2_field, fields.leaf2)):
        if value is None:
            continue
        if type(value) is not str:
            raise _fact_error(line_no, field,
                              "dimension value must be a leaf member string or null")
        if value not in leaf_set:
            raise _fact_error(line_no, field,
                              "unknown leaf member %r (ids compare literally; "
                              "empty string is not a wildcard)" % value)
    if vehicle is not None and type(vehicle) is not str:
        raise _fact_error(line_no, fields.vehicle_field,
                          "vehicle_unit must be a string or null")
    if amount is not None and not _is_int(amount):
        raise _fact_error(line_no, fields.amount_field,
                          "amount must be an integer of any size or null "
                          "(no bool, no decimals)")
    return dim1, dim2, vehicle, amount


def iter_fact_lines(facts_path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield (1-based physical line number, raw line bytes) once per line."""
    with open(facts_path, "rb") as handle:
        for line_no, raw in enumerate(handle, start=1):
            yield line_no, raw


def parse_fact_line(raw: bytes, line_no: int, fields: FactFields) -> tuple[
        str | None, str | None, str | None, int | None]:
    stripped = raw.strip()
    if not stripped:
        raise _fact_error(line_no, None, "empty line is not a fact")
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fact_error(line_no, None, "line is not valid UTF-8: %s" % exc) from exc
    try:
        obj = json.loads(text, object_pairs_hook=_strict_object)
    except _DupKey as exc:
        raise _fact_error(line_no, exc.key, "duplicate JSON member") from exc
    except json.JSONDecodeError as exc:
        raise _fact_error(line_no, None, "invalid JSON: %s" % exc) from exc
    return parse_fact(obj, line_no, fields)


# ---------------------------------------------------------------------------
# Result database schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The exact config document this result was built from (read-back).
CREATE TABLE config_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL
);

-- Every parsed fact, keyed by its 1-based physical line number.
CREATE TABLE facts (
    line_no INTEGER PRIMARY KEY,
    dim1_value TEXT,
    dim2_value TEXT,
    vehicle_unit TEXT,
    amount TEXT
);

CREATE TABLE grains (
    grain_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    dim1_level TEXT,   -- NULL means the ALL selector (dimension dropped)
    dim2_level TEXT
);

-- Occupied cells only; never the full Cartesian product of members.
CREATE TABLE cells (
    cell_id INTEGER PRIMARY KEY,
    grain_id INTEGER NOT NULL REFERENCES grains,
    d1_kind TEXT NOT NULL CHECK (d1_kind IN ('member', 'null', 'all')),
    d1_value TEXT,
    d2_kind TEXT NOT NULL CHECK (d2_kind IN ('member', 'null', 'all')),
    d2_value TEXT,
    row_w_num TEXT NOT NULL,
    row_w_den TEXT NOT NULL,
    val_w_num TEXT NOT NULL,
    val_w_den TEXT NOT NULL,
    sum_num TEXT,             -- NULL when no non-null amount reached the cell
    sum_den TEXT,
    vehicle_count INTEGER NOT NULL
);
-- Member ids are non-empty, so '' is a safe NULL stand-in for uniqueness.
CREATE UNIQUE INDEX cells_key ON cells
    (grain_id, d1_kind, ifnull(d1_value, ''), d2_kind, ifnull(d2_value, ''));

-- Drill-down ledger: one row per (cell, source line); coefficients merged.
CREATE TABLE contrib (
    cell_id INTEGER NOT NULL REFERENCES cells,
    line_no INTEGER NOT NULL REFERENCES facts,
    coeff_num TEXT NOT NULL,
    coeff_den TEXT NOT NULL,
    PRIMARY KEY (cell_id, line_no)
);

-- Build-time staging (dropped before publish).
CREATE TABLE cell_part (
    grain_id INTEGER NOT NULL,
    d1_kind TEXT NOT NULL,
    d1_value TEXT,
    d2_kind TEXT NOT NULL,
    d2_value TEXT,
    row_w_num TEXT NOT NULL,
    row_w_den TEXT NOT NULL,
    val_w_num TEXT NOT NULL,
    val_w_den TEXT NOT NULL,
    sum_num TEXT,
    sum_den TEXT
);
CREATE TABLE contrib_stage (
    grain_id INTEGER NOT NULL,
    d1_kind TEXT NOT NULL,
    d1_value TEXT,
    d2_kind TEXT NOT NULL,
    d2_value TEXT,
    line_no INTEGER NOT NULL,
    coeff_num TEXT NOT NULL,
    coeff_den TEXT NOT NULL
);
"""


def _frac_text(value: Fraction) -> tuple[str, str]:
    return str(value.numerator), str(value.denominator)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

class _CellAccum:
    """Per-batch partial sums for one cell (flushed to cell_part)."""

    __slots__ = ("row_w", "val_w", "amount_sum", "has_amount")

    def __init__(self) -> None:
        self.row_w = Fraction(0)
        self.val_w = Fraction(0)
        self.amount_sum = Fraction(0)
        self.has_amount = False

    def add(self, coeff: Fraction, amount: int | None) -> None:
        self.row_w += coeff
        if amount is not None:
            self.has_amount = True
            self.val_w += coeff
            self.amount_sum += amount * coeff


def _grain_targets(config: Config, fields: FactFields, grain: Grain,
                   dim1_value: str | None, dim2_value: str | None
                   ) -> Iterable[tuple[tuple[str, str | None], tuple[str, str | None],
                                       Fraction]]:
    """Yield ((kind1, value1), (kind2, value2), coefficient) for one fact."""
    per_dim: list[list[tuple[str, str | None, Fraction]]] = []
    for dim_name, dim_value in ((fields.dim1_name, dim1_value),
                                (fields.dim2_name, dim2_value)):
        selector = grain.levels[dim_name]
        if selector == ALL:
            per_dim.append([(KIND_ALL, None, Fraction(1))])
        elif dim_value is None:
            per_dim.append([(KIND_NULL, None, Fraction(1))])
        else:
            mapping = config.level_map(dim_name, selector)[dim_value]
            per_dim.append([(KIND_MEMBER, target, coeff)
                            for target, coeff in mapping.items()])
    for kind1, value1, coeff1 in per_dim[0]:
        for kind2, value2, coeff2 in per_dim[1]:
            yield (kind1, value1), (kind2, value2), coeff1 * coeff2


def build(config_path: str | os.PathLike[str], facts_path: str | os.PathLike[str],
          out_path: str | os.PathLike[str], *, batch_size: int = DEFAULT_BATCH_SIZE,
          cache_kib: int = DEFAULT_CACHE_KIB) -> dict[str, Any]:
    """Build one result database; refuses to overwrite; cleans up on failure."""
    config_path = Path(config_path)
    facts_path = Path(facts_path)
    out_path = Path(out_path)
    _require(batch_size >= 1, "batch_size must be >= 1")
    _require(cache_kib >= 128, "cache_kib must be >= 128")
    if out_path.exists():
        raise RollupError("result database %s already exists; refusing to "
                          "overwrite it" % out_path)
    if not config_path.is_file():
        raise RollupError("config file not found: %s" % config_path)
    if not facts_path.is_file():
        raise RollupError("facts file not found: %s" % facts_path)

    try:
        raw_config = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RollupError("config file is not valid UTF-8: %s" % exc) from exc
    except OSError as exc:
        raise RollupError("cannot read config file %s: %s" % (config_path, exc)) from exc
    config = validate_config(raw_config)
    fields = FactFields(config)
    config_sha = hashlib.sha256(raw_config.encode("utf-8")).hexdigest()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Best-effort removal of stale temp files from crashed earlier builds.
    for stale in out_path.parent.glob(out_path.name + _TMP_SUFFIX + "*"):
        try:
            stale.unlink()
        except OSError:
            pass
    tmp_path = out_path.with_name(out_path.name + _TMP_SUFFIX + str(os.getpid()))

    conn = sqlite3.connect(str(tmp_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=OFF")       # temp file: discarded on failure
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=FILE")        # big sorts stay out of RAM
        conn.execute("PRAGMA cache_size=-%d" % cache_kib)
        conn.executescript(_DDL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta = {
            "schema_version": SCHEMA_VERSION,
            "status": "building",
            "created_at": now,
            "config_sha256": config_sha,
            "config_file": config_path.name,
            "facts_file": facts_path.name,
            "batch_size": str(batch_size),
            "cache_kib": str(cache_kib),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                         sorted(meta.items()))
        conn.execute("INSERT INTO config_snapshot(id, config_json) VALUES (1, ?)",
                     (raw_config,))
        grain_ids: dict[str, int] = {}
        for grain in config.grains:
            cur = conn.execute(
                "INSERT INTO grains(name, dim1_level, dim2_level) VALUES (?, ?, ?)",
                (grain.name,
                 None if grain.levels[fields.dim1_name] == ALL
                 else grain.levels[fields.dim1_name],
                 None if grain.levels[fields.dim2_name] == ALL
                 else grain.levels[fields.dim2_name]))
            grain_ids[grain.name] = int(cur.lastrowid)

        fact_count = 0
        contrib_count = 0
        facts_batch: list[tuple[Any, ...]] = []
        stage_batch: list[tuple[Any, ...]] = []
        cell_acc: dict[tuple[Any, ...], _CellAccum] = {}

        def flush() -> None:
            nonlocal contrib_count
            if not facts_batch and not stage_batch and not cell_acc:
                return
            conn.execute("BEGIN")
            try:
                if facts_batch:
                    conn.executemany(
                        "INSERT INTO facts(line_no, dim1_value, dim2_value, "
                        "vehicle_unit, amount) VALUES (?, ?, ?, ?, ?)", facts_batch)
                if stage_batch:
                    conn.executemany(
                        "INSERT INTO contrib_stage(grain_id, d1_kind, d1_value, "
                        "d2_kind, d2_value, line_no, coeff_num, coeff_den) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", stage_batch)
                    contrib_count += len(stage_batch)
                if cell_acc:
                    part_rows = []
                    for (grain_id, k1, v1, k2, v2), acc in cell_acc.items():
                        row_num, row_den = _frac_text(acc.row_w)
                        val_num, val_den = _frac_text(acc.val_w)
                        if acc.has_amount:
                            sum_num, sum_den = _frac_text(acc.amount_sum)
                        else:
                            sum_num = sum_den = None
                        part_rows.append((grain_id, k1, v1, k2, v2,
                                          row_num, row_den, val_num, val_den,
                                          sum_num, sum_den))
                    conn.executemany(
                        "INSERT INTO cell_part(grain_id, d1_kind, d1_value, d2_kind, "
                        "d2_value, row_w_num, row_w_den, val_w_num, val_w_den, "
                        "sum_num, sum_den) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        part_rows)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            facts_batch.clear()
            stage_batch.clear()
            cell_acc.clear()

        facts_sha = hashlib.sha256()
        for line_no, raw in iter_fact_lines(facts_path):
            facts_sha.update(raw)
            dim1_value, dim2_value, vehicle, amount = parse_fact_line(
                raw, line_no, fields)
            facts_batch.append((line_no, dim1_value, dim2_value, vehicle,
                                None if amount is None else str(amount)))
            for grain in config.grains:
                grain_id = grain_ids[grain.name]
                for (kind1, value1), (kind2, value2), coeff in _grain_targets(
                        config, fields, grain, dim1_value, dim2_value):
                    key = (grain_id, kind1, value1, kind2, value2)
                    acc = cell_acc.get(key)
                    if acc is None:
                        acc = cell_acc[key] = _CellAccum()
                    acc.add(coeff, amount)
                    num, den = _frac_text(coeff)
                    stage_batch.append((grain_id, kind1, value1, kind2, value2,
                                        line_no, num, den))
            fact_count += 1
            if fact_count % batch_size == 0:
                flush()
        flush()

        # --- finalize cells: one streaming pass over sorted partials -------
        conn.execute("BEGIN")
        try:
            cell_rows = 0
            current_key: tuple[Any, ...] | None = None
            row_w = val_w = amount_sum = Fraction(0)
            has_amount = False

            def emit_cell() -> None:
                nonlocal cell_rows
                grain_id, k1, v1, k2, v2 = current_key
                row_num, row_den = _frac_text(row_w)
                val_num, val_den = _frac_text(val_w)
                if has_amount:
                    sum_num, sum_den = _frac_text(amount_sum)
                else:
                    sum_num = sum_den = None
                conn.execute(
                    "INSERT INTO cells(grain_id, d1_kind, d1_value, d2_kind, "
                    "d2_value, row_w_num, row_w_den, val_w_num, val_w_den, "
                    "sum_num, sum_den, vehicle_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (grain_id, k1, v1, k2, v2, row_num, row_den,
                     val_num, val_den, sum_num, sum_den))
                cell_rows += 1

            cursor = conn.execute(
                "SELECT grain_id, d1_kind, d1_value, d2_kind, d2_value, "
                "row_w_num, row_w_den, val_w_num, val_w_den, sum_num, sum_den "
                "FROM cell_part ORDER BY grain_id, d1_kind, d1_value, d2_kind, "
                "d2_value")
            for row in cursor:
                key = (row["grain_id"], row["d1_kind"], row["d1_value"],
                       row["d2_kind"], row["d2_value"])
                if key != current_key:
                    if current_key is not None:
                        emit_cell()
                    current_key = key
                    row_w = val_w = amount_sum = Fraction(0)
                    has_amount = False
                row_w += Fraction(int(row["row_w_num"]), int(row["row_w_den"]))
                val_w += Fraction(int(row["val_w_num"]), int(row["val_w_den"]))
                if row["sum_num"] is not None:
                    has_amount = True
                    amount_sum += Fraction(int(row["sum_num"]), int(row["sum_den"]))
            if current_key is not None:
                emit_cell()

            # --- vehicle dedup inside SQLite (temp b-trees, not Python) ----
            for row in conn.execute(
                    "SELECT s.grain_id AS grain_id, s.d1_kind AS d1_kind, "
                    "s.d1_value AS d1_value, s.d2_kind AS d2_kind, "
                    "s.d2_value AS d2_value, "
                    "COUNT(DISTINCT f.vehicle_unit) AS vehicles "
                    "FROM contrib_stage s JOIN facts f ON f.line_no = s.line_no "
                    "WHERE f.vehicle_unit IS NOT NULL "
                    "GROUP BY s.grain_id, s.d1_kind, s.d1_value, s.d2_kind, "
                    "s.d2_value"):
                conn.execute(
                    "UPDATE cells SET vehicle_count = ? "
                    "WHERE grain_id = ? AND d1_kind = ? AND d1_value IS ? "
                    "AND d2_kind = ? AND d2_value IS ?",
                    (row["vehicles"], row["grain_id"], row["d1_kind"],
                     row["d1_value"], row["d2_kind"], row["d2_value"]))

            # --- final drill-down ledger -----------------------------------
            conn.execute(
                "CREATE INDEX stage_key ON contrib_stage"
                "(grain_id, d1_kind, d1_value, d2_kind, d2_value)")
            conn.execute(
                "INSERT INTO contrib(cell_id, line_no, coeff_num, coeff_den) "
                "SELECT c.cell_id, s.line_no, s.coeff_num, s.coeff_den "
                "FROM contrib_stage s JOIN cells c "
                "ON c.grain_id = s.grain_id AND c.d1_kind = s.d1_kind "
                "AND c.d1_value IS s.d1_value AND c.d2_kind = s.d2_kind "
                "AND c.d2_value IS s.d2_value")
            staged = conn.execute("SELECT COUNT(*) FROM contrib_stage").fetchone()[0]
            moved = conn.execute("SELECT COUNT(*) FROM contrib").fetchone()[0]
            _require(staged == moved,
                     "internal: staged %d contributions but joined %d"
                     % (staged, moved))
            conn.execute("DROP TABLE contrib_stage")
            conn.execute("DROP TABLE cell_part")
            final_meta = {
                "status": "complete",
                "fact_rows": str(fact_count),
                "cell_rows": str(cell_rows),
                "contribution_rows": str(contrib_count),
                "grain_count": str(len(config.grains)),
                "facts_sha256": facts_sha.hexdigest(),
            }
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                sorted(final_meta.items()))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.close()
    except BaseException:
        try:
            conn.close()
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    # Atomic, no-overwrite publish: hard-link the finished temp file into
    # place; the link itself fails if a result appeared meanwhile.  On
    # filesystems without hard links, fall back to a checked rename.
    try:
        os.link(tmp_path, out_path)
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        raise RollupError("result database %s already exists; refusing to "
                          "overwrite it" % out_path)
    except OSError:
        if out_path.exists():
            tmp_path.unlink(missing_ok=True)
            raise RollupError("result database %s already exists; refusing to "
                              "overwrite it" % out_path)
        os.replace(tmp_path, out_path)
    else:
        tmp_path.unlink()
    return {
        "result": str(out_path),
        "status": "complete",
        "facts": fact_count,
        "grains": len(config.grains),
        "cells": cell_rows,
        "contributions": contrib_count,
        "config_sha256": config_sha,
        "facts_sha256": facts_sha.hexdigest(),
        "batch_size": batch_size,
        "cache_kib": cache_kib,
    }


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

def _frac_json(num: str, den: str) -> dict[str, str]:
    return {"num": num, "den": den}


def _frac_from(row: sqlite3.Row, num_col: str, den_col: str) -> Fraction:
    return Fraction(int(row[num_col]), int(row[den_col]))


class Coord:
    """One explicit cell coordinate: kind in {member, null, all}."""

    def __init__(self, kind: str, value: str | None = None) -> None:
        if kind not in _KINDS:
            raise RollupError("coordinate kind must be one of %s" % (list(_KINDS),))
        if kind == KIND_MEMBER:
            if not _is_nonempty_str(value):
                raise RollupError("member coordinate needs a non-empty member id")
        elif value is not None:
            raise RollupError("%s coordinate takes no member id" % kind)
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Coord(%s, %r)" % (self.kind, self.value)


class RollupResult:
    """Read-only access to one completed result database."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise RollupError("result database not found: %s" % self.path)
        uri = "file:%s?mode=ro" % self.path
        self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            meta = {row["key"]: row["value"]
                    for row in self.conn.execute("SELECT key, value FROM meta")}
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise RollupError("%s is not a rollup result database: %s"
                              % (self.path, exc)) from exc
        if meta.get("schema_version") != SCHEMA_VERSION:
            self.conn.close()
            raise RollupError("result %s uses schema version %r, this code "
                              "expects %r" % (self.path,
                                              meta.get("schema_version"),
                                              SCHEMA_VERSION))
        if meta.get("status") != "complete":
            self.conn.close()
            raise RollupError("result %s is incomplete (status=%r); a failed "
                              "build is never queryable"
                              % (self.path, meta.get("status")))
        self.meta = meta
        self.config = validate_config(
            self.conn.execute("SELECT config_json FROM config_snapshot WHERE id = 1")
            .fetchone()[0])
        self.dim1_name, self.dim2_name = self.config.dim_order
        self._grains = {row["name"]: dict(row) for row in self.conn.execute(
            "SELECT grain_id, name, dim1_level, dim2_level FROM grains "
            "ORDER BY grain_id")}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RollupResult":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- metadata ---------------------------------------------------------

    def info(self) -> dict[str, Any]:
        return {
            "result": str(self.path),
            "status": self.meta["status"],
            "created_at": self.meta["created_at"],
            "config_file": self.meta["config_file"],
            "config_sha256": self.meta["config_sha256"],
            "facts_file": self.meta["facts_file"],
            "facts_sha256": self.meta["facts_sha256"],
            "fact_rows": int(self.meta["fact_rows"]),
            "cell_rows": int(self.meta["cell_rows"]),
            "contribution_rows": int(self.meta["contribution_rows"]),
            "grain_count": int(self.meta["grain_count"]),
            "batch_size": int(self.meta["batch_size"]),
            "cache_kib": int(self.meta["cache_kib"]),
            "dimensions": self.config.dim_order,
            "grains": self.grains(),
        }

    def grains(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self._grains, key=lambda n: self._grains[n]["grain_id"]):
            row = self._grains[name]
            out.append({
                "name": name,
                "levels": {
                    self.dim1_name: row["dim1_level"] if row["dim1_level"] is not None else ALL,
                    self.dim2_name: row["dim2_level"] if row["dim2_level"] is not None else ALL,
                },
                "cells": self.conn.execute(
                    "SELECT COUNT(*) FROM cells WHERE grain_id = ?",
                    (row["grain_id"],)).fetchone()[0],
            })
        return out

    def fact(self, line_no: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT line_no, dim1_value, dim2_value, vehicle_unit, amount "
            "FROM facts WHERE line_no = ?", (line_no,)).fetchone()
        if row is None:
            raise RollupError("no fact stored for line %d" % line_no)
        return self._fact_json(row)

    def _fact_json(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "line_no": row["line_no"],
            "dimensions": {
                self.dim1_name: row["dim1_value"],
                self.dim2_name: row["dim2_value"],
            },
            "vehicle_unit": row["vehicle_unit"],
            "amount": None if row["amount"] is None else int(row["amount"]),
        }

    # -- cells ------------------------------------------------------------

    def _grain_row(self, grain: str) -> dict[str, Any]:
        row = self._grains.get(grain)
        if row is None:
            raise RollupError("unknown grain %r (have: %s)"
                              % (grain, sorted(self._grains)))
        return row

    def _coord_json(self, dim_name: str, level: str | None,
                    kind: str, value: str | None) -> dict[str, Any]:
        if kind == KIND_ALL:
            return {"kind": KIND_ALL}
        coord: dict[str, Any] = {"kind": kind, "level": level}
        if kind == KIND_MEMBER:
            coord["value"] = value
        return coord

    def _metrics_json(self, row: sqlite3.Row) -> dict[str, Any]:
        row_w = _frac_from(row, "row_w_num", "row_w_den")
        val_w = _frac_from(row, "val_w_num", "val_w_den")
        if row["sum_num"] is None:
            amount_sum = None
            amount_mean = None
        else:
            amount_sum = Fraction(int(row["sum_num"]), int(row["sum_den"]))
            amount_mean = amount_sum / val_w
        return {
            "row_weight": _frac_json(*_frac_text(row_w)),
            "value_weight": _frac_json(*_frac_text(val_w)),
            "amount_sum": None if amount_sum is None else _frac_json(*_frac_text(amount_sum)),
            "amount_mean": None if amount_mean is None else _frac_json(*_frac_text(amount_mean)),
            "vehicle_count": row["vehicle_count"],
        }

    def _cell_json(self, grain_row: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
        return {
            "grain": grain_row["name"],
            "coords": {
                self.dim1_name: self._coord_json(
                    self.dim1_name, grain_row["dim1_level"],
                    row["d1_kind"], row["d1_value"]),
                self.dim2_name: self._coord_json(
                    self.dim2_name, grain_row["dim2_level"],
                    row["d2_kind"], row["d2_value"]),
            },
            "metrics": self._metrics_json(row),
        }

    def cells(self, grain: str, *, d1: Coord | None = None,
              d2: Coord | None = None, limit: int | None = None,
              offset: int = 0) -> list[dict[str, Any]]:
        grain_row = self._grain_row(grain)
        sql = ("SELECT * FROM cells WHERE grain_id = ?")
        params: list[Any] = [grain_row["grain_id"]]
        for prefix, coord in (("d1", d1), ("d2", d2)):
            if coord is None:
                continue
            sql += " AND %s_kind = ? AND %s_value IS ?" % (prefix, prefix)
            params += [coord.kind, coord.value]
        sql += " ORDER BY cell_id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [self._cell_json(grain_row, row)
                for row in self.conn.execute(sql, params)]

    def drill(self, grain: str, d1: Coord, d2: Coord, *,
              offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """One cell's metrics plus a page of its contribution ledger.

        Totals come from the stored cell row and a COUNT over the whole
        ledger -- never from the returned page alone.
        """
        if limit < 1:
            raise RollupError("drill limit must be >= 1")
        if offset < 0:
            raise RollupError("drill offset must be >= 0")
        grain_row = self._grain_row(grain)
        cell = self.conn.execute(
            "SELECT * FROM cells WHERE grain_id = ? AND d1_kind = ? "
            "AND d1_value IS ? AND d2_kind = ? AND d2_value IS ?",
            (grain_row["grain_id"], d1.kind, d1.value,
             d2.kind, d2.value)).fetchone()
        if cell is None:
            raise RollupError("grain %r has no cell at the requested "
                              "coordinates" % grain)
        total = self.conn.execute(
            "SELECT COUNT(*) FROM contrib WHERE cell_id = ?",
            (cell["cell_id"],)).fetchone()[0]
        rows = []
        for row in self.conn.execute(
                "SELECT c.line_no AS line_no, c.coeff_num AS coeff_num, "
                "c.coeff_den AS coeff_den, f.dim1_value AS dim1_value, "
                "f.dim2_value AS dim2_value, f.vehicle_unit AS vehicle_unit, "
                "f.amount AS amount "
                "FROM contrib c JOIN facts f ON f.line_no = c.line_no "
                "WHERE c.cell_id = ? ORDER BY c.line_no LIMIT ? OFFSET ?",
                (cell["cell_id"], limit, offset)):
            rows.append({
                "line_no": row["line_no"],
                "dimensions": {
                    self.dim1_name: row["dim1_value"],
                    self.dim2_name: row["dim2_value"],
                },
                "vehicle_unit": row["vehicle_unit"],
                "amount": None if row["amount"] is None else int(row["amount"]),
                "coefficient": _frac_json(row["coeff_num"], row["coeff_den"]),
            })
        return {
            "grain": grain_row["name"],
            "coords": self._cell_json(grain_row, cell)["coords"],
            "metrics": self._metrics_json(cell),
            "total_contributions": total,
            "offset": offset,
            "limit": limit,
            "rows": rows,
        }
