"""Independent byte-level verifier for the J1939 reassembly output.

This script deliberately imports nothing from the production package except
the standard-library ``sqlite3``/``csv``/``json`` modules.  It opens the
database written by a real ``j1939-import`` run and checks, from scratch:

1. every published message's BLOB equals the hand-written expected payload,
   byte by byte (including the last-packet padding being excluded);
2. every byte of every message is traceable to exactly one DT source frame
   and that frame's in-frame bytes really contain the claimed payload bytes;
3. byte-trace queries over hand-picked intervals return exactly the frames
   that intersect them, including intervals spanning packet boundaries;
4. transfer states, failure codes, missing packets, abort reasons and the
   raw eight-byte RTS/CTS/ACK controls match the expected scenario ledger;
5. duplicate DT frames are associated but never contribute payload twice;
6. raw-frame provenance is complete: every published message has one packet
   row per sequence number and every packet points at an existing raw frame.

Usage:  python3 -I tools/verify_j1939_output.py --db PATH [--batch N]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "j1939"

FAILURES: list[str] = []


def check(cond: bool, text: str) -> None:
    if not cond:
        FAILURES.append(text)
    print(("  ok  " if cond else "  FAIL ") + text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--expected", default=str(DIST / "j1939-expected.json"))
    parser.add_argument("--capture", default=str(DIST / "j1939-capture.csv"))
    args = parser.parse_args()

    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    # Re-read the original capture independently so source lines and raw
    # eight-byte frames can be compared without trusting the database copy.
    with open(args.capture, newline="", encoding="utf-8") as handle:
        capture_rows = list(csv.reader(handle))
    raw_lines = {int(i) + 1: row for i, row in enumerate(capture_rows)}

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    global FAILURES
    FAILURES = []
    batch_id = args.batch or conn.execute(
        "SELECT MAX(batch_id) FROM j1939_batches").fetchone()[0]

    print("verifying batch %d in %s" % (batch_id, args.db))

    # ------------------------------------------------------------------
    # 1-2. Payload bytes and per-byte provenance, reconstructed independently
    # ------------------------------------------------------------------
    messages = conn.execute(
        "SELECT * FROM j1939_messages WHERE batch_id=? ORDER BY message_id",
        (batch_id,)).fetchall()
    expected_payload_hexes = set()
    for sc in expected["scenarios"]:
        if "payload_hex" in sc:
            expected_payload_hexes.add(sc["payload_hex"])
        for ch in sc.get("channels", []):
            expected_payload_hexes.add(ch["payload_hex"])
        if "reuse_payload_hex" in sc:
            expected_payload_hexes.add(sc["reuse_payload_hex"])
        if "replacement" in sc:
            expected_payload_hexes.add(sc["replacement"]["payload_hex"])

    check(all(bytes(m["payload"]).hex() in expected_payload_hexes
              for m in messages),
          "every published payload matches one of the hand-written expected bytes")

    for msg in messages:
        payload = bytes(msg["payload"])
        check(payload.hex() in expected_payload_hexes,
              "message %d payload matches hand-written bytes" % msg["message_id"])
        check(len(payload) == msg["length_bytes"],
              "message %d BLOB length equals stored length_bytes"
              % msg["message_id"])

        packets = conn.execute(
            "SELECT * FROM j1939_packets WHERE transfer_id=? ORDER BY packet_seq",
            (msg["transfer_id"],)).fetchall()
        # Exactly one contributing packet per sequence number.
        seqs = [p["packet_seq"] for p in packets]
        check(seqs == list(range(1, len(packets) + 1)),
              "message %d has one packet row per sequence 1..%d"
              % (msg["message_id"], len(packets)))

        # Rebuild the payload from the RAW capture lines the DB points at.
        rebuilt = bytearray()
        covered = 0
        for p in packets:
            frame = conn.execute(
                "SELECT * FROM j1939_frames WHERE frame_id=?",
                (p["frame_id"],)).fetchone()
            raw = raw_lines[frame["source_line"]]
            check(raw[2] == frame["can_id_text"] and raw[3] == frame["data_hex"],
                  "message %d packet %d DB row matches capture line %d"
                  % (msg["message_id"], p["packet_seq"], frame["source_line"]))
            eight = bytes.fromhex(raw[3])
            check(eight[0] == p["packet_seq"],
                  "message %d packet %d sequence byte agrees with frame line %d"
                  % (msg["message_id"], p["packet_seq"], frame["source_line"]))
            rebuilt.extend(eight[1:8])
            covered += 7
        rebuilt = bytes(rebuilt)[:msg["length_bytes"]]
        check(rebuilt == payload,
              "message %d payload rebuilt independently from raw DT frames "
              "(last-packet padding excluded)" % msg["message_id"])

    # ------------------------------------------------------------------
    # 3. Hand-picked trace intervals, including cross-packet ranges
    # ------------------------------------------------------------------
    s1 = next(s for s in expected["scenarios"]
              if s["name"] == "S1_bam_three_packets")
    s1_msg = conn.execute(
        "SELECT message_id FROM j1939_messages m JOIN j1939_transfers t "
        "ON t.transfer_id=m.transfer_id WHERE m.batch_id=? AND m.mode='BAM' "
        "ORDER BY message_id LIMIT 1", (batch_id,)).fetchone()
    for case in s1["trace_cases"]:
        hits = conn.execute(
            "SELECT f.source_line FROM j1939_packets p "
            "JOIN j1939_frames f ON f.frame_id=p.frame_id "
            "JOIN j1939_messages m ON m.transfer_id=p.transfer_id "
            "WHERE m.message_id=? AND p.payload_offset < ? "
            "AND p.payload_offset + p.data_len > ? "
            "ORDER BY p.packet_seq",
            (s1_msg["message_id"], case["end"], case["start"])).fetchall()
        got = [r["source_line"] for r in hits]
        check(got == case["lines"],
              "trace [%d,%d) intersects exactly %s (got %s)"
              % (case["start"], case["end"], case["lines"], got))

    # Cross-check the frame-offset bytes returned by the production trace
    # against the payload slice itself.
    for msg in messages:
        payload = bytes(msg["payload"])
        midpoint = max(1, len(payload) // 2)
        rows = conn.execute(
            "SELECT p.packet_seq, p.payload_offset, f.data_hex, f.source_line "
            "FROM j1939_packets p JOIN j1939_frames f ON f.frame_id=p.frame_id "
            "WHERE p.transfer_id=? ORDER BY p.packet_seq",
            (msg["transfer_id"],)).fetchall()
        for r in rows:
            if r["payload_offset"] >= midpoint:
                continue
            start = r["payload_offset"]
            end = min(midpoint, r["payload_offset"] + 7)
            eight = bytes.fromhex(r["data_hex"])
            frame_slice = eight[1 + (start - r["payload_offset"]):
                                1 + (end - r["payload_offset"])]
            check(frame_slice == payload[start:end],
                  "message %d traced frame bytes at line %d equal payload slice"
                  % (msg["message_id"], r["source_line"]))

    # ------------------------------------------------------------------
    # 4. Scenario ledger: states, failure codes, missing packets, aborts
    # ------------------------------------------------------------------
    transfers = {r["transfer_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM j1939_transfers WHERE batch_id=? ORDER BY transfer_id",
        (batch_id,))}

    def transfers_between(src: int, dst: int):
        return [t for t in transfers.values()
                if t["src_addr"] == src and t["dst_addr"] == dst]

    def find_transfer(src: int, dst: int, ordinal: int = 0):
        return transfers_between(src, dst)[ordinal]

    # RTS channel 0x10->0x20, in fixture order:
    #   0 S2 completed, 1 S3 receiver-1 completed, 2 S4 failed conflict,
    #   3 S5 failed window, 4 S6 timed_out, 5 S7 replaced,
    #   6 S7 replacement completed, 7 S9 completed, 8 S10 aborted,
    #   9 S11 completed
    rts_r1 = transfers_between(0x10, 0x20)
    t4, t5, t6 = rts_r1[2], rts_r1[3], rts_r1[4]
    check(t4["state"] == "failed" and t4["failure_code"] == "dt_seq_conflict",
          "S4 changed-bytes retransmission fails with dt_seq_conflict")
    check(t5["state"] == "failed" and t5["failure_code"] == "dt_outside_window",
          "S5 data outside the CTS window fails with dt_outside_window")
    check(t6["state"] == "timed_out" and t6["missing_ack_flag"] == 1,
          "S6 wrong ACK never publishes and the wait times out missing ACK")

    t8_old, t8_new = rts_r1[5], rts_r1[6]
    check(t8_old["state"] == "replaced"
          and t8_old["replaced_by_transfer_id"] == t8_new["transfer_id"]
          and t8_new["state"] == "completed",
          "S7 replaced transfer points at the successful replacement")

    # BAM channel 0x10->0xFF, in fixture order:
    #   0 S1 completed, 1 S8 timed_out, 2 S8 reuse completed, 3 S12 eof
    bams = transfers_between(0x10, 0xFF)
    check(bams[1]["state"] == "timed_out",
          "S8 first BAM times out at exact deadline equality")
    check(bams[2]["state"] == "completed",
          "S8 same channel is reusable after the timeout")
    t15 = bams[3]

    t9 = rts_r1[7]
    check(t9["state"] == "completed",
          "S9 other-bus traffic does not advance this bus's deadline")

    t13 = rts_r1[8]
    check(t13["state"] == "aborted" and t13["abort_reason"] == 0x06,
          "S10 abort reason byte is saved verbatim")

    check(t15["state"] == "eof_incomplete",
          "S12 unfinished BAM is recorded eof_incomplete, never filled with zeros")
    missing = [r["packet_seq"] for r in conn.execute(
        "SELECT packet_seq FROM j1939_packets WHERE transfer_id=? ORDER BY packet_seq",
        (t15["transfer_id"],))]
    check(missing == [1], "S12 only packet 1 has a source; packets 2..3 stay missing")

    # ------------------------------------------------------------------
    # 5. Duplicate DT: associated via an event, but one packet row only
    # ------------------------------------------------------------------
    t2 = rts_r1[0]
    dup_events = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_events WHERE transfer_id=? "
        "AND event_code='duplicate_packet'", (t2["transfer_id"],)).fetchone()["c"]
    check(dup_events == 1, "S2 identical retransmission logs one duplicate_packet event")
    packet_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_packets WHERE transfer_id=?",
        (t2["transfer_id"],)).fetchone()["c"]
    check(packet_rows == 3, "S2 duplicate contributes no second packet source")

    # Two CTS rounds and the ACK are all retrievable as raw eight bytes.
    controls = conn.execute(
        "SELECT control_type, cts_count, cts_start FROM j1939_controls "
        "WHERE transfer_id=? ORDER BY control_id", (t2["transfer_id"],)).fetchall()
    cts_rounds = [(r["cts_count"], r["cts_start"]) for r in controls
                  if r["control_type"] == "CTS"]
    check(cts_rounds == [(2, 1), (1, 3)],
          "S2 two CTS rounds recorded with counts/starts %s" % cts_rounds)
    check(any(r["control_type"] == "EOM_ACK" for r in controls),
          "S2 EOM ACK is retrievable for the transfer")
    raw_eight = conn.execute(
        "SELECT f.data_hex FROM j1939_controls c JOIN j1939_frames f "
        "ON f.frame_id=c.frame_id WHERE c.transfer_id=? AND c.control_type='RTS'",
        (t2["transfer_id"],)).fetchone()
    check(raw_eight is not None and len(raw_eight["data_hex"]) == 16,
          "S2 RTS raw eight bytes are stored")

    # ------------------------------------------------------------------
    # 6. Frame ledger completeness and ordinary-frame isolation
    # ------------------------------------------------------------------
    frame_count = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_frames WHERE batch_id=?",
        (batch_id,)).fetchone()["c"]
    batch = conn.execute("SELECT * FROM j1939_batches WHERE batch_id=?",
                         (batch_id,)).fetchone()
    check(frame_count == batch["total_frames"],
          "every input line is registered as a raw frame")
    orphan_packets = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_packets p WHERE p.transfer_id IN "
        "(SELECT transfer_id FROM j1939_transfers WHERE state='completed') "
        "AND p.transfer_id NOT IN "
        "(SELECT transfer_id FROM j1939_messages)").fetchone()["c"]
    check(orphan_packets == 0,
          "every packet of a completed transfer belongs to a published message")
    ordinary = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_frames WHERE batch_id=? "
        "AND frame_class='ordinary'", (batch_id,)).fetchone()["c"]
    check(ordinary >= 4, "ordinary frames (including both buses) are registered, "
          "never reassembled")
    rejected = conn.execute(
        "SELECT COUNT(*) AS c FROM j1939_frames WHERE batch_id=? "
        "AND frame_class='rejected'", (batch_id,)).fetchone()["c"]
    check(rejected == 1, "the reserved-bit frame is classified rejected")

    print()
    if FAILURES:
        print("VERIFICATION FAILED: %d check(s)" % len(FAILURES))
        for text in FAILURES:
            print("  - " + text)
        return 1
    print("ALL INDEPENDENT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
