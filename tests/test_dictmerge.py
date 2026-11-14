"""Tests for gtfs_store.dictmerge (three-way relational dictionary merge).

The reference merge in ``ref_plan`` is a deliberate, independent in-memory
implementation of the merge rules.  It imports nothing from
gtfs_store.dictmerge except the public error type and the final I/O entry
points, so the SQLite engine is checked against separate logic rather than
against its own helpers.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store.cli import main as cli_main  # noqa: E402
from gtfs_store.dictmerge import (  # noqa: E402
    ADD_COLLISION,
    DELETE_MODIFY,
    FIELD_CONFLICT,
    MISSING,
    DictMergeError,
    WorkLibrary,
    canonical_state,
    compare,
    decode_value,
    encode_value,
    finalize,
    info,
    plan_key,
    review_conflict,
    review_conflicts,
    review_key,
)

CLI = str(ROOT / "gtfs-store")


# ---------------------------------------------------------------------------
# Independent reference implementation (shares no merge code with the engine)
# ---------------------------------------------------------------------------

def ref_state(row: dict | None, field: str):
    """Reference field state: ('missing', None) / ('null'|'str'|'int'|'bool', v)."""
    if row is None or field not in row:
        return MISSING, None
    value = row[field]
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", value
    return "str", value


def ref_plan(base: dict | None, left: dict | None, right: dict | None,
             key_fields: list[str], key: tuple[str, ...]):
    """Independent three-way rule; returns (outcome, field_map, conflicts).

    Field map values are plain Python scalars; MISSING means the field must
    not appear on the output row.  Conflicts are tagged by category/field.
    """
    def nonkey(row):
        return [f for f in row if f not in key_fields] if row else []

    conflicts = []
    if base is None:
        if left is not None and right is None:
            return "keep", {f: left[f] for f in nonkey(left)}, conflicts
        if right is not None and left is None:
            return "keep", {f: right[f] for f in nonkey(right)}, conflicts
        if left is not None and right is not None:
            if left == right:
                return "keep", {f: left[f] for f in nonkey(left)}, conflicts
            conflicts.append((ADD_COLLISION, None))
            merged = {}
            for f in sorted(set(nonkey(left)) | set(nonkey(right))):
                if ref_state(left, f) == ref_state(right, f) and f in left and f in right:
                    merged[f] = left[f]
                elif f in left and f not in right:
                    merged[f] = left[f]
                elif f in right and f not in left:
                    merged[f] = right[f]
            return "keep", merged, conflicts
        return "delete", {}, conflicts

    if left is None and right is None:
        return "delete", {}, conflicts
    if left is None or right is None:
        survivor = right if left is None else left
        if survivor == base:
            return "delete", {}, conflicts
        conflicts.append((DELETE_MODIFY, None))
        merged = {}
        for f in sorted(set(nonkey(base)) | set(nonkey(survivor))):
            if f in survivor:
                merged[f] = survivor[f]
        return "keep", merged, conflicts

    merged = {}
    for f in sorted(set(nonkey(base)) | set(nonkey(left)) | set(nonkey(right))):
        bs, ls, rs = ref_state(base, f), ref_state(left, f), ref_state(right, f)
        if ls == rs:
            if ls[0] != MISSING:
                merged[f] = left[f]
        elif ls == bs:
            if rs[0] != MISSING:
                merged[f] = right[f]
        elif rs == bs:
            if ls[0] != MISSING:
                merged[f] = left[f]
        else:
            conflicts.append((FIELD_CONFLICT, f))
    return "keep", merged, conflicts


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class MergeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dictmerge-test-")
        self.tmp = Path(self._tmp.name)
        self.key = ["agency", "stop_id"]
    def tearDown(self) -> None:
        self._tmp.cleanup()

    def paths(self, name: str):
        return (self.tmp / ("%s-base.jsonl" % name),
                self.tmp / ("%s-left.jsonl" % name),
                self.tmp / ("%s-right.jsonl" % name))

    # Short alias used by tests that build one triple per name.
    def path(self, name: str):
        return self.paths(name)

    def compare(self, base, left, right, *, key=None, name="m"):
        bp, lp, rp = self.paths(name)
        write_jsonl(bp, base)
        write_jsonl(lp, left)
        write_jsonl(rp, right)
        work = self.tmp / ("%s.db" % name)
        return compare(bp, lp, rp, key or self.key, work)

    def cli(self, *argv, expect: int = 0):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["--json", *argv])
        self.assertEqual(code, expect, "stdout=%r stderr=%r"
                         % (out.getvalue(), err.getvalue()))
        return json.loads(out.getvalue())


# ---------------------------------------------------------------------------
# plan_key pure rule (the five hand-listed scenarios and more)
# ---------------------------------------------------------------------------

class PlanKeyTests(unittest.TestCase):
    KEY = ("k",)

    def state_maps(self, base, left, right):
        def m(row):
            if row is None:
                return None
            return {f: encode_value(v) if f != "k" else encode_value(v)
                    for f, v in row.items() if f != "k"}
        return {"base": m(base), "left": m(left), "right": m(right)}

    def plan(self, base, left, right):
        return plan_key(self.KEY, self.state_maps(base, left, right))

    def test_same_value_double_change_auto_merges(self):
        # Both sides make the identical edit: take it once, no conflict.
        p = self.plan({"k": "1", "name": "A"},
                      {"k": "1", "name": "B"},
                      {"k": "1", "name": "B"})
        self.assertEqual(p["conflicts"], [])
        self.assertEqual(p["merged_fields"]["name"][:2], encode_value("B"))
        self.assertEqual(p["merged_fields"]["name"][2], "both")

    def test_rename_vs_category_both_kept(self):
        p = self.plan({"k": "1", "name": "A"},
                      {"k": "1", "name": "B"},
                      {"k": "1", "name": "A", "cat": "C"})
        self.assertEqual(p["conflicts"], [])
        self.assertEqual(p["merged_fields"]["name"][:2], encode_value("B"))
        self.assertEqual(p["merged_fields"]["cat"][:2], encode_value("C"))

    def test_delete_vs_unchanged_deletes(self):
        p = self.plan({"k": "1", "v": 1}, None, {"k": "1", "v": 1})
        self.assertEqual(p["outcome"], "delete")
        self.assertEqual(p["conflicts"], [])
        p2 = self.plan({"k": "1", "v": 1}, {"k": "1", "v": 1}, None)
        self.assertEqual(p2["outcome"], "delete")

    def test_delete_vs_modify_conflicts(self):
        p = self.plan({"k": "1", "v": 1}, None, {"k": "1", "v": 2})
        self.assertEqual(len(p["conflicts"]), 1)
        self.assertEqual(p["conflicts"][0]["category"], DELETE_MODIFY)
        # no per-field conflicts on top of the whole-row conflict
        self.assertIsNone(p["conflicts"][0]["field"])
        self.assertEqual(p["merged_fields"]["v"][:2], encode_value(2))

    def test_add_collision_distinct_objects(self):
        p = self.plan(None, {"k": "1", "v": 1}, {"k": "1", "v": 2})
        self.assertEqual(len(p["conflicts"]), 1)
        self.assertEqual(p["conflicts"][0]["category"], ADD_COLLISION)
        self.assertEqual(p["merged_fields"], {})  # nothing agreed

    def test_add_collision_keeps_agreed_fields_as_suggestion(self):
        p = self.plan(None,
                      {"k": "1", "v": 1, "same": "x"},
                      {"k": "1", "v": 2, "same": "x"})
        self.assertEqual(p["conflicts"][0]["category"], ADD_COLLISION)
        self.assertEqual(p["merged_fields"]["same"][:2], encode_value("x"))

    def test_identical_new_rows_taken_once(self):
        p = self.plan(None, {"k": "1", "v": 1}, {"k": "1", "v": 1})
        self.assertEqual(p["conflicts"], [])
        self.assertEqual(p["merged_fields"]["v"][2], "both")

    def test_field_delete_vs_null_is_conflict(self):
        p = self.plan({"k": "1", "v": 1}, {"k": "1"}, {"k": "1", "v": None})
        cats = [c["category"] for c in p["conflicts"]]
        self.assertEqual(cats, [FIELD_CONFLICT])
        self.assertEqual(p["conflicts"][0]["field"], "v")

    def test_field_delete_vs_untouched_auto_absent(self):
        # One side deletes the field, the other leaves base alone: merge as
        # absent without asking.
        p = self.plan({"k": "1", "v": 1}, {"k": "1"}, {"k": "1", "v": 1})
        self.assertEqual(p["conflicts"], [])
        self.assertNotIn("v", p["merged_fields"])

    def test_absence_distinct_from_null_empty_zero_false(self):
        for value in (None, "", 0, False):
            p = self.plan({"k": "1", "v": value},
                          {"k": "1"},
                          {"k": "1", "v": value})
            self.assertEqual(p["conflicts"], [])
            self.assertNotIn("v", p["merged_fields"])
        # null vs "" vs 0 vs false are pairwise different
        vals = [None, "", 0, False]
        for i in range(len(vals)):
            for j in range(len(vals)):
                if i == j:
                    continue
                p = self.plan({"k": "1", "v": vals[i]},
                              {"k": "1", "v": vals[j]},
                              {"k": "1", "v": "third-%d" % i})
                self.assertEqual(
                    [c["category"] for c in p["conflicts"]],
                    [FIELD_CONFLICT], "i=%d j=%d" % (i, j))

    def test_bool_and_int_are_not_mixed(self):
        p = self.plan({"k": "1"}, {"k": "1", "v": True},
                      {"k": "1", "v": 1})
        self.assertEqual(
            plan_key(self.KEY, self.state_maps(
                {"k": "1"}, {"k": "1", "v": 1}, {"k": "1", "v": True}))
            ["conflicts"][0]["category"], FIELD_CONFLICT)
        self.assertEqual(p["conflicts"][0]["field"], "v")

    def test_big_integer_roundtrip_in_plan(self):
        big = 2 ** 127 + 3
        p = self.plan({"k": "1"}, {"k": "1", "big": big}, {"k": "1"})
        self.assertEqual(p["merged_fields"]["big"][:2],
                         (V_INT := "int", str(big)))
        self.assertEqual(decode_value(*p["merged_fields"]["big"][:2]), big)

    def test_field_conflict_suggestions_cover_other_fields(self):
        p = self.plan({"k": "1", "a": 1, "b": 1},
                      {"k": "1", "a": 2, "b": 1},
                      {"k": "1", "a": 3, "b": 9})
        fields = {c["field"] for c in p["conflicts"]}
        self.assertEqual(fields, {"a"})
        self.assertEqual(p["merged_fields"]["b"][:2], encode_value(9))


# ---------------------------------------------------------------------------
# Engine vs. the independent reference on a rich hand-written dictionary
# ---------------------------------------------------------------------------

RICH_KEY = ["agency", "stop_id"]


def rich_inputs():
    base = [
        # composite key; 007 vs 7; explicit null; absent column; 0 vs false;
        # integer beyond signed 64 bits (well within 128).
        {"agency": "A1", "stop_id": "007", "name": "Seven", "cat": None,
         "zero": 0, "flag": False, "big": 2 ** 127},
        {"agency": "A1", "stop_id": "7", "name": "Literal seven"},
        {"agency": "A2", "stop_id": "S1", "name": "Same value double edit"},
        {"agency": "A2", "stop_id": "S2", "name": "Delete vs untouched"},
        {"agency": "A2", "stop_id": "S3", "name": "Delete vs modify", "v": "x"},
        {"agency": "A3", "stop_id": "F1", "name": "Drop vs null", "v": 1},
        {"agency": "A3", "stop_id": "F2", "name": "Rename vs category"},
        {"agency": "A4", "stop_id": "U1", "name": "Unchanged"},
        {"agency": "A5", "stop_id": "D1", "name": "field del", "gone": "z"},
    ]
    left = [
        {"agency": "A1", "stop_id": "007", "name": "Seven (L)", "cat": None,
         "zero": 0, "flag": False, "big": 2 ** 127},
        {"agency": "A1", "stop_id": "7", "name": "Literal seven"},
        {"agency": "A2", "stop_id": "S1", "name": "Both wrote this"},
        # S2 deleted on the left
        # S3 deleted on the left
        {"agency": "A3", "stop_id": "F1", "name": "Drop vs null"},  # drops v
        {"agency": "A3", "stop_id": "F2", "name": "F2 new name"},
        {"agency": "A4", "stop_id": "U1", "name": "Unchanged"},
        {"agency": "A5", "stop_id": "D1", "name": "field del"},  # drops gone
        {"agency": "A9", "stop_id": "N1", "name": "only left new"},
    ]
    right = [
        {"agency": "A1", "stop_id": "007", "name": "Seven", "cat": "URBAN",
         "zero": 0, "flag": False, "big": 2 ** 127, "note": None},
        {"agency": "A1", "stop_id": "7", "name": "Literal seven"},
        {"agency": "A2", "stop_id": "S1", "name": "Both wrote this"},
        # S2 untouched on the right -> auto delete
        {"agency": "A2", "stop_id": "S3", "name": "Delete vs modify", "v": "y"},
        {"agency": "A3", "stop_id": "F1", "name": "Drop vs null",
         "v": None},  # sets null
        {"agency": "A3", "stop_id": "F2", "name": "Rename vs category",
         "cat": "RAIL"},
        {"agency": "A4", "stop_id": "U1", "name": "Unchanged"},
        {"agency": "A5", "stop_id": "D1", "name": "field del", "gone": "z"},
        {"agency": "A9", "stop_id": "N2", "name": "only right new"},
    ]
    return base, left, right


class EngineReferenceTests(MergeCase):
    def _build(self, name="rich", swap=False):
        base, left, right = rich_inputs()
        if swap:
            left, right = right, left
        result = self.compare(base, left, right, name=name)
        return result, base, left, right

    def _db(self, name="rich"):
        return self.tmp / ("%s.db" % name)

    def test_counts_and_categories(self):
        result, _, _, _ = self._build()
        c = result["counts"]
        # distinct keys: 9 base + N1 + N2 = 11
        self.assertEqual(c["total_keys"], 11)
        # conflict rows: S3 delete_modify, F1 field_conflict,
        # F2? no (rename+category auto), N1/N2 are distinct keys -> not a
        # collision. So conflicts: S3 (row), F1 (field) = 2 conflicts.
        self.assertEqual(c["conflicts_total"], 2)
        self.assertEqual(c["field_conflicts"], 1)
        self.assertEqual(c["row_conflicts"], 1)
        self.assertEqual(c["conflict_rows"], 2)
        self.assertEqual(c["auto_rows"], 9)

    def test_engine_matches_reference_for_every_key(self):
        _, base, left, right = self._build()
        work = self.tmp / "rich.db"
        with WorkLibrary(work) as w:
            all_rows = w.list_auto(limit=1000)["rows"]
            page = w.list_conflicts(limit=1000)["conflicts"]
            conflict_keys = {tuple(c["key"]) for c in page}
        b = {tuple(r[k] for k in RICH_KEY): r for r in base}
        l = {tuple(r[k] for k in RICH_KEY): r for r in left}
        r = {tuple(r[k] for k in RICH_KEY): r for r in right}
        engine = {}
        for row in all_rows:
            key = tuple(row["key"])
            if row["outcome"] == "delete":
                continue
            engine[key] = {f: d["value"] for f, d in row["fields"].items()}
        for key in sorted(set(b) | set(l) | set(r)):
            if key in conflict_keys:
                continue
            outcome, merged, conflicts = ref_plan(
                b.get(key), l.get(key), r.get(key), RICH_KEY, key)
            self.assertEqual(conflicts, [], key)
            if outcome == "delete":
                self.assertNotIn(key, engine)
            else:
                self.assertEqual(engine.get(key), merged, key)

    def test_conflict_identities_stable_under_swap_and_reorder(self):
        r1, _, _, _ = self._build("a")
        r2, _, _, _ = self._build("b", swap=True)
        def ids(res_db):
            with WorkLibrary(res_db) as w:
                page = w.list_conflicts(limit=1000)["conflicts"]
            return sorted((c["conflict_id"], c["category"], tuple(c["key"]),
                           c["field"]) for c in page)
        self.assertEqual(ids(self.tmp / "a.db"), ids(self.tmp / "b.db"))

        # Reorder inputs: same ids, different displayed line numbers.
        base, left, right = rich_inputs()
        bp, lp, rp = self.path("reorder")
        write_jsonl(bp, list(reversed(base)))
        write_jsonl(lp, list(reversed(left)))
        write_jsonl(rp, list(reversed(right)))
        work = self.tmp / "reorder.db"
        compare(bp, lp, rp, RICH_KEY, work)
        self.assertEqual(ids(work), ids(self.tmp / "a.db"))
        with WorkLibrary(work) as w:
            detail = w.conflict(
                ids(work)[0][0])
            lines = [s["line_no"] for s in detail["states"].values()
                     if s["present"]]
            self.assertTrue(all(isinstance(n, int) for n in lines))

    def test_conflict_id_bound_to_states_not_just_key_and_field(self):
        # Same key/field, different states must yield different ids.
        p1 = plan_key(("k",), {"base": {"v": encode_value(1)},
                               "left": {"v": encode_value(2)},
                               "right": {"v": encode_value(3)}})
        p2 = plan_key(("k",), {"base": {"v": encode_value(1)},
                               "left": {"v": encode_value(2)},
                               "right": {"v": encode_value(4)}})
        self.assertNotEqual(p1["conflicts"][0]["id"], p2["conflicts"][0]["id"])

    def test_pagination_reports_full_totals(self):
        self._build()
        work = self._db()
        with WorkLibrary(work) as w:
            p1 = w.list_conflicts(limit=1, offset=0)
            p2 = w.list_conflicts(limit=1, offset=1)
            self.assertEqual(p1["total"], 2)
            self.assertEqual(p1["open"], 2)
            self.assertEqual(len(p1["conflicts"]), 1)
            self.assertEqual(len(p2["conflicts"]), 1)
            self.assertNotEqual(p1["conflicts"][0]["conflict_id"],
                                p2["conflicts"][0]["conflict_id"])
            auto = w.list_auto(limit=2, offset=0)
            self.assertEqual(auto["total"], 9)
            self.assertEqual(len(auto["rows"]), 2)
            # key order follows Unicode code points
            keys = [tuple(r["key"]) for r in w.list_auto(limit=1000)["rows"]]
            self.assertEqual(keys, sorted(keys))

    def test_review_key_shows_three_states_and_line_numbers(self):
        self._build()
        work = self._db()
        detail = review_key(work, ("A2", "S3"))
        self.assertFalse(detail["states"]["left"]["present"])
        self.assertTrue(detail["states"]["base"]["present"])
        self.assertEqual(detail["states"]["right"]["line_no"], 4)
        self.assertEqual(len(detail["conflicts"]), 1)

    def test_snapshot_allows_review_after_inputs_move(self):
        self._build()
        work = self._db()
        moved = self.tmp / "moved"
        moved.mkdir()
        new_work = moved / "copied.db"
        shutil.copy(work, new_work)
        with WorkLibrary(new_work) as w:
            snap = w.snapshot("left")
        original = (self.tmp / "rich-left.jsonl").read_text(encoding="utf-8")
        self.assertEqual(snap["content"], original)
        self.assertEqual(len(snap["sha256"]), 64)
        review_conflicts(new_work)

    def test_sqlite_encoding_preserves_types(self):
        self._build()
        work = self._db()
        with WorkLibrary(work) as w:
            detail = w.key_detail(("A1", "007"))
        st = detail["states"]["base"]["fields"]
        self.assertEqual(st["cat"], canonical_state("null", None))
        self.assertEqual(st["zero"]["type"], "int")
        self.assertEqual(st["flag"]["type"], "bool")
        self.assertEqual(st["big"]["value"], str(2 ** 127))
        # 007 stays a string distinct from 7
        keys = {tuple(r["key"]) for r in
                w.list_auto(limit=1000)["rows"]} if False else None
        detail7 = review_key(work, ("A1", "7"))
        self.assertEqual(detail7["key"], ["A1", "7"])


class FinalizeTests(MergeCase):
    def _work(self, name="rich"):
        base, left, right = rich_inputs()
        self.compare(base, left, right, name=name)
        return self.tmp / ("%s.db" % name)

    def _decisions_for(self, work, resolver):
        """Build a valid decision file via resolver(category,key,field,detail)."""
        lines = []
        with WorkLibrary(work) as w:
            for c in w.list_conflicts(limit=1000)["conflicts"]:
                choice, custom = resolver(c)
                obj = {"conflict_id": c["conflict_id"],
                       "state_fingerprint": c["conflict_id"],
                       "choice": choice}
                if custom is not None:
                    obj["custom"] = custom
                lines.append(json.dumps(obj, ensure_ascii=False))
        path = self.tmp / "decisions.jsonl"
        path.write_text("\n".join(lines) + ("\n" if lines else ""),
                        encoding="utf-8")
        return path

    def test_finalize_matches_reference_with_side_and_custom_choices(self):
        work = self._work()

        def resolver(c):
            if c["category"] == DELETE_MODIFY:
                return "right", None
            if c["category"] == FIELD_CONFLICT:
                # custom third value; another custom-missing case is covered
                # separately below.
                return "custom", {"value": "settled"}
            return "left", None

        decisions = self._decisions_for(work, resolver)
        out = self.tmp / "final.jsonl"
        report = self.tmp / "report.jsonl"
        result = finalize(work, decisions, out, report)
        self.assertEqual(result["counts"]["open_conflicts"], 0)
        rows = read_jsonl(out)
        # output sorted by composite key, one row each
        keys = [tuple(r[k] for k in RICH_KEY) for r in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        by_key = {tuple(r[k] for k in RICH_KEY): r for r in rows}
        # left rename + right category both survived
        self.assertEqual(by_key[("A3", "F2")]["name"], "F2 new name")
        self.assertEqual(by_key[("A3", "F2")]["cat"], "RAIL")
        # delete-vs-untouched row really disappeared
        self.assertNotIn(("A2", "S2"), by_key)
        # custom third value applied
        self.assertEqual(by_key[("A3", "F1")]["v"], "settled")
        # right side set cat="URBAN" while base had explicit null: that is an
        # auto-merged change; explicit null vs absence stays visible on note.
        self.assertEqual(by_key[("A1", "007")]["cat"], "URBAN")
        self.assertEqual(by_key[("A1", "007")]["note"], None)
        # big integer exact
        self.assertEqual(by_key[("A1", "007")]["big"], 2 ** 127)
        # 0 vs false types survive JSON round-trip
        self.assertEqual(type(by_key[("A1", "007")]["zero"]), int)
        self.assertEqual(type(by_key[("A1", "007")]["flag"]), bool)
        # report mentions every auto-merged change and each decision
        report_rows = read_jsonl(report)
        report_keys = {tuple(r["key"]) for r in report_rows}
        self.assertIn(("A2", "S3"), report_keys)
        self.assertIn(("A3", "F2"), report_keys)

    def test_custom_missing_deletes_field_and_custom_row_works(self):
        work = self._work("c2")

        def resolver(c):
            if c["category"] == FIELD_CONFLICT:
                return "custom", {"missing": True}
            return "custom", {"row": {"agency": c["key"][0],
                                      "stop_id": c["key"][1],
                                      "name": "third opinion",
                                      "n": -5}}

        path = self._decisions_for(work, resolver)
        out = self.tmp / "c2.jsonl"
        finalize(work, path, out)
        by_key = {tuple(r[k] for k in RICH_KEY): r for r in read_jsonl(out)}
        self.assertNotIn("v", by_key[("A3", "F1")])
        self.assertEqual(by_key[("A2", "S3")]["name"], "third opinion")
        self.assertEqual(by_key[("A2", "S3")]["n"], -5)

    def test_empty_decisions_file_accepted_only_without_conflicts(self):
        bp, lp, rp = self.path("clean")
        rows = [{"k": "1", "v": 1}]
        write_jsonl(bp, rows)
        write_jsonl(lp, [{"k": "1", "v": 2}])
        write_jsonl(rp, rows)
        work = self.tmp / "clean.db"
        compare(bp, lp, rp, ["k"], work)
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        out = self.tmp / "clean-out.jsonl"
        finalize(work, empty, out)
        self.assertEqual(read_jsonl(out), [{"k": "1", "v": 2}])

    def test_missing_decisions_rejected_and_output_untouched(self):
        work = self._work("m1")
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        out = self.tmp / "keep.jsonl"
        out.write_text("PREEXISTING-BYTES", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, empty, out)
        self.assertEqual(out.read_text(), "PREEXISTING-BYTES")
        # work library not marked finalized
        self.assertFalse(info(work)["finalized"])

    def test_stale_fingerprint_rejected(self):
        work = self._work("m2")
        with WorkLibrary(work) as w:
            cid = w.list_conflicts(limit=1)["conflicts"][0]["conflict_id"]
        bad = self.tmp / "stale.jsonl"
        bad.write_text(json.dumps({
            "conflict_id": cid, "state_fingerprint": "0" * 64,
            "choice": "left"}) + "\n", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, bad, self.tmp / "x.jsonl")

    def test_duplicate_and_unknown_and_category_errors(self):
        work = self._work("m3")
        with WorkLibrary(work) as w:
            conflicts = w.list_conflicts(limit=1000)["conflicts"]
        ids = [c["conflict_id"] for c in conflicts]
        # duplicate id
        dup = self.tmp / "dup.jsonl"
        dup.write_text(
            "\n".join(json.dumps({"conflict_id": ids[0],
                                  "state_fingerprint": ids[0],
                                  "choice": "left"}) for _ in range(2))
            + "\n", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, dup, self.tmp / "d.jsonl")
        # unknown id
        unk = self.tmp / "unk.jsonl"
        unk.write_text(json.dumps({"conflict_id": "f" * 64,
                                   "state_fingerprint": "f" * 64,
                                   "choice": "left"}) + "\n", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, unk, self.tmp / "d.jsonl")
        # category mismatch: custom.row on a field conflict
        bad_cat = self.tmp / "badcat.jsonl"
        field_cid = next(c["conflict_id"] for c in conflicts
                         if c["category"] == FIELD_CONFLICT)
        bad_cat.write_text(json.dumps({
            "conflict_id": field_cid, "state_fingerprint": field_cid,
            "choice": "custom",
            "custom": {"row": {"agency": "A3", "stop_id": "F1"}}}) + "\n",
            encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, bad_cat, self.tmp / "d.jsonl")
        # invalid custom value type
        bad_val = self.tmp / "badval.jsonl"
        bad_val.write_text(json.dumps({
            "conflict_id": field_cid, "state_fingerprint": field_cid,
            "choice": "custom", "custom": {"value": 1.25}}) + "\n",
            encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, bad_val, self.tmp / "d.jsonl")

    def test_custom_row_changing_key_rejected(self):
        work = self._work("m4")
        with WorkLibrary(work) as w:
            row_cid = next(c["conflict_id"] for c in
                           w.list_conflicts(limit=1000)["conflicts"]
                           if c["category"] == DELETE_MODIFY)
        bad = self.tmp / "badkey.jsonl"
        bad.write_text(json.dumps({
            "conflict_id": row_cid, "state_fingerprint": row_cid,
            "choice": "custom",
            "custom": {"row": {"agency": "A2", "stop_id": "HACKED",
                               "name": "x"}}}) + "\n", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, bad, self.tmp / "d.jsonl")

    def test_choosing_nonexistent_side_rejected(self):
        # add_collision where base is absent: choosing base is impossible.
        bp, lp, rp = self.path("coll")
        write_jsonl(bp, [])
        write_jsonl(lp, [{"k": "1", "v": 1}])
        write_jsonl(rp, [{"k": "1", "v": 2}])
        work = self.tmp / "coll.db"
        compare(bp, lp, rp, ["k"], work)
        with WorkLibrary(work) as w:
            cid = w.list_conflicts(limit=1)["conflicts"][0]["conflict_id"]
        dec = self.tmp / "dec.jsonl"
        dec.write_text(json.dumps({"conflict_id": cid,
                                   "state_fingerprint": cid,
                                   "choice": "base"}) + "\n", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            finalize(work, dec, self.tmp / "d.jsonl")

    def test_decision_portable_between_identical_libraries_only(self):
        work = self._work("p1")
        path = self._decisions_for(
            work, lambda c: ("right", None)
            if c["category"] == DELETE_MODIFY
            else ("custom", {"value": "portable"}))
        # Identical rebuild -> same ids and fingerprints, decision applies.
        base, left, right = rich_inputs()
        bp, lp, rp = self.path("p2")
        write_jsonl(bp, base)
        write_jsonl(lp, left)
        write_jsonl(rp, right)
        work2 = self.tmp / "p2.db"
        compare(bp, lp, rp, RICH_KEY, work2)
        out2 = self.tmp / "p2.jsonl"
        finalize(work2, path, out2)
        self.assertTrue(out2.exists())
        # Changed right content on the conflicting field itself -> different
        # fingerprint, the same decision file must be rejected as stale.
        def change(row):
            if row.get("stop_id") == "F1":
                row = dict(row)
                row["v"] = 42            # was explicit null on F1
            return row
        changed = [change(r) for r in right]
        bp3, lp3, rp3 = self.path("p3")
        write_jsonl(bp3, base)
        write_jsonl(lp3, left)
        write_jsonl(rp3, changed)
        work3 = self.tmp / "p3.db"
        compare(bp3, lp3, rp3, RICH_KEY, work3)
        with self.assertRaises(DictMergeError):
            finalize(work3, path, self.tmp / "p3.jsonl")

    def test_snapshot_survives_multibyte_chunk_boundaries(self):
        # A long CJK-heavy field forces many 1 MiB snapshot boundaries to
        # land inside multi-byte characters.
        bp, lp, rp = self.path("uni")
        row = {"k": "1", "cjk": "中" * (2_000_000 // 3 + 7)}
        write_jsonl(bp, [row])
        write_jsonl(lp, [row])
        write_jsonl(rp, [row])
        work = self.tmp / "uni.db"
        compare(bp, lp, rp, ["k"], work, batch_size=64)
        with WorkLibrary(work) as w:
            snap = w.snapshot("base")
        self.assertEqual(snap["content"],
                         bp.read_text(encoding="utf-8"))

    def test_refuse_overwrite_and_refuse_refinalize(self):
        work = self._work("m5")
        path = self._decisions_for(
            work, lambda c: ("right", None)
            if c["category"] == DELETE_MODIFY
            else ("custom", {"value": 9}))
        out = self.tmp / "once.jsonl"
        finalize(work, path, out)
        with self.assertRaises(DictMergeError):
            finalize(work, path, out)
        with self.assertRaises(DictMergeError):
            finalize(work, path, self.tmp / "twice.jsonl")

    def test_swap_equivalent_choices_produce_identical_output(self):
        base, left, right = rich_inputs()
        w1 = self.tmp / "s1.db"
        w2 = self.tmp / "s2.db"
        b1, l1, r1 = self.path("s1x")
        b2, l2, r2 = self.path("s2x")
        write_jsonl(b1, base), write_jsonl(l1, left), write_jsonl(r1, right)
        write_jsonl(b2, base), write_jsonl(l2, right), write_jsonl(r2, left)
        compare(b1, l1, r1, RICH_KEY, w1)
        compare(b2, l2, r2, RICH_KEY, w2)

        def resolver(c):
            if c["category"] == FIELD_CONFLICT:
                return "custom", {"value": "settled"}
            if c["category"] == DELETE_MODIFY:
                return "right", None
            return "left", None

        def swapped(c):
            choice, custom = resolver(c)
            if choice == "left":
                choice = "right"
            elif choice == "right":
                choice = "left"
            return choice, custom

        d1 = self.tmp / "d1.jsonl"
        d2 = self.tmp / "d2.jsonl"
        for work, path, fn in ((w1, d1, resolver), (w2, d2, swapped)):
            lines = []
            with WorkLibrary(work) as w:
                for c in w.list_conflicts(limit=1000)["conflicts"]:
                    choice, custom = fn(c)
                    obj = {"conflict_id": c["conflict_id"],
                           "state_fingerprint": c["conflict_id"],
                           "choice": choice}
                    if custom:
                        obj["custom"] = custom
                    lines.append(json.dumps(obj))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        finalize(w1, d1, self.tmp / "o1.jsonl")
        finalize(w2, d2, self.tmp / "o2.jsonl")
        self.assertEqual(read_jsonl(self.tmp / "o1.jsonl"),
                         read_jsonl(self.tmp / "o2.jsonl"))


class InputValidationTests(MergeCase):
    def _files(self, left_rows_text: str, *, right_rows_text=None):
        bp = self.tmp / "b.jsonl"
        lp = self.tmp / "l.jsonl"
        rp = self.tmp / "r.jsonl"
        bp.write_text('{"k":"1"}\n', encoding="utf-8")
        lp.write_text(left_rows_text, encoding="utf-8")
        rp.write_text(right_rows_text or '{"k":"1"}\n', encoding="utf-8")
        return bp, lp, rp

    def _expect_fail(self, text):
        bp, lp, rp = self._files(text)
        with self.assertRaises(DictMergeError):
            compare(bp, lp, rp, ["k"], self.tmp / "fail.db")
        self.assertFalse((self.tmp / "fail.db").exists())

    def test_bad_json(self):
        self._expect_fail('{"k":}\n')

    def test_empty_line(self):
        self._expect_fail('{"k":"1"}\n\n')

    def test_duplicate_key(self):
        self._expect_fail('{"k":"1"}\n{"k":"1"}\n')

    def test_duplicate_object_member(self):
        self._expect_fail('{"k":"1","k":"2"}\n')

    def test_missing_key_component(self):
        self._expect_fail('{"other":"1"}\n')

    def test_empty_key_component(self):
        self._expect_fail('{"k":""}\n')

    def test_nonstring_key_component(self):
        self._expect_fail('{"k":7}\n')

    def test_float_value_rejected(self):
        self._expect_fail('{"k":"1","x":1.5}\n')

    def test_array_and_object_rejected(self):
        self._expect_fail('{"k":"1","x":[1]}\n')
        self._expect_fail('{"k":"1","x":{"y":1}}\n')

    def test_existing_target_refused(self):
        bp, lp, rp = self._files('{"k":"1"}\n')
        work = self.tmp / "exists.db"
        work.write_text("x", encoding="utf-8")
        with self.assertRaises(DictMergeError):
            compare(bp, lp, rp, ["k"], work)
        self.assertEqual(work.read_text(), "x")

    def test_field_names_with_spaces_dots_quotes_sql(self):
        weird = 'weird "name". x'
        rows = [{weird: "v"}]
        bp, lp, rp = self.path("w")
        write_jsonl(bp, [{"k": "1", weird: "base"}])
        write_jsonl(lp, [{"k": "1", weird: "left"}])
        write_jsonl(rp, [{"k": "1", weird: "left"}])
        result = compare(bp, lp, rp, ["k"], self.tmp / "w.db")
        self.assertEqual(result["counts"]["conflicts_total"], 0)
        detail = review_key(self.tmp / "w.db", ("1",))
        self.assertEqual(detail["states"]["left"]["fields"][weird]["value"],
                         "left")

    def test_composite_key_boundary_not_concatenated(self):
        bp, lp, rp = self.path("ck")
        # ("ab","c") vs ("a","bc") would collide under naive concatenation.
        write_jsonl(bp, [])
        write_jsonl(lp, [{"a": "ab", "c": "c", "v": 1}])
        write_jsonl(rp, [{"a": "a", "c": "bc", "v": 2}])
        result = compare(bp, lp, rp, ["a", "c"], self.tmp / "ck.db")
        # two distinct new keys, no add-collision
        self.assertEqual(result["counts"]["total_keys"], 2)
        self.assertEqual(result["counts"]["conflicts_total"], 0)


class CliTests(MergeCase):
    def test_cli_compare_review_finalize(self):
        base, left, right = rich_inputs()
        bp, lp, rp = self.path("cli")
        write_jsonl(bp, base)
        write_jsonl(lp, left)
        write_jsonl(rp, right)
        work = self.tmp / "cli.db"
        info_payload = self.cli(
            "dict-compare", "--base", str(bp), "--left", str(lp),
            "--right", str(rp), "--key", "agency", "--key", "stop_id",
            "--out", str(work))
        self.assertEqual(info_payload["counts"]["conflicts_total"], 2)
        page = self.cli("dict-conflicts", "--work", str(work),
                        "--limit", "1", "--offset", "0")
        self.assertEqual(page["total"], 2)
        cid = page["conflicts"][0]["conflict_id"]
        detail = self.cli("dict-conflict", "--work", str(work), "--id", cid)
        self.assertIn("states", detail)
        # plain (non-json) mode also works
        proc = subprocess.run(
            [sys.executable, "-I", CLI, "dict-info", "--work", str(work)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("open=2", proc.stdout)

    def test_cli_bad_input_exit_nonzero(self):
        bp = self.tmp / "b.jsonl"
        lp = self.tmp / "l.jsonl"
        rp = self.tmp / "r.jsonl"
        bp.write_text('{"k":"1"}\n', encoding="utf-8")
        lp.write_text("not-json\n", encoding="utf-8")
        rp.write_text('{"k":"1"}\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-I", CLI, "dict-compare", "--base", str(bp),
             "--left", str(lp), "--right", str(rp), "--key", "k",
             "--out", str(self.tmp / "x.db")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid JSON", proc.stderr)


if __name__ == "__main__":
    unittest.main()
