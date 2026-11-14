"""Build the hand-written fixtures for the three-way dictionary merge demo.

Everything under dist/dictmerge/ is regenerated from scratch.  The three
JSONL inputs deliberately contain:

* a composite primary key (org, code) whose component boundary matters;
* the ids "007" and "7" (no leading-zero stripping, literal comparison);
* an explicit JSON null, an absent column, integer 0 and boolean false
  (four states that must never collapse);
* an integer beyond signed 64-bit range;
* a left-side display-name rename next to a right-side category addition,
  so both teams' contributions must survive;
* the five hand-listed scenarios, each with a written expectation in
  manual-expected.json: same-value double edit, delete-vs-unchanged,
  delete-vs-modify, add collision, field-delete-vs-set-null.

Run:  python3 -I tools/build_dictmerge_fixtures.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "dictmerge"

KEY = ["org", "code"]
BIG = 2 ** 127 + 19  # well beyond signed 64-bit, inside signed 128-bit


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build() -> dict:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # ------------------------------------------------------------------ base
    base = [
        # Composite key; 007 vs 7; null vs absent; 0 vs false; huge integer.
        {"org": "A1", "code": "007", "name": "Central", "category": None,
         "zero": 0, "flag": False, "big": BIG},
        {"org": "A1", "code": "7", "name": "Literal seven", "category": "C0"},
        # Scenario 1: same value double edit -> auto, taken once.
        {"org": "A2", "code": "S1", "name": "old label", "owner": "ops"},
        # Scenario 2: delete vs untouched -> auto delete.
        {"org": "A2", "code": "S2", "name": "retire me", "owner": "ops"},
        # Scenario 3: delete vs modify -> whole-row delete_modify conflict.
        {"org": "A2", "code": "S3", "name": "disputed", "v": "x"},
        # The showcase row: left renames, right adds a category.
        {"org": "A3", "code": "F2", "name": "Rename me"},
        # Scenario 5: field delete vs explicit null -> field conflict.
        {"org": "A3", "code": "F1", "name": "Drop vs null", "v": 1},
        # Untouched control row.
        {"org": "A4", "code": "U1", "name": "Unchanged"},
        # Field deleted on one side, untouched on the other -> auto absent.
        {"org": "A5", "code": "D1", "name": "field delete", "gone": "z"},
    ]

    # ------------------------------------------------------------------ left
    left = [
        {"org": "A1", "code": "007", "name": "Central (renamed L)",
         "category": None, "zero": 0, "flag": False, "big": BIG},
        {"org": "A1", "code": "7", "name": "Literal seven", "category": "C0"},
        # Scenario 1: both teams write the identical new label.
        {"org": "A2", "code": "S1", "name": "agreed label", "owner": "ops"},
        # Scenario 2: left deletes S2.
        # Scenario 3: left deletes S3.
        # Showcase: left changes the display name only.
        {"org": "A3", "code": "F2", "name": "Rename me (L)"},
        # Scenario 5: left deletes field v (the key stays).
        {"org": "A3", "code": "F1", "name": "Drop vs null"},
        {"org": "A4", "code": "U1", "name": "Unchanged"},
        # D1: left drops "gone"; right never touches it -> auto absent.
        {"org": "A5", "code": "D1", "name": "field delete"},
        # Scenario 4 ingredient: left adds a brand-new key.
        {"org": "A9", "code": "N1", "name": "only left new", "v": 1},
    ]

    # ----------------------------------------------------------------- right
    right = [
        {"org": "A1", "code": "007", "name": "Central", "category": "URBAN",
         "zero": 0, "flag": False, "big": BIG, "note": None},
        {"org": "A1", "code": "7", "name": "Literal seven", "category": "C0"},
        # Scenario 1: right writes the identical new label.
        {"org": "A2", "code": "S1", "name": "agreed label", "owner": "ops"},
        # Scenario 2: right keeps S2 exactly as base -> delete wins.
        {"org": "A2", "code": "S2", "name": "retire me", "owner": "ops"},
        # Scenario 3: right modifies S3 instead of deleting it.
        {"org": "A2", "code": "S3", "name": "disputed", "v": "y"},
        # Showcase: right adds a category (name stays at base).
        {"org": "A3", "code": "F2", "name": "Rename me", "category": "RAIL"},
        # Scenario 5: right sets the same field v to explicit null.
        {"org": "A3", "code": "F1", "name": "Drop vs null", "v": None},
        {"org": "A4", "code": "U1", "name": "Unchanged"},
        {"org": "A5", "code": "D1", "name": "field delete", "gone": "z"},
        # Scenario 4 ingredient: right independently adds the same key with
        # different content -> add_collision, never fused into one row.
        {"org": "A9", "code": "N1", "name": "only right new", "v": 2},
    ]

    write_jsonl(DIST / "base.jsonl", base)
    write_jsonl(DIST / "left.jsonl", left)
    write_jsonl(DIST / "right.jsonl", right)

    # Hand-written expectations (not derived from engine output).
    expected = {
        "key_fields": KEY,
        "big_integer": BIG,
        "scenarios": [
            {"id": "same-value-double-edit", "key": ["A2", "S1"],
             "expect": "auto keep; name='agreed label' taken once (source both)"},
            {"id": "delete-vs-unchanged", "key": ["A2", "S2"],
             "expect": "auto delete; the row must not appear in the final file"},
            {"id": "delete-vs-modify", "key": ["A2", "S3"],
             "expect": "whole-row conflict delete_modify; no per-field conflicts"},
            {"id": "add-collision", "key": ["A9", "N1"],
             "expect": "whole-row conflict add_collision; two different new "
                       "objects stay separate until a decision"},
            {"id": "field-delete-vs-null", "key": ["A3", "F1"],
             "expect": "field_conflict on v; absence (left) vs explicit null "
                       "(right) cannot be auto-merged"},
        ],
        "showcase": {
            "key": ["A3", "F2"],
            "expect": "auto keep with both contributions: name from left "
                      "'Rename me (L)' and category from right 'RAIL'"},
        "state_integrity": {
            "key": ["A1", "007"],
            "expect": "007 stays the string '007' (never 7); category changes "
                      "null->'URBAN'; note is explicit null; zero is int 0; "
                      "flag is bool false; big round-trips exactly"},
        "distinct_key": ["A1", "7"],
        "auto_absent_field": {"key": ["A5", "D1"],
                              "expect": "field 'gone' absent from final row"},
        # Decisions the walkthrough will hand in (stable ids are filled in at
        # run time; here only the intended choice semantics are documented).
        "decision_plan": [
            {"scenario": "delete-vs-modify", "choice": "right",
             "meaning": "keep the modified right-hand row"},
            {"scenario": "add-collision", "choice": "custom",
             "meaning": "third-opinion custom row keeping the same key"},
            {"scenario": "field-delete-vs-null", "choice": "custom",
             "custom": "value",
             "meaning": "analyst types a third value instead of picking sides"},
        ],
        "expected_final_rows_after_decisions": [
            {"org": "A1", "code": "007", "name": "Central (renamed L)",
             "category": "URBAN", "zero": 0, "flag": False, "big": BIG,
             "note": None},
            {"org": "A1", "code": "7", "name": "Literal seven",
             "category": "C0"},
            {"org": "A2", "code": "S1", "name": "agreed label", "owner": "ops"},
            {"org": "A2", "code": "S3", "name": "disputed", "v": "y"},
            {"org": "A3", "code": "F1", "name": "Drop vs null",
             "v": "third opinion"},
            {"org": "A3", "code": "F2", "name": "Rename me (L)",
             "category": "RAIL"},
            {"org": "A4", "code": "U1", "name": "Unchanged"},
            {"org": "A5", "code": "D1", "name": "field delete"},
            {"org": "A9", "code": "N1", "name": "merged third opinion",
             "v": 3, "reviewed": True},
        ],
    }
    (DIST / "manual-expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return expected


if __name__ == "__main__":
    payload = build()
    print("wrote %d expected final rows and %d scenarios to %s"
          % (len(payload["expected_final_rows_after_decisions"]),
             len(payload["scenarios"]), DIST))
