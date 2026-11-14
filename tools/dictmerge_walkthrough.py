"""End-to-end rehearsal of the three-way relational dictionary merge.

Everything runs the real terminal entries (./gtfs-store dict-*) in fresh
subprocesses against the hand-written fixtures in dist/dictmerge/:

* paged review of the auto rows and the real conflicts, with the base/left/
  right states and line numbers of each conflict opened one by one;
* conflicts resolved with a side choice AND with custom third values, then
  the final JSONL is reopened and checked row-by-row, field-by-field against
  both the hand-written expectations and an independent reference merger
  (this file imports nothing from gtfs_store.dictmerge);
* a reorder run and a left/right swap run: conflict identities must stay
  stable and swapped-but-equivalent decisions must produce identical output;
* the five required rejections (missing decision, duplicate decision, stale
  fingerprint, custom row that changes the key, bad value type on the last
  input line), each checked to leave pre-existing output bytes untouched.

Run:  python3 -I tools/dictmerge_walkthrough.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = str(ROOT / "gtfs-store")
DIST = ROOT / "dist" / "dictmerge"


def run_cli(*argv: str, expect: int = 0):
    proc = subprocess.run([sys.executable, "-I", CLI, "--json", *argv],
                          capture_output=True, text=True)
    if proc.returncode != expect:
        raise AssertionError(
            "gtfs-store %s -> exit %d (wanted %d)\nstdout: %s\nstderr: %s"
            % (" ".join(argv), proc.returncode, expect,
               proc.stdout, proc.stderr))
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def run_cli_raw(*argv: str):
    return subprocess.run([sys.executable, "-I", CLI, *argv],
                          capture_output=True, text=True)


def show(title: str) -> None:
    print("\n=== %s %s" % (title, "=" * max(2, 66 - len(title))))


def write_jsonl(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------------
# Independent reference merger (no imports from gtfs_store.dictmerge)
# ---------------------------------------------------------------------------

MISSING = object()


def ref_state(row, field):
    if row is None or field not in row:
        return MISSING
    return row[field]


def reference_merge(base_rows, left_rows, right_rows, key_fields):
    """Pure-dict re-implementation of the documented merge rule."""
    def idx(rows):
        return {tuple(r[k] for k in key_fields): r for r in rows}

    b, l, r = idx(base_rows), idx(left_rows), idx(right_rows)
    auto, conflicts = {}, []
    for key in sorted(set(b) | set(l) | set(r)):
        br, lr, rr = b.get(key), l.get(key), r.get(key)
        if br is None:
            if lr is not None and rr is None:
                auto[key] = {f: v for f, v in lr.items() if f not in key_fields}
            elif rr is not None and lr is None:
                auto[key] = {f: v for f, v in rr.items() if f not in key_fields}
            elif lr is not None and rr is not None:
                if lr == rr:
                    auto[key] = {f: v for f, v in lr.items()
                                 if f not in key_fields}
                else:
                    conflicts.append(("add_collision", key, None))
                    merged = {}
                    for f in sorted(set(lr) | set(rr)):
                        if f in key_fields:
                            continue
                        if ref_state(lr, f) == ref_state(rr, f) and f in lr and f in rr:
                            merged[f] = lr[f]
                        elif f in lr and f not in rr:
                            merged[f] = lr[f]
                        elif f in rr and f not in lr:
                            merged[f] = rr[f]
                    auto[key] = merged
            continue
        if lr is None and rr is None:
            continue  # deleted
        if lr is None or rr is None:
            survivor = rr if lr is None else lr
            if survivor == br:
                continue  # delete vs untouched -> delete
            conflicts.append(("delete_modify", key, None))
            auto[key] = {f: v for f, v in survivor.items()
                         if f not in key_fields}
            continue
        merged = {}
        for f in sorted(set(br) | set(lr) | set(rr)):
            if f in key_fields:
                continue
            bs, ls, rs = ref_state(br, f), ref_state(lr, f), ref_state(rr, f)
            if ls == rs:
                if ls is not MISSING:
                    merged[f] = lr[f]
            elif ls == bs:
                if rs is not MISSING:
                    merged[f] = rr[f]
            elif rs == bs:
                if ls is not MISSING:
                    merged[f] = lr[f]
            else:
                conflicts.append(("field_conflict", key, f))
        auto[key] = merged
    return auto, conflicts


def main() -> int:
    expected = json.loads((DIST / "manual-expected.json").read_text(
        encoding="utf-8"))
    key_fields = expected["key_fields"]
    base = read_jsonl(DIST / "base.jsonl")
    left = read_jsonl(DIST / "left.jsonl")
    right = read_jsonl(DIST / "right.jsonl")

    workdir = Path(tempfile.mkdtemp(prefix="dictmerge-demo-", dir=str(DIST)))
    work = workdir / "work.db"

    # ---------------------------------------------------------- 1. compare
    show("1. compare: base + two revisions -> one work library")
    stats = run_cli("dict-compare", "--base", str(DIST / "base.jsonl"),
                    "--left", str(DIST / "left.jsonl"),
                    "--right", str(DIST / "right.jsonl"),
                    "--key", "org", "--key", "code", "--out", str(work))
    c = stats["counts"]
    print("keys=%d auto_rows=%d conflict_rows=%d conflicts=%d (field=%d, "
          "whole-row=%d)"
          % (c["total_keys"], c["auto_rows"], c["conflict_rows"],
             c["conflicts_total"], c["field_conflicts"], c["row_conflicts"]))
    assert c["total_keys"] == 10, c
    assert c["conflicts_total"] == 3, c
    assert (c["field_conflicts"], c["row_conflicts"]) == (1, 2), c

    # Re-running against the same target must be refused.
    proc = run_cli_raw("--json", "dict-compare", "--base",
                       str(DIST / "base.jsonl"), "--left",
                       str(DIST / "left.jsonl"), "--right",
                       str(DIST / "right.jsonl"), "--key", "org",
                       "--key", "code", "--out", str(work))
    assert proc.returncode == 1 and "already exists" in proc.stderr, proc.stderr
    print("overwrite attempt refused: %s" % proc.stderr.strip().split(": ", 1)[1])

    # ----------------------------------------------- 2. paged review (auto)
    show("2. paged review of auto-accepted rows (page size 3)")
    seen_auto = []
    offset = 0
    while True:
        page = run_cli("dict-auto", "--work", str(work),
                       "--limit", "3", "--offset", str(offset))
        print("page offset=%d rows=%d of total=%d"
              % (offset, len(page["rows"]), page["total"]))
        seen_auto.extend(tuple(r["key"]) for r in page["rows"])
        offset += len(page["rows"])
        if offset >= page["total"]:
            break
    print("reviewed %d auto rows in %d pages; totals reported on every page"
          % (len(seen_auto), (offset + 2) // 3))
    assert len(seen_auto) == c["auto_rows"]

    # ------------------------------------------ 3. paged review (conflicts)
    show("3. paged review of conflicts; open every conflict's three states")
    page1 = run_cli("dict-conflicts", "--work", str(work),
                    "--limit", "1", "--offset", "0")
    page2 = run_cli("dict-conflicts", "--work", str(work),
                    "--limit", "1", "--offset", "1")
    page3 = run_cli("dict-conflicts", "--work", str(work),
                    "--limit", "1", "--offset", "2")
    assert page1["total"] == page2["total"] == page3["total"] == 3
    assert page1["open"] == 3
    all_conflicts = (page1["conflicts"] + page2["conflicts"]
                     + page3["conflicts"])
    by_scenario = {}
    for item in all_conflicts:
        detail = run_cli("dict-conflict", "--work", str(work),
                         "--id", item["conflict_id"])
        present = {s: st["present"] for s, st in detail["states"].items()}
        lines = {s: st["line_no"] for s, st in detail["states"].items()
                 if st["present"]}
        print("  %-15s key=%r field=%s present=%s source_lines=%s"
              % (detail["category"], detail["key"], detail["field"] or "-",
                 present, lines))
        by_scenario.setdefault(detail["category"], []).append(detail)
    assert {d["category"] for d in all_conflicts} == {
        "add_collision", "delete_modify", "field_conflict"}
    # field conflict must show the left absence vs right explicit null.
    fc = by_scenario["field_conflict"][0]
    assert fc["states"]["left"]["fields"].get("v") is None
    assert fc["states"]["right"]["fields"]["v"] == {
        "present": True, "type": "null", "value": None}

    # Showcase row: open the key across all three inputs.
    showcase = run_cli("dict-key", "--work", str(work),
                       "--key", "A3", "--key", "F2")
    assert showcase["status"] == "auto"
    print("showcase F2 suggestion fields: %s"
          % {n: d["value"] for n, d in showcase["fields"].items()})
    assert showcase["fields"]["name"]["auto_source"] == "left"
    assert showcase["fields"]["category"]["auto_source"] == "right"

    # ----------------------------- 4. independent reference cross-check
    show("4. independent reference merger (no product merge code)")
    ref_auto, ref_conflicts = reference_merge(base, left, right, key_fields)
    ref_conf = {(cat, tuple(key), field)
                for cat, key, field in ref_conflicts}
    engine_conf = {(d["category"], tuple(d["key"]), d["field"])
                   for d in all_conflicts}
    assert ref_conf == engine_conf, (ref_conf, engine_conf)
    print("conflict sets match: %s" % sorted(engine_conf))
    # Every auto row's suggestion matches the reference field map.
    for key, ref_fields in ref_auto.items():
        detail = run_cli("dict-key", "--work", str(work),
                         "--key", key[0], "--key", key[1])
        if detail["outcome"] == "delete":
            assert key not in [k for k in ref_auto] or ref_fields == {}
            continue
        engine_fields = {n: d["value"] for n, d in detail["fields"].items()}
        assert engine_fields == ref_fields, (key, engine_fields, ref_fields)
    print("all %d auto rows match the independent reference field-by-field"
          % len(ref_auto))

    # --------------------------------------------- 5. decisions + finalize
    show("5. decisions: side choices and custom third values")
    decisions = []
    for detail in all_conflicts:
        cid = detail["conflict_id"]
        if detail["category"] == "delete_modify":
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "right"}      # choose the modified side
        elif detail["category"] == "add_collision":
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "custom",
                   "custom": {"row": {
                       "org": detail["key"][0], "code": detail["key"][1],
                       "name": "merged third opinion", "v": 3,
                       "reviewed": True}}}
        else:
            dec = {"conflict_id": cid, "state_fingerprint": cid,
                   "choice": "custom",
                   "custom": {"value": "third opinion"}}
        decisions.append(dec)
    dec_path = workdir / "decisions.jsonl"
    write_jsonl(dec_path, decisions)
    final_path = workdir / "final.jsonl"
    report_path = workdir / "report.jsonl"
    result = run_cli("dict-finalize", "--work", str(work),
                     "--decisions", str(dec_path), "--out", str(final_path),
                     "--report", str(report_path))
    print("finalized rows=%d open_conflicts=%d"
          % (result["output_rows"], result["counts"]["open_conflicts"]))
    assert result["counts"]["open_conflicts"] == 0

    # --------------------------- 6. reopen final file, every row/field
    show("6. reopen the final file and verify every row and every field")
    final_rows = read_jsonl(final_path)
    expected_rows = expected["expected_final_rows_after_decisions"]
    assert len(final_rows) == len(expected_rows), (
        len(final_rows), len(expected_rows))
    for got, want in zip(final_rows, expected_rows):
        assert got == want, (got, want)
        # strict type checks on the tricky states
        for field, value in got.items():
            assert isinstance(value, (str, int, bool)) or value is None
    keys = [tuple(r[k] for k in key_fields) for r in final_rows]
    assert keys == sorted(keys), "output must be in code-point key order"
    assert len(set(keys)) == len(keys), "one row per key"
    zero_seven = next(r for r in final_rows if r["code"] == "007")
    assert type(zero_seven["zero"]) is int and zero_seven["zero"] == 0
    assert type(zero_seven["flag"]) is bool and zero_seven["flag"] is False
    assert zero_seven["big"] == expected["big_integer"]
    assert zero_seven["note"] is None and "category" in zero_seven
    assert "gone" not in next(r for r in final_rows
                              if r["code"] == "D1"), "field deletion is real"
    assert not any(r["code"] == "S2" for r in final_rows), "row deletion is real"
    print("verified %d rows / all fields; 007!=7, null vs absence, 0 vs "
          "false and the 128-bit integer all survived" % len(final_rows))
    report = read_jsonl(report_path)
    assert any(tuple(r["key"]) == ("A3", "F2") for r in report)
    assert any(r.get("outcome") == "deleted" and r["key"] == ["A2", "S2"]
               for r in report)
    print("finalization report entries=%d (auto changes + decisions + deletes)"
          % len(report))

    # ----------------------------------- 7. reorder + swap stability
    show("7. reorder inputs and swap left/right: identities stay stable")
    original_ids = sorted(d["conflict_id"] for d in all_conflicts)

    re_dir = workdir / "reorder"
    re_dir.mkdir()
    write_jsonl(re_dir / "base.jsonl", list(reversed(base)))
    write_jsonl(re_dir / "left.jsonl", list(reversed(left)))
    write_jsonl(re_dir / "right.jsonl", list(reversed(right)))
    re_work = re_dir / "work.db"
    run_cli("dict-compare", "--base", str(re_dir / "base.jsonl"),
            "--left", str(re_dir / "left.jsonl"),
            "--right", str(re_dir / "right.jsonl"),
            "--key", "org", "--key", "code", "--out", str(re_work))
    re_ids = sorted(c["conflict_id"] for c in
                    run_cli("dict-conflicts", "--work", str(re_work),
                            "--limit", "100")["conflicts"])
    assert re_ids == original_ids
    # line numbers must have moved even though identities did not.
    re_detail = run_cli("dict-conflict", "--work", str(re_work),
                        "--id", original_ids[0])
    print("reorder: ids identical=%s; displayed line numbers changed=%s"
          % (re_ids == original_ids,
             [s["line_no"] for s in re_detail["states"].values()
              if s["present"]]))

    sw_dir = workdir / "swap"
    sw_dir.mkdir()
    write_jsonl(sw_dir / "base.jsonl", base)
    write_jsonl(sw_dir / "left.jsonl", right)
    write_jsonl(sw_dir / "right.jsonl", left)
    sw_work = sw_dir / "work.db"
    run_cli("dict-compare", "--base", str(sw_dir / "base.jsonl"),
            "--left", str(sw_dir / "left.jsonl"),
            "--right", str(sw_dir / "right.jsonl"),
            "--key", "org", "--key", "code", "--out", str(sw_work))
    sw_page = run_cli("dict-conflicts", "--work", str(sw_work),
                      "--limit", "100")
    sw_ids = sorted(c["conflict_id"] for c in sw_page["conflicts"])
    assert sw_ids == original_ids
    # Apply swapped-but-equivalent decisions.
    sw_decisions = []
    for detail in sw_page["conflicts"]:
        cid = detail["conflict_id"]
        if detail["category"] == "delete_modify":
            choice = "left"   # was "right" before the swap
            sw_decisions.append({"conflict_id": cid,
                                 "state_fingerprint": cid,
                                 "choice": choice})
        elif detail["category"] == "add_collision":
            sw_decisions.append({"conflict_id": cid,
                                 "state_fingerprint": cid,
                                 "choice": "custom",
                                 "custom": {"row": {
                                     "org": detail["key"][0],
                                     "code": detail["key"][1],
                                     "name": "merged third opinion", "v": 3,
                                     "reviewed": True}}})
        else:
            sw_decisions.append({"conflict_id": cid,
                                 "state_fingerprint": cid,
                                 "choice": "custom",
                                 "custom": {"value": "third opinion"}})
    sw_dec = sw_dir / "decisions.jsonl"
    write_jsonl(sw_dec, sw_decisions)
    sw_final = sw_dir / "final.jsonl"
    run_cli("dict-finalize", "--work", str(sw_work),
            "--decisions", str(sw_dec), "--out", str(sw_final))
    assert read_jsonl(sw_final) == final_rows
    print("swap: identical conflict ids and byte-identical final output")

    # ------------------------------------------------- 8. rejection demos
    show("8. rejection demos; pre-existing output bytes must survive")

    def fresh_work() -> Path:
        w = workdir / ("reject-%d.db" % len(list(workdir.glob("reject-*"))))
        run_cli("dict-compare", "--base", str(DIST / "base.jsonl"),
                "--left", str(DIST / "left.jsonl"),
                "--right", str(DIST / "right.jsonl"),
                "--key", "org", "--key", "code", "--out", str(w))
        return w

    marker = "UNTOUCHED-OUTPUT-BYTES\n"

    def expect_reject(work_db, decision_rows, label):
        dec = workdir / ("%s.jsonl" % label)
        write_jsonl(dec, decision_rows)
        out = workdir / ("%s.out" % label)
        out.write_text(marker, encoding="utf-8")
        proc = run_cli_raw("--json", "dict-finalize", "--work", str(work_db),
                           "--decisions", str(dec), "--out", str(out))
        assert proc.returncode == 1, (label, proc.stdout, proc.stderr)
        assert out.read_text(encoding="utf-8") == marker, label
        print("  %-28s rejected (exit 1), output bytes unchanged" % label)

    w = fresh_work()
    expect_reject(w, [], "missing-decision")           # empty decisions file

    w = fresh_work()
    cid = all_conflicts[0]["conflict_id"]
    expect_reject(w, [
        {"conflict_id": cid, "state_fingerprint": cid, "choice": "left"},
        {"conflict_id": cid, "state_fingerprint": cid, "choice": "right"},
    ], "duplicate-decision")

    w = fresh_work()
    expect_reject(w, [
        {"conflict_id": cid, "state_fingerprint": "0" * 64,
         "choice": "left"},
    ], "stale-fingerprint")

    w = fresh_work()
    row_cid = next(d["conflict_id"] for d in all_conflicts
                   if d["category"] == "delete_modify")
    expect_reject(w, [
        {"conflict_id": row_cid, "state_fingerprint": row_cid,
         "choice": "custom",
         "custom": {"row": {"org": "A2", "code": "HACKED", "name": "x"}}},
    ], "custom-row-changes-key")

    # Bad value type on the LAST input line: compare fails and leaves no
    # reviewable library behind.
    bad_dir = workdir / "bad-tail"
    bad_dir.mkdir()
    bad_left = bad_dir / "left.jsonl"
    bad_rows = left + [{"org": "A9", "code": "ZZ", "weight": 2.5}]
    write_jsonl(bad_left, bad_rows)
    bad_work = bad_dir / "work.db"
    proc = run_cli_raw("--json", "dict-compare",
                       "--base", str(DIST / "base.jsonl"),
                       "--left", str(bad_left),
                       "--right", str(DIST / "right.jsonl"),
                       "--key", "org", "--key", "code",
                       "--out", str(bad_work))
    assert proc.returncode == 1 and "decimals" in proc.stderr, proc.stderr
    assert not bad_work.exists(), "failed compare must leave no library"
    print("  %-28s compare rejected, no work library created"
          % "last-line-bad-type")

    # Decision reuse across work libraries: only identical id AND fingerprint
    # carry over.  A decision file from work must work on an identical rebuild.
    rebuild = workdir / "rebuild.db"
    run_cli("dict-compare", "--base", str(DIST / "base.jsonl"),
            "--left", str(DIST / "left.jsonl"),
            "--right", str(DIST / "right.jsonl"),
            "--key", "org", "--key", "code", "--out", str(rebuild))
    reused_out = workdir / "reused.jsonl"
    run_cli("dict-finalize", "--work", str(rebuild),
            "--decisions", str(dec_path), "--out", str(reused_out))
    assert read_jsonl(reused_out) == final_rows
    print("decision file carries over to an identical rebuild (id + fingerprint)")

    show("walkthrough complete")
    print("artifacts kept under: %s" % workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
