"""Offline J1939 TP reassembly: parsing, state machine, deadlines and storage.

The tests exercise the narrow documented subset directly and also run the
full hand-written capture through the real CLI.  Byte-level end-to-end
provenance is cross-checked independently by tools/verify_j1939_output.py
(which imports no production reassembly code); this file focuses on the
contract rules and their diagnostics.

Run with:  python3 -I -m unittest tests.test_j1939 -v
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from helpers import ROOT, StoreTestCase

from gtfs_store.cli import main as cli_main
from gtfs_store.errors import GtfsError
from gtfs_store.j1939 import (
    J1939FileError,
    J1939Store,
    decode_can_id,
    parse_capture,
    valid_target_pgn,
)

DIST = ROOT / "dist" / "j1939"
DATA_GAP = 100
CONTROL_GAP = 50


def cid(prio, edp, dp, pf, ps, sa):
    return (prio << 26) | (edp << 25) | (dp << 24) | (pf << 16) | (ps << 8) | sa


def cm(b1, b2=0, b3=0, b4=0, b5=0, pgn=0x00F004):
    return bytes([b1, b2, b3, b4, b5, pgn & 0xFF, (pgn >> 8) & 0xFF,
                  (pgn >> 16) & 0xFF])


def rts(size, packets, max_cts, pgn=0x00F004):
    return cm(0x10, size & 0xFF, size >> 8, packets, max_cts, pgn)


def bam(size, packets, pgn=0x00F004):
    return cm(0x20, size & 0xFF, size >> 8, packets, 0xFF, pgn)


def cts(n, start, pgn=0x00F004):
    return cm(0x11, n, start, 0xFF, 0xFF, pgn)


def ack(size, packets, pgn=0x00F004):
    return cm(0x13, size & 0xFF, size >> 8, packets, 0xFF, pgn)


def abort(reason, pgn=0x00F004):
    return cm(0xFF, reason, 0xFF, 0xFF, 0xFF, pgn)


def dt(seq, chunk):
    chunk = chunk[:7]
    return bytes([seq]) + chunk + b"\x00" * (7 - len(chunk))


def write_csv(rows, header=("bus", "timestamp_ns", "can_id", "data_hex")):
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(list(header))
    for row in rows:
        w.writerow(row)
    return buf.getvalue()


class CaptureBuilder:
    """Tiny helper for building captures with explicit, monotonic timestamps."""

    def __init__(self, bus="b1", start=1000):
        self.bus = bus
        self.t = start
        self.rows = []

    def add(self, ident, data, ts=None, bus=None):
        if ts is None:
            ts = self.t
        self.t = max(self.t, ts)
        self.rows.append((bus or self.bus, ts, "%08X" % ident,
                          data.hex().upper()))
        return self

    def step(self, ident, data, delta=10, bus=None):
        self.t += delta
        return self.add(ident, data, self.t, bus)

    def text(self):
        return write_csv(self.rows)


class IdentifierTests(StoreTestCase):
    def test_pdu1_pdu2_and_priority_split(self):
        # PDU1: PF<240, PS is destination, PGN low byte zero.
        ident = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        can = decode_can_id(ident)
        self.assertEqual((can.priority, can.reserved, can.data_page,
                          can.pf, can.ps, can.source_addr),
                         (6, 0, 0, 0xEC, 0x20, 0x10))
        self.assertEqual(can.pgn, 0x00EC00)
        self.assertEqual(can.destination, 0x20)
        # PDU2: PF>=240, PS belongs to PGN, destination is global 255.
        ident2 = cid(3, 0, 1, 0xFE, 0xEE, 0x40)
        can2 = decode_can_id(ident2)
        self.assertEqual(can2.pgn, 0x01FEEE)
        self.assertEqual(can2.destination, 255)

    def test_target_pgn_rules(self):
        self.assertTrue(valid_target_pgn(0x00F004))
        self.assertTrue(valid_target_pgn(0x000000))
        self.assertFalse(valid_target_pgn(0x40000))          # > 18 bits
        self.assertFalse(valid_target_pgn(0x00EC01))         # PDU1 low byte
        self.assertFalse(valid_target_pgn(0x00EC00))         # TP.CM
        self.assertFalse(valid_target_pgn(0x00EB00))         # TP.DT


class ParseTests(StoreTestCase):
    def _import_text(self, text):
        path = self.tmp / "c.csv"
        path.write_text(text, encoding="utf-8")
        return J1939Store(self.store).import_capture(path, DATA_GAP, CONTROL_GAP)

    def test_header_must_match_exactly(self):
        with self.assertRaises(J1939FileError):
            list(parse_capture(write_csv([], header=("x", "timestamp_ns",
                                                      "can_id", "data_hex")).encode()
                               if False else write_csv([], header=("x",)).encode()))

    def test_missing_and_extra_columns_reject_file(self):
        ident = "%08X" % cid(6, 0, 0, 0xEC, 0x20, 0x10)
        # Short record: the CSV reader pads nothing, so the row has 3 fields.
        short = ("bus,timestamp_ns,can_id,data_hex\n"
                 "b1,1,%s\n" % ident)
        with self.assertRaises(J1939FileError):
            list(parse_capture(short.encode()))
        extra = ("bus,timestamp_ns,can_id,data_hex\n"
                 "b1,1,%s,%s,extra\n" % (ident, "00" * 8))
        with self.assertRaises(J1939FileError):
            list(parse_capture(extra.encode()))

    def test_bad_fields_and_backwards_time_reject_file(self):
        base = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        cases = [
            [("", "1", "%08X" % base, "00" * 8)],                       # empty bus
            [("b1", "-1", "%08X" % base, "00" * 8)],                    # negative ts
            [("b1", "1", "ABCDEF1", "00" * 8)],                         # 7 hex digits
            [("b1", "1", "ABCDEF10", "00" * 7)],                        # 7 data bytes
            [("b1", "1", "20000000", "00" * 8)],                        # >29 bit
            [("b1", "1", "ghijklmn", "00" * 8)],                        # non-hex id
        ]
        for rows in cases:
            with self.assertRaises(J1939FileError):
                list(parse_capture(write_csv(rows).encode()))
        # Non-decreasing is required; equal timestamps keep file order.
        rows = [("b1", "10", "%08X" % base, "00" * 8),
                ("b1", "9", "%08X" % base, "00" * 8)]
        with self.assertRaises(J1939FileError):
            list(parse_capture(write_csv(rows).encode()))

    def test_leading_zeros_preserved_in_storage(self):
        ident = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        rows = [("b1", "10", "00000000", "0011223344556677")]
        # all-zero can_id is an ordinary frame here; text form is stored raw
        self._import_text(write_csv(rows))
        row = self.store.conn.execute(
            "SELECT can_id_text, data_hex FROM j1939_frames").fetchone()
        self.assertEqual(row["can_id_text"], "00000000")
        self.assertEqual(row["data_hex"], "0011223344556677")

    def test_damaged_prefix_is_not_committed(self):
        path = self.tmp / "bad.csv"
        # Good BAM prefix, then a backwards-timestamp line.
        cap = CaptureBuilder()
        cap.add(cid(6, 0, 0, 0xEC, 0xFF, 0x10), bam(16, 3))
        cap.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10), dt(1, b"\x00" * 7))
        text = cap.text()
        text += "b1,%d,%s,%s\n" % (cap.t - 1, "%08X" % cid(6, 0, 0, 0xEB, 0xFF, 0x10),
                                   dt(2, b"\x00" * 7).hex().upper())
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(J1939FileError):
            J1939Store(self.store).import_capture(path, DATA_GAP, CONTROL_GAP)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM j1939_frames").fetchone()[0], 0)


class BamTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.js = J1939Store(self.store)

    def _import(self, cap):
        path = self.tmp / "c.csv"
        path.write_text(cap.text(), encoding="utf-8")
        return self.js.import_capture(path, DATA_GAP, CONTROL_GAP)

    def test_bam_three_packets_with_padding_excluded(self):
        payload = bytes([0xA0 + i for i in range(15)])
        cap = CaptureBuilder()
        cap.add(cid(6, 0, 0, 0xEC, 0xFF, 0x10), bam(15, 3))
        for i in range(3):
            cap.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10),
                     dt(i + 1, payload[i * 7:(i + 1) * 7]))
        self._import(cap)
        msg = self.js.messages()[0]
        self.assertEqual(msg["length_bytes"], 15)
        self.assertEqual(self.js.message_payload(msg["message_id"]), payload)
        self.assertIsNone(msg["ack_frame_id"] if "ack_frame_id" in msg else None)

    def test_size_limits_and_packet_count_math(self):
        # (size, announced packets, encodable, acceptable)
        for size, packets, encodable, ok in ((8, 2, True, False), (9, 2, True, True),
                                             (1785, 255, True, True),
                                             (1785, 254, True, False),
                                             (14, 2, True, False),
                                             (1786, 0, False, False)):
            cap = CaptureBuilder()
            if not encodable:
                # Build an out-of-range size field with an encodable packet byte.
                cap.add(cid(6, 0, 0, 0xEC, 0xFF, 0x10),
                        cm(0x20, size & 0xFF, size >> 8, 255, 0xFF))
            else:
                cap.add(cid(6, 0, 0, 0xEC, 0xFF, 0x10), bam(size, packets))
            self._import(cap)
            if ok:
                self.assertTrue(self.js.transfers())
                self.assertNotEqual(self.js.transfers()[-1]["state"], "rejected")
            else:
                ev = [e for e in self.js.events()
                      if e["event_code"] == "announce_rejected"]
                self.assertTrue(ev, "size=%d packets=%d must reject" % (size, packets))

    def test_bam_requires_global_destination_and_b5(self):
        cap = CaptureBuilder()
        # Destination 0x20 with a BAM control byte: must be rejected.
        cap.add(cid(6, 0, 0, 0xEC, 0x20, 0x10), bam(9, 2))
        self._import(cap)
        ev = self.js.events()
        self.assertTrue(any(e["event_code"] == "announce_rejected" for e in ev))
        self.assertEqual(self.js.transfers(), [])

    def test_reserved_bit_frame_rejected_but_sibling_transfer_ok(self):
        cap = CaptureBuilder()
        bad = cid(6, 1, 0, 0xFE, 0xEE, 0x60)
        cap.add(bad, b"\xDE" + b"\x00" * 7)
        cap.step(cid(6, 0, 0, 0xEC, 0xFF, 0x10), bam(9, 2))
        cap.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10), dt(1, b"\x01" * 7))
        cap.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10), dt(2, b"\x02\x03"))
        self._import(cap)
        classes = {r["frame_class"] for r in self.store.conn.execute(
            "SELECT DISTINCT frame_class FROM j1939_frames")}
        self.assertIn("rejected", classes)
        self.assertEqual(self.js.messages()[0]["length_bytes"], 9)

    def test_data_page_one_tp_frames_are_ordinary(self):
        # PF=0xEC/0xEB but data_page=1: numeric collision must not be treated
        # as TP; such frames are ordinary and never drive a transfer.
        dp1_cm = cid(6, 0, 1, 0xEC, 0xFF, 0x10)
        cap = CaptureBuilder()
        cap.add(dp1_cm, bam(9, 2))
        cap.step(cid(6, 0, 1, 0xEB, 0xFF, 0x10), dt(1, b"\x01" * 7))
        self._import(cap)
        classes = [r["frame_class"] for r in self.store.conn.execute(
            "SELECT frame_class FROM j1939_frames ORDER BY frame_seq")]
        self.assertEqual(classes, ["ordinary", "ordinary"])
        self.assertEqual(self.js.transfers(), [])
        self.assertEqual(self.js.messages(), [])


class RtsFlowControlTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.js = J1939Store(self.store)
        self.cm_s = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        self.cm_r = cid(6, 0, 0, 0xEC, 0x10, 0x20)
        self.dt_s = cid(6, 0, 0, 0xEB, 0x20, 0x10)

    def _import(self, cap):
        path = self.tmp / "c.csv"
        path.write_text(cap.text(), encoding="utf-8")
        return self.js.import_capture(path, DATA_GAP, CONTROL_GAP)

    def _happy(self, cap, payload):
        cap.add(self.cm_s, rts(len(payload), (len(payload) + 6) // 7, 2))
        cap.step(self.cm_r, cts(2, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        cap.step(self.dt_s, dt(2, payload[7:14]))
        cap.step(self.cm_r, cts(1, 3))
        cap.step(self.dt_s, dt(3, payload[14:16]))
        cap.step(self.cm_r, ack(len(payload), 3))

    def test_completed_only_after_ack(self):
        payload = bytes(range(16))
        cap = CaptureBuilder()
        self._happy(cap, payload)
        self._import(cap)
        tr = self.js.transfers()[0]
        self.assertEqual(tr["state"], "completed")
        msg = self.js.messages()[0]
        self.assertEqual(self.js.message_payload(msg["message_id"]), payload)
        self.assertIsNotNone(msg["ack_frame_id"])

    def test_early_ack_cannot_publish(self):
        payload = bytes(range(16))
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 3))
        cap.step(self.cm_r, cts(3, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        # ACK while packets are still missing: no publication.
        cap.step(self.cm_r, ack(16, 3))
        cap.step(self.dt_s, dt(2, payload[7:14]))
        cap.step(self.dt_s, dt(3, payload[14:16]))
        self._import(cap)
        self.assertEqual(self.js.messages(), [])
        ev = [e for e in self.js.events() if e["event_code"] == "ack_not_awaiting"]
        self.assertEqual(len(ev), 1)

    def test_wrong_ack_counts_rejected(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 2))
        cap.step(self.cm_r, cts(2, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        cap.step(self.dt_s, dt(2, b"\x02\x03"))
        cap.step(self.cm_r, ack(8, 2))
        self._import(cap)
        self.assertTrue(any(e["event_code"] == "ack_bad" for e in self.js.events()))
        self.assertEqual(self.js.messages(), [])

    def test_cts_validation_rules(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 2))
        # Too many packets vs RTS b5.
        cap.step(self.cm_r, cts(3, 1))
        # Pause permission (count zero) unsupported.
        cap.step(self.cm_r, cts(0, 1))
        # Start packet mismatch.
        cap.step(self.cm_r, cts(1, 2))
        self._import(cap)
        codes = {e["event_code"] for e in self.js.events()}
        self.assertIn("cts_bad_count", codes)
        self.assertIn("cts_unsupported", codes)
        self.assertIn("cts_bad_start", codes)
        # None of those bad controls damaged the still-waiting transfer.
        self.assertEqual(len(self.js.transfers()), 1)

    def test_cts_window_cannot_cross_total(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 2))
        # Window 2..3 crosses the total packet count 2 (start equals next).
        cap.step(self.cm_r, cts(2, 2))
        cap.step(self.cm_r, cts(2, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        cap.step(self.dt_s, dt(2, b"\x02\x03"))
        cap.step(self.cm_r, ack(9, 2))
        self._import(cap)
        self.assertIn("cts_bad_window",
                      {e["event_code"] for e in self.js.events()})
        self.assertEqual(self.js.messages()[0]["length_bytes"], 9)

    def test_data_outside_window_fails_and_duplicate_no_slot(self):
        payload = bytes(range(16))
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 1))
        # No CTS yet: data fails immediately.
        cap.step(self.dt_s, dt(1, payload[0:7]))
        # A fresh announcement reuses the failed channel.
        cap.step(self.cm_s, rts(16, 3, 1))
        cap.step(self.cm_r, cts(1, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        cap.step(self.dt_s, dt(1, payload[0:7]))   # duplicate: no slot used
        # Window of size 1 is already consumed; another packet fails.
        cap.step(self.dt_s, dt(2, payload[7:14]))
        self._import(cap)
        codes = [e["event_code"] for e in self.js.events()]
        self.assertEqual(codes.count("dt_outside_window"), 2)
        self.assertIn("duplicate_packet", codes)

    def test_reauthorization_unsupported(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 2))
        cap.step(self.cm_r, cts(2, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        # Re-authorized from already received packet 1.
        cap.step(self.cm_r, cts(2, 1))
        self._import(cap)
        self.assertIn("cts_unsupported",
                      {e["event_code"] for e in self.js.events()})

    def test_same_seq_changed_bytes_fails(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 3))
        cap.step(self.cm_r, cts(3, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        cap.step(self.dt_s, dt(1, b"\x02" * 7))
        self._import(cap)
        tr = self.js.transfers()[0]
        self.assertEqual(tr["state"], "failed")
        self.assertEqual(tr["failure_code"], "dt_seq_conflict")
        detail = self.js.transfer(tr["transfer_id"])
        self.assertEqual(detail["missing_packet_seqs"], [2, 3])

    def test_gap_skip_cannot_assemble(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 3))
        cap.step(self.cm_r, cts(3, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        cap.step(self.dt_s, dt(3, b"\x03" * 7))
        self._import(cap)
        tr = self.js.transfers()[0]
        self.assertEqual(tr["failure_code"], "dt_seq_gap")
        self.assertEqual(self.js.messages(), [])

    def test_orphan_reverse_controls(self):
        cap = CaptureBuilder()
        # CTS with no matching transfer.
        cap.add(self.cm_r, cts(1, 1))
        cap.step(self.cm_r, ack(9, 2))
        cap.step(self.cm_r, abort(0x05))
        self._import(cap)
        self.assertEqual(
            [e["event_code"] for e in self.js.events()].count("orphan_control"), 3)
        self.assertEqual(self.js.transfers(), [])

    def test_unknown_control_byte(self):
        cap = CaptureBuilder()
        cap.add(self.cm_r, cm(0x77))
        self._import(cap)
        self.assertIn("unsupported_control",
                      {e["event_code"] for e in self.js.events()})

    def test_abort_by_either_endpoint(self):
        cap = CaptureBuilder()
        # Receiver aborts.
        cap.add(self.cm_s, rts(9, 2, 1))
        cap.step(self.cm_r, cts(1, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))
        cap.step(self.cm_r, abort(0x06))
        # Sender aborts a second transfer (same direction as the RTS).
        cap.step(self.cm_s, rts(9, 2, 1))
        cap.step(self.cm_s, abort(0x07))
        self._import(cap)
        trs = self.js.transfers()
        self.assertEqual([t["state"] for t in trs], ["aborted", "aborted"])
        self.assertEqual([t["abort_reason"] for t in trs], [6, 7])

    def test_two_receivers_never_cross(self):
        cap = CaptureBuilder()
        payload = b"\x09" * 9
        cm_s2 = cid(6, 0, 0, 0xEC, 0x30, 0x10)
        cm_r2 = cid(6, 0, 0, 0xEC, 0x10, 0x30)
        dt_s2 = cid(6, 0, 0, 0xEB, 0x30, 0x10)
        cap.add(self.cm_s, rts(9, 2, 1))
        cap.add(cm_s2, rts(9, 2, 1), ts=cap.t + 5)
        cap.add(self.cm_r, cts(1, 1), ts=cap.t + 5)
        cap.add(cm_r2, cts(1, 1), ts=cap.t + 5)
        cap.step(self.dt_s, dt(1, payload[0:7]), delta=5)
        cap.step(dt_s2, dt(1, payload[0:7]))
        cap.step(self.cm_r, cts(1, 2))
        cap.step(cm_r2, cts(1, 2))
        cap.step(self.dt_s, dt(2, payload[7:9]))
        cap.step(dt_s2, dt(2, payload[7:9]))
        cap.step(self.cm_r, ack(9, 2))
        cap.step(cm_r2, ack(9, 2))
        self._import(cap)
        self.assertEqual(len(self.js.messages()), 2)
        for msg in self.js.messages():
            self.assertEqual(self.js.message_payload(msg["message_id"]), payload)


class DeadlineTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.js = J1939Store(self.store)
        self.cm_s = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        self.cm_r = cid(6, 0, 0, 0xEC, 0x10, 0x20)
        self.dt_s = cid(6, 0, 0, 0xEB, 0x20, 0x10)
        self.bam_cm = cid(6, 0, 0, 0xEC, 0xFF, 0x10)
        self.bam_dt = cid(6, 0, 0, 0xEB, 0xFF, 0x10)

    def _import(self, cap):
        path = self.tmp / "c.csv"
        path.write_text(cap.text(), encoding="utf-8")
        return self.js.import_capture(path, DATA_GAP, CONTROL_GAP)

    def test_bam_exact_deadline_equality_times_out(self):
        cap = CaptureBuilder()
        cap.add(self.bam_cm, bam(9, 2))
        cap.step(self.bam_dt, dt(1, b"\x01" * 7), delta=10)
        # Frame at packet1_time + DATA_GAP exactly: timeout must fire.
        ordinary = cid(6, 0, 0, 0xFE, 0xEE, 0x40)
        cap.step(ordinary, b"\x00" * 8, delta=DATA_GAP)
        self._import(cap)
        self.assertEqual(self.js.transfers()[0]["state"], "timed_out")

    def test_one_tick_before_deadline_survives(self):
        cap = CaptureBuilder()
        cap.add(self.bam_cm, bam(9, 2))
        cap.step(self.bam_dt, dt(1, b"\x01" * 7))
        cap.step(self.bam_dt, dt(2, b"\x02\x03"), delta=DATA_GAP - 11)
        self._import(cap)
        self.assertEqual(self.js.transfers()[0]["state"], "completed")

    def test_other_bus_does_not_advance_deadline(self):
        t0 = 1000
        cap = CaptureBuilder(start=t0)
        cap.add(self.cm_s, rts(9, 2, 1))                  # deadline = t0+50
        ordinary = cid(6, 0, 0, 0xFE, 0xEE, 0x40)
        # Bus b2 traffic interleaves through the whole wait.  A b2 timestamp
        # never runs the b1 timeout sweep, even though frames are globally
        # non-decreasing.
        cap.add(ordinary, b"\x00" * 8, ts=t0 + 10, bus="b2")
        # b1 CTS lands one tick before the b1 control deadline (t0+49 <= +50).
        cap.add(self.cm_r, cts(1, 1), ts=t0 + CONTROL_GAP - 1)
        cap.add(ordinary, b"\x00" * 8, ts=t0 + 50, bus="b2")
        cap.add(self.dt_s, dt(1, b"\x01" * 7), ts=t0 + 60)
        cap.add(self.cm_r, cts(1, 2), ts=t0 + 70)
        cap.add(self.dt_s, dt(2, b"\x02\x03"), ts=t0 + 80)
        cap.add(self.cm_r, ack(9, 2), ts=t0 + 90)
        cap.add(ordinary, b"\x00" * 8, ts=t0 + 95, bus="b2")
        self._import(cap)
        self.assertEqual(self.js.transfers()[0]["state"], "completed")

    def test_eof_incomplete_recorded(self):
        cap = CaptureBuilder()
        cap.add(self.bam_cm, bam(16, 3))
        cap.step(self.bam_dt, dt(1, b"\x01" * 7))
        self._import(cap)
        tr = self.js.transfers()[0]
        self.assertEqual(tr["state"], "eof_incomplete")
        detail = self.js.transfer(tr["transfer_id"])
        self.assertEqual(detail["missing_packet_seqs"], [2, 3])
        self.assertEqual(self.js.messages(), [])

    def test_missing_ack_flag_only_when_awaiting_ack(self):
        ordinary = cid(6, 0, 0, 0xFE, 0xEE, 0x40)
        # Timeout while still waiting for the first CTS: not an ACK gap.
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 1))
        cap.step(ordinary, b"\x00" * 8, delta=CONTROL_GAP)
        self._import(cap)
        tr = self.js.transfers()[0]
        self.assertEqual(tr["state"], "timed_out")
        self.assertEqual(tr["missing_ack_flag"], 0)

        # Timeout after all packets arrived while awaiting the EOM ACK.
        cap2 = CaptureBuilder()
        cap2.add(self.cm_s, rts(9, 2, 2))
        cap2.step(self.cm_r, cts(2, 1))
        cap2.step(self.dt_s, dt(1, b"\x01" * 7))
        cap2.step(self.dt_s, dt(2, b"\x02\x03"))
        # Last packet armed a control deadline; let an ordinary frame expire it.
        cap2.step(ordinary, b"\x00" * 8, delta=CONTROL_GAP)
        self._import(cap2)
        tr2 = self.js.transfers()[1]
        self.assertEqual(tr2["state"], "timed_out")
        self.assertEqual(tr2["missing_ack_flag"], 1)
        self.assertEqual(self.js.messages(), [])

    def test_duplicate_does_not_refresh_deadline(self):
        cap = CaptureBuilder()
        cap.add(self.bam_cm, bam(9, 2))
        cap.step(self.bam_dt, dt(1, b"\x01" * 7), delta=10)
        # Duplicate inside the original deadline; it must not extend it.
        cap.step(self.bam_dt, dt(1, b"\x01" * 7), delta=20)
        ordinary = cid(6, 0, 0, 0xFE, 0xEE, 0x40)
        # Original deadline was packet1_time + DATA_GAP; the ordinary frame
        # lands after it, so the transfer times out despite the duplicate.
        cap.step(ordinary, b"\x00" * 8,
                 delta=DATA_GAP - 20 + 1)   # total since packet 1 = 101
        self._import(cap)
        self.assertEqual(self.js.transfers()[0]["state"], "timed_out")
        self.assertIn("duplicate_packet",
                      {e["event_code"] for e in self.js.events()})


class ReplacementAndPersistenceTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.js = J1939Store(self.store)
        self.cm_s = cid(6, 0, 0, 0xEC, 0x20, 0x10)
        self.cm_r = cid(6, 0, 0, 0xEC, 0x10, 0x20)
        self.dt_s = cid(6, 0, 0, 0xEB, 0x20, 0x10)

    def _import(self, cap, data_gap=DATA_GAP, control_gap=CONTROL_GAP):
        path = self.tmp / "c.csv"
        path.write_text(cap.text(), encoding="utf-8")
        return self.js.import_capture(path, data_gap, control_gap)

    def test_bad_announcement_leaves_old_transfer_intact(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 2))
        # Illegal announcement (bad packet count) on the same channel: it is
        # only recorded and must not replace the waiting transfer.
        cap.step(self.cm_s, rts(16, 4, 2))
        # A valid CTS afterwards still drives the original transfer.
        cap.step(self.cm_r, cts(2, 1))
        self._import(cap)
        transfers = self.js.transfers()
        self.assertEqual(len(transfers), 1)
        self.assertIn(transfers[0]["state"], ("data", "await_cts",
                                               "eof_incomplete"))
        self.assertIn("announce_rejected",
                      {e["event_code"] for e in self.js.events()})

    def test_legal_replacement_publishes_only_new_transfer(self):
        payload = b"\x07" * 9
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(16, 3, 2))           # never progresses
        cap.step(self.cm_s, rts(9, 2, 1))           # legal replacement
        cap.step(self.cm_r, cts(1, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        cap.step(self.cm_r, cts(1, 2))
        cap.step(self.dt_s, dt(2, payload[7:9]))
        cap.step(self.cm_r, ack(9, 2))
        self._import(cap)
        trs = self.js.transfers()
        self.assertEqual([t["state"] for t in trs], ["replaced", "completed"])
        self.assertEqual(trs[0]["replaced_by_transfer_id"], trs[1]["transfer_id"])
        self.assertEqual(len(self.js.messages()), 1)

    def test_failed_channel_reusable_and_history_kept(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 1))
        cap.step(self.dt_s, dt(1, b"\x01" * 7))     # outside window -> fail
        payload = b"\x09" * 9
        cap.step(self.cm_s, rts(9, 2, 1))
        cap.step(self.cm_r, cts(1, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        cap.step(self.cm_r, cts(1, 2))
        cap.step(self.dt_s, dt(2, payload[7:9]))
        cap.step(self.cm_r, ack(9, 2))
        self._import(cap)
        states = [t["state"] for t in self.js.transfers()]
        self.assertEqual(states, ["failed", "completed"])

    def test_independent_batches_and_gap_validation(self):
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 1))
        with self.assertRaises(GtfsError):
            self._import(cap, data_gap=0)
        with self.assertRaises(GtfsError):
            self._import(cap, control_gap=-5)
        self._import(cap)
        self._import(cap)
        self.assertEqual(len(self.js.batches()), 2)

    def test_trace_interval_validation(self):
        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09"
        cap = CaptureBuilder()
        cap.add(self.cm_s, rts(9, 2, 2))
        cap.step(self.cm_r, cts(2, 1))
        cap.step(self.dt_s, dt(1, payload[0:7]))
        cap.step(self.dt_s, dt(2, payload[7:9]))
        cap.step(self.cm_r, ack(9, 2))
        self._import(cap)
        mid = self.js.messages()[0]["message_id"]
        for bad in ((-1, 5), (5, 5), (5, 4), (0, 10)):
            with self.assertRaises(GtfsError):
                self.js.trace_bytes(mid, *bad)


class CliTests(StoreTestCase):
    def test_full_fixture_cli_exit_codes_and_queries(self):
        db = self.tmp / "cli.db"
        capture = DIST / "j1939-capture.csv"
        if not capture.exists():
            subprocess.run([sys.executable, "-I",
                            str(ROOT / "tools" / "build_j1939_fixtures.py")],
                           check=True, capture_output=True)
        # The demo batch contains deliberate failures: non-zero exit.
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
             "j1939-import", "--capture", str(capture),
             "--data-gap-ns", str(DATA_GAP),
             "--control-gap-ns", str(CONTROL_GAP)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        # A clean BAM-only batch exits zero.
        good = CaptureBuilder()
        payload = bytes(range(9))
        good.add(cid(6, 0, 0, 0xEC, 0xFF, 0x10), bam(9, 2))
        good.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10), dt(1, payload[0:7]))
        good.step(cid(6, 0, 0, 0xEB, 0xFF, 0x10), dt(2, payload[7:9]))
        clean = self.tmp / "clean.csv"
        clean.write_text(good.text(), encoding="utf-8")
        r2 = subprocess.run(
            [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
             "j1939-import", "--capture", str(clean),
             "--data-gap-ns", str(DATA_GAP),
             "--control-gap-ns", str(CONTROL_GAP)],
            capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0)
        # Query commands work in a fresh process.
        r3 = subprocess.run(
            [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
             "j1939-trace", "--message-id", "1", "--start", "6", "--end", "9"],
            capture_output=True, text=True)
        self.assertEqual(r3.returncode, 0)
        self.assertIn("2 frame hit(s)", r3.stdout)

    def test_bad_capture_files_exit_nonzero_without_writing(self):
        db = self.tmp / "bad.db"
        for name in sorted(os.listdir(DIST / "bad")):
            r = subprocess.run(
                [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
                 "j1939-import", "--capture", str(DIST / "bad" / name),
                 "--data-gap-ns", str(DATA_GAP),
                 "--control-gap-ns", str(CONTROL_GAP)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, name)
            self.assertIn("error:", r.stderr, name)
        import sqlite3
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM j1939_batches").fetchone()[0], 0)


class CrashInjectionTests(StoreTestCase):
    def test_hard_exit_mid_write_leaves_no_batch(self):
        capture = DIST / "j1939-capture.csv"
        if not capture.exists():
            subprocess.run([sys.executable, "-I",
                            str(ROOT / "tools" / "build_j1939_fixtures.py")],
                           check=True, capture_output=True)
        db = self.tmp / "crash.db"
        env = dict(os.environ, GTFS_FAULT_J1939_AFTER_FRAMES="15")
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "gtfs-store"), "--db", str(db),
             "j1939-import", "--capture", str(capture),
             "--data-gap-ns", str(DATA_GAP),
             "--control-gap-ns", str(CONTROL_GAP)],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 91)
        # Reopen in a brand new process: nothing from the killed batch.
        js = J1939Store(self.store)
        self.assertEqual(js.batches(), [])
        self.assertEqual(js.messages(), [])
        # A subsequent clean import works and gets its own batch id.
        result = js.import_capture(capture, DATA_GAP, CONTROL_GAP)
        self.assertEqual(result["batch_id"], 1)
        self.assertTrue(js.messages())
