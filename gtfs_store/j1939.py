"""Offline J1939-21 transport protocol reassembly from raw CAN capture CSV.

This module implements one deliberately narrow subset of J1939 transport
(TP.CM / TP.DT, BAM and RTS/CTS/EOM-ACK/Abort).  It never touches hardware,
never interprets application fault codes and never derives diagnostic
messages from the GPS, schedule or anonymization pipelines; its only input
is the handoff CSV (``bus,timestamp_ns,can_id,data_hex``).

Supported subset
----------------
* 29-bit extended identifiers are split into priority(3), reserved/EDP(1),
  data page(1), PF(8), PS(8), source address(8).  The reserved bit must be
  zero; frames with it set are recorded as rejected identifier frames and
  never participate in anything.
* PDU1 (PF < 240): PS is the destination address and the PGN low byte is
  zero.  PDU2 (PF >= 240): PS belongs to the PGN and the destination is 255.
* TP.CM is PGN ``0x00EC00`` and TP.DT is PGN ``0x00EB00``; both are only
  accepted with data page 0.  Every other PGN is registered as an ordinary
  frame and never takes part in reassembly.
* BAM (control byte 0x20) broadcast transfers and peer-to-peer RTS
  (0x10)/CTS (0x11)/EOM-ACK (0x13)/Abort (0xFF) transfers with explicit
  per-window flow control.  Unknown control bytes are reported unsupported.

Channels are keyed by ``(bus, source_address, destination_address)``: a
sender talking to two receivers can never cross packets, and TP.DT frames
carry no target PGN so no parallel PGN channel is ever invented.

Deadlines
---------
Every legal input line first expires active transfers on the *same bus*
whose deadline is ``<=`` the frame timestamp (equality times out), then the
frame is interpreted.  Ordinary frames advance bus time too; traffic on
another bus never does.  BAM announcements and fresh data packets arm a
``data_gap_ns`` deadline; RTS, a fully consumed CTS window and the final
data packet arm a ``control_gap_ns`` deadline.  Retransmitted duplicate
packets never refresh a deadline.

Persistence and atomicity
-------------------------
The whole input is validated before any database write opens.  One
``BEGIN IMMEDIATE`` transaction then stores the batch, every raw frame, the
transfer ledger, controls, packet sources, events and completed messages;
a crash (including the injected ``GTFS_FAULT_J1939_AFTER_FRAMES`` hard
exit) rolls the whole batch back, so a new process can never read a
published message that is missing a byte source.  Every import is an
independent batch; there is no resume cursor.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .errors import GtfsError
from .model import read_csv_bytes

J1939_SCHEMA_VERSION = "1"

INPUT_COLUMNS = ("bus", "timestamp_ns", "can_id", "data_hex")

PGN_TP_CM = 0x00EC00
PGN_TP_DT = 0x00EB00
GLOBAL_ADDRESS = 255

# TP.CM control bytes.
CM_RTS = 0x10
CM_BAM = 0x20
CM_CTS = 0x11
CM_EOM_ACK = 0x13
CM_ABORT = 0xFF

MIN_TP_BYTES = 9
MAX_TP_BYTES = 1785

# Terminal transfer states.
ST_ACTIVE = ("await_cts", "data", "await_ack")
ST_TERMINAL = ("completed", "failed", "aborted", "replaced", "timed_out",
               "eof_incomplete")

_HEX8_RE = re.compile(r"[0-9A-Fa-f]{8}")
_HEX16_RE = re.compile(r"[0-9A-Fa-f]{16}")
_DIGITS_RE = re.compile(r"\d+")


class J1939FileError(GtfsError):
    """File-level failure: the capture cannot be processed as a batch at all."""

    exit_code = 1


# ---------------------------------------------------------------------------
# Identifier decoding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanId:
    raw: int
    priority: int
    reserved: int
    data_page: int
    pf: int
    ps: int
    source_addr: int

    @property
    def pgn(self) -> int:
        """The 18-bit PGN; PDU1 PGNs have their low byte forced to zero."""
        if self.pf < 240:
            return (self.data_page << 16) | (self.pf << 8)
        return (self.data_page << 16) | (self.pf << 8) | self.ps

    @property
    def destination(self) -> int:
        return self.ps if self.pf < 240 else GLOBAL_ADDRESS


def decode_can_id(value: int) -> CanId:
    return CanId(
        raw=value,
        priority=(value >> 26) & 0x07,
        reserved=(value >> 25) & 0x01,
        data_page=(value >> 24) & 0x01,
        pf=(value >> 16) & 0xFF,
        ps=(value >> 8) & 0xFF,
        source_addr=value & 0xFF,
    )


def valid_target_pgn(pgn: int) -> bool:
    """Announcement target PGN rules: 18-bit, PDU1 low byte zero, not a TP PGN."""
    if not 0 <= pgn <= 0x3FFFF:
        return False
    pf = (pgn >> 8) & 0xFF
    if pf < 240 and (pgn & 0xFF) != 0:
        return False
    if pgn in (PGN_TP_CM, PGN_TP_DT):
        return False
    return True


# ---------------------------------------------------------------------------
# Strict input parsing (two passes; nothing is written before pass 1 passes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedFrame:
    frame_seq: int
    source_line: int
    bus: str
    timestamp_ns: int
    can_id_text: str
    can_id: int
    data_hex: str
    data: bytes


def parse_capture(data: bytes) -> Iterator[ParsedFrame]:
    """Validate the handoff CSV and yield frames in file order.

    Any structural or field defect raises :class:`J1939FileError`: the whole
    file is then unusable and the good-looking prefix must not be treated as
    a complete batch.
    """
    try:
        header, rows = read_csv_bytes(data)
    except GtfsError as exc:
        raise J1939FileError("capture file cannot be parsed: %s" % exc) from exc
    if tuple(header) != INPUT_COLUMNS:
        raise J1939FileError(
            "capture header must be exactly %s, got %s"
            % (",".join(INPUT_COLUMNS), ",".join(header) if header else "(empty)"))

    previous_ts: int | None = None
    seq = 0
    for raw, line, field_count in rows:
        if field_count != len(INPUT_COLUMNS):
            raise J1939FileError(
                "line %d: expected %d columns, got %d"
                % (line, len(INPUT_COLUMNS), field_count))
        bus = raw["bus"]
        if not bus:
            raise J1939FileError("line %d: bus must be non-empty" % line)
        ts_text = raw["timestamp_ns"]
        if not _DIGITS_RE.fullmatch(ts_text):
            raise J1939FileError(
                "line %d: timestamp_ns=%r must be a non-negative integer"
                % (line, ts_text))
        timestamp_ns = int(ts_text)
        if previous_ts is not None and timestamp_ns < previous_ts:
            raise J1939FileError(
                "line %d: timestamp_ns=%d goes backwards from %d; the capture "
                "must be non-decreasing" % (line, timestamp_ns, previous_ts))
        can_text = raw["can_id"]
        if not _HEX8_RE.fullmatch(can_text):
            raise J1939FileError(
                "line %d: can_id=%r must be exactly eight hexadecimal digits "
                "(29-bit extended id, leading zeros retained)" % (line, can_text))
        can_id = int(can_text, 16)
        if can_id > 0x1FFFFFFF:
            raise J1939FileError(
                "line %d: can_id=%s exceeds the 29-bit extended identifier range"
                % (line, can_text))
        data_text = raw["data_hex"]
        if not _HEX16_RE.fullmatch(data_text):
            raise J1939FileError(
                "line %d: data_hex=%r must be exactly 16 hexadecimal digits "
                "representing 8 bytes" % (line, data_text))
        payload = bytes.fromhex(data_text)
        seq += 1
        yield ParsedFrame(seq, line, bus, timestamp_ns, can_text, can_id,
                          data_text, payload)
        previous_ts = timestamp_ns


# ---------------------------------------------------------------------------
# Live transfer state machine
# ---------------------------------------------------------------------------

@dataclass
class _Transfer:
    transfer_id: int
    bus: str
    src: int
    dst: int
    mode: str                      # "BAM" or "RTS"
    app_pgn: int
    total_bytes: int
    total_packets: int
    max_cts: int | None
    announce_frame_id: int
    start_ns: int
    state: str = "data"            # BAM starts in data; RTS starts in await_cts
    next_seq: int = 1
    window_remaining: int | None = None
    deadline: int | None = None
    packets: dict[int, bytes] = field(default_factory=dict)
    end_ns: int | None = None


class J1939Store:
    """Reassembly engine and audit reads over one :class:`Store` database."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn
        self.conn.executescript(J1939_DDL)
        stored = self.conn.execute(
            "SELECT value FROM meta WHERE key='j1939_schema_version'").fetchone()
        if stored is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('j1939_schema_version', ?)",
                (J1939_SCHEMA_VERSION,))
        elif stored[0] != J1939_SCHEMA_VERSION:
            raise GtfsError(
                "j1939 schema version %s is stored but this code expects %s"
                % (stored[0], J1939_SCHEMA_VERSION))

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_capture(self, input_path: str | os.PathLike[str],
                       data_gap_ns: int, control_gap_ns: int,
                       raw_bytes: bytes | None = None) -> dict[str, Any]:
        for name, value in (("data_gap_ns", data_gap_ns),
                            ("control_gap_ns", control_gap_ns)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise GtfsError("%s must be a positive integer" % name)
        path = Path(input_path)
        if raw_bytes is None:
            try:
                raw_bytes = path.read_bytes()
            except OSError as exc:
                raise J1939FileError("cannot read capture file %s: %s"
                                     % (path, exc)) from exc
        source_sha = hashlib.sha256(raw_bytes).hexdigest()

        # Pass 1: full structural validation.  Any failure aborts before the
        # write transaction opens, so a damaged prefix can never be committed.
        frame_count = 0
        for _frame in parse_capture(raw_bytes):
            frame_count += 1

        fault_after = _fault_after_frames()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO j1939_batches(source_file, source_sha256, "
                "data_gap_ns, control_gap_ns, total_frames, total_transfers, "
                "completed_transfers, published_messages, problem_events, "
                "all_succeeded, created_at) VALUES(?,?,?,?,?,0,0,0,0,0,?)",
                (os.path.basename(str(path)), source_sha, data_gap_ns,
                 control_gap_ns, frame_count, _utc_now()))
            batch_id = cur.lastrowid
            engine = _Engine(self.conn, batch_id, data_gap_ns, control_gap_ns)
            processed = 0
            # Pass 2: replay the now-proven-valid stream, persisting each raw
            # frame and every state-machine conclusion in source order.
            for frame in parse_capture(raw_bytes):
                engine.handle_frame(frame)
                processed += 1
                if fault_after is not None and processed >= fault_after:
                    # A real process death: no commit, no ROLLBACK, no cleanup.
                    # The uncommitted WAL frames are left behind so reopen has
                    # to recover them; a reader must then see no batch at all.
                    os._exit(91)
            engine.finish_batch()
            self._finalize_batch(batch_id, engine)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.batch(batch_id)

    def _finalize_batch(self, batch_id: int, engine: "_Engine") -> None:
        transfers = engine.transfer_counter
        messages = engine.message_counter
        problems = engine.problem_counter
        completed = self.conn.execute(
            "SELECT COUNT(*) FROM j1939_transfers WHERE batch_id=? "
            "AND state='completed'", (batch_id,)).fetchone()[0]
        self.conn.execute(
            "UPDATE j1939_batches SET total_transfers=?, completed_transfers=?, "
            "published_messages=?, problem_events=?, all_succeeded=? "
            "WHERE batch_id=?",
            (transfers, completed, messages, problems,
             1 if problems == 0 and transfers == completed else 0, batch_id))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def batches(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM j1939_batches ORDER BY batch_id")]

    def batch(self, batch_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM j1939_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise GtfsError("no j1939 batch %d" % batch_id)
        result = dict(row)
        result["state_counts"] = dict(self.conn.execute(
            "SELECT state, COUNT(*) FROM j1939_transfers WHERE batch_id=? "
            "GROUP BY state", (batch_id,)).fetchall())
        result["frame_class_counts"] = dict(self.conn.execute(
            "SELECT frame_class, COUNT(*) FROM j1939_frames WHERE batch_id=? "
            "GROUP BY frame_class", (batch_id,)).fetchall())
        return result

    def messages(self, batch_id: int | None = None,
                 bus: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM j1939_messages WHERE 1=1"
        args: list[Any] = []
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        if bus is not None:
            sql += " AND bus=?"
            args.append(bus)
        sql += " ORDER BY message_id"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        for row in rows:
            row["payload_hex"] = row.pop("payload").hex()
        return rows

    def message(self, message_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM j1939_messages WHERE message_id=?",
            (message_id,)).fetchone()
        if row is None:
            raise GtfsError("no j1939 message %d" % message_id)
        result = dict(row)
        payload = result.pop("payload")
        result["payload_hex"] = payload.hex()
        result["sources"] = [dict(r) for r in self.conn.execute(
            "SELECT p.packet_seq, p.payload_offset, p.frame_offset, p.data_len, "
            "       f.frame_id, f.source_line, f.bus, f.timestamp_ns, "
            "       f.can_id_text, f.data_hex "
            "FROM j1939_packets p JOIN j1939_frames f ON f.frame_id=p.frame_id "
            "WHERE p.transfer_id=? ORDER BY p.packet_seq",
            (row["transfer_id"],))]
        return result

    def message_payload(self, message_id: int) -> bytes:
        row = self.conn.execute(
            "SELECT payload FROM j1939_messages WHERE message_id=?",
            (message_id,)).fetchone()
        if row is None:
            raise GtfsError("no j1939 message %d" % message_id)
        return bytes(row["payload"])

    def transfers(self, batch_id: int | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM j1939_transfers WHERE 1=1"
        args: list[Any] = []
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        if status is not None:
            sql += " AND state=?"
            args.append(status)
        sql += " ORDER BY transfer_id"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def transfer(self, transfer_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM j1939_transfers WHERE transfer_id=?",
            (transfer_id,)).fetchone()
        if row is None:
            raise GtfsError("no j1939 transfer %d" % transfer_id)
        result = dict(row)
        result["events"] = [dict(r) for r in self.conn.execute(
            "SELECT e.event_seq, e.event_code, e.message, e.frame_id, "
            "       f.source_line, f.timestamp_ns, f.can_id_text, f.data_hex "
            "FROM j1939_events e LEFT JOIN j1939_frames f "
            "  ON f.frame_id=e.frame_id "
            "WHERE e.transfer_id=? ORDER BY e.event_seq", (transfer_id,))]
        result["controls"] = [dict(r) for r in self.conn.execute(
            "SELECT c.*, f.source_line, f.timestamp_ns, f.can_id_text, f.data_hex "
            "FROM j1939_controls c JOIN j1939_frames f ON f.frame_id=c.frame_id "
            "WHERE c.transfer_id=? ORDER BY c.control_id", (transfer_id,))]
        result["packets"] = [dict(r) for r in self.conn.execute(
            "SELECT p.packet_seq, p.payload_offset, p.frame_offset, p.data_len, "
            "       p.frame_id, f.source_line, f.timestamp_ns, f.can_id_text, "
            "       f.data_hex "
            "FROM j1939_packets p JOIN j1939_frames f ON f.frame_id=p.frame_id "
            "WHERE p.transfer_id=? ORDER BY p.packet_seq", (transfer_id,))]
        received = {p["packet_seq"] for p in result["packets"]}
        result["missing_packet_seqs"] = [
            seq for seq in range(1, result["total_packets"] + 1)
            if seq not in received]
        result["missing_ack"] = result["state"] in (
            "await_ack", "timed_out", "eof_incomplete", "failed") and bool(
            result["missing_ack_flag"])
        return result

    def events(self, batch_id: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT e.*, f.source_line FROM j1939_events e "
               "LEFT JOIN j1939_frames f ON f.frame_id=e.frame_id WHERE 1=1")
        args: list[Any] = []
        if batch_id is not None:
            sql += " AND e.batch_id=?"
            args.append(batch_id)
        sql += " ORDER BY e.event_seq"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def trace_bytes(self, message_id: int, start: int,
                    end: int) -> dict[str, Any]:
        """Map a left-closed/right-open payload interval back to DT source frames."""
        if not isinstance(start, int) or isinstance(start, bool) \
                or not isinstance(end, int) or isinstance(end, bool):
            raise GtfsError("byte trace bounds must be integers")
        row = self.conn.execute(
            "SELECT message_id, batch_id, length_bytes, transfer_id "
            "FROM j1939_messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise GtfsError("no j1939 message %d" % message_id)
        length = row["length_bytes"]
        if not 0 <= start < end <= length:
            raise GtfsError(
                "byte interval [%d, %d) is invalid for a %d-byte message; "
                "require 0 <= start < end <= length" % (start, end, length))
        hits: list[dict[str, Any]] = []
        for p in self.conn.execute(
                "SELECT p.packet_seq, p.payload_offset, p.frame_offset, "
                "       p.data_len, f.frame_id, f.source_line, f.bus, "
                "       f.timestamp_ns, f.can_id_text, f.data_hex "
                "FROM j1939_packets p JOIN j1939_frames f ON f.frame_id=p.frame_id "
                "WHERE p.transfer_id=? ORDER BY p.packet_seq",
                (row["transfer_id"],)):
            seg_lo = p["payload_offset"]
            seg_effective_end = min(seg_lo + p["data_len"], length)
            if end <= seg_lo or start >= seg_effective_end:
                continue
            hit_lo = max(start, seg_lo)
            hit_hi = min(end, seg_effective_end)
            frame = bytes.fromhex(p["data_hex"])
            frame_lo = p["frame_offset"] + (hit_lo - seg_lo)
            frame_hi = p["frame_offset"] + (hit_hi - seg_lo)
            hits.append({
                "packet_seq": p["packet_seq"],
                "frame_id": p["frame_id"],
                "source_line": p["source_line"],
                "bus": p["bus"],
                "timestamp_ns": p["timestamp_ns"],
                "can_id_text": p["can_id_text"],
                "data_hex": p["data_hex"],
                "payload_start": hit_lo,
                "payload_end": hit_hi,
                "frame_offset_start": frame_lo,
                "frame_offset_end": frame_hi,
                "frame_bytes_hex": frame[frame_lo:frame_hi].hex(),
            })
        return {"message_id": message_id, "start": start, "end": end,
                "length_bytes": length, "hits": hits}


# ---------------------------------------------------------------------------
# Internal state machine engine (one import instance)
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self, conn, batch_id: int, data_gap_ns: int,
                 control_gap_ns: int) -> None:
        self.conn = conn
        self.batch_id = batch_id
        self.data_gap = data_gap_ns
        self.control_gap = control_gap_ns
        self.active: dict[tuple[str, int, int], _Transfer] = {}
        self.event_seq = 0
        self.transfer_counter = 0
        self.message_counter = 0
        self.problem_counter = 0

    # -- ledger helpers -------------------------------------------------

    def _event(self, code: str, message: str, frame_id: int | None,
               transfer: _Transfer | None = None, *, problem: bool = True) -> None:
        self.event_seq += 1
        self.conn.execute(
            "INSERT INTO j1939_events(batch_id, event_seq, transfer_id, "
            "frame_id, event_code, message) VALUES(?,?,?,?,?,?)",
            (self.batch_id, self.event_seq,
             transfer.transfer_id if transfer else None, frame_id, code, message))
        if problem:
            self.problem_counter += 1

    def _insert_frame(self, frame: ParsedFrame, can: CanId | None,
                      frame_class: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO j1939_frames(batch_id, frame_seq, source_line, bus, "
            "timestamp_ns, can_id_text, can_id, priority, reserved_bit, "
            "data_page, pf, ps, source_addr, pgn, destination_addr, frame_class, "
            "data_hex) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, frame.frame_seq, frame.source_line,
             frame.bus, frame.timestamp_ns, frame.can_id_text, frame.can_id,
             can.priority if can else None, can.reserved if can else None,
             can.data_page if can else None, can.pf if can else None,
             can.ps if can else None, can.source_addr if can else None,
             can.pgn if can else None, can.destination if can else None,
             frame_class, frame.data_hex))
        return cur.lastrowid

    def _persist_transfer(self, tr: _Transfer) -> None:
        self.conn.execute(
            "INSERT INTO j1939_transfers(batch_id, bus, src_addr, dst_addr, "
            "mode, app_pgn, total_bytes, total_packets, max_cts, state, next_seq, "
            "window_remaining, deadline_ns, announce_frame_id, start_ns, end_ns, "
            "failure_code, failure_frame_id, abort_reason, missing_ack_flag, "
            "replaced_by_transfer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, tr.bus, tr.src, tr.dst, tr.mode,
             tr.app_pgn, tr.total_bytes, tr.total_packets, tr.max_cts, tr.state,
             tr.next_seq, tr.window_remaining, tr.deadline,
             tr.announce_frame_id, tr.start_ns, tr.end_ns, None, None, None,
             0, None))

    def _sync_transfer(self, tr: _Transfer) -> None:
        self.conn.execute(
            "UPDATE j1939_transfers SET state=?, next_seq=?, window_remaining=?, "
            "deadline_ns=?, end_ns=?, missing_ack_flag=? WHERE transfer_id=?",
            (tr.state, tr.next_seq, tr.window_remaining, tr.deadline, tr.end_ns,
             1 if tr.state == "await_ack" else 0, tr.transfer_id))

    def _fail(self, tr: _Transfer, code: str, message: str,
              frame_id: int) -> None:
        tr.state = "failed"
        tr.end_ns = self._frame_time(frame_id)
        self._event(code, message, frame_id, tr)
        self.conn.execute(
            "UPDATE j1939_transfers SET state='failed', end_ns=?, "
            "failure_code=?, failure_frame_id=?, missing_ack_flag=? "
            "WHERE transfer_id=?",
            (tr.end_ns, code, frame_id,
             1 if tr.mode == "RTS" and tr.next_seq > tr.total_packets else 0,
             tr.transfer_id))
        self.active.pop((tr.bus, tr.src, tr.dst), None)

    def _frame_time(self, frame_id: int) -> int:
        return self.conn.execute(
            "SELECT timestamp_ns FROM j1939_frames WHERE frame_id=?",
            (frame_id,)).fetchone()[0]

    # -- timeout sweep --------------------------------------------------

    def _expire(self, frame: ParsedFrame, frame_id: int) -> None:
        """Timeout active transfers on this bus at deadline <= frame time."""
        due = sorted(
            (tr for key, tr in self.active.items()
             if key[0] == frame.bus and tr.deadline is not None
             and tr.deadline <= frame.timestamp_ns),
            key=lambda tr: (tr.deadline, tr.src, tr.dst))
        for tr in due:
            if (tr.bus, tr.src, tr.dst) not in self.active:
                continue
            awaiting_ack = tr.state == "await_ack"
            waiting = ("awaiting CTS" if tr.state == "await_cts"
                       else "awaiting EOM ACK" if awaiting_ack
                       else "awaiting packet %d" % tr.next_seq)
            tr.state = "timed_out"
            tr.end_ns = frame.timestamp_ns
            self._event(
                "timed_out",
                "transfer timed out at timestamp_ns=%d (deadline=%d, %s)"
                % (frame.timestamp_ns, tr.deadline, waiting),
                frame_id, tr)
            # The ACK is only "missing" if all packets had already arrived and
            # the transfer was actually waiting on the confirmation.
            self.conn.execute(
                "UPDATE j1939_transfers SET state='timed_out', end_ns=?, "
                "deadline_ns=?, missing_ack_flag=? WHERE transfer_id=?",
                (tr.end_ns, tr.deadline,
                 1 if awaiting_ack else 0, tr.transfer_id))
            self.active.pop((tr.bus, tr.src, tr.dst), None)

    # -- frame dispatch -------------------------------------------------

    def handle_frame(self, frame: ParsedFrame) -> None:
        can = decode_can_id(frame.can_id)
        if can.reserved != 0:
            frame_id = self._insert_frame(frame, can, "rejected")
            self._event(
                "identifier_rejected",
                "reserved (EDP) bit is set in can_id=%s; frame takes no part "
                "in any transfer" % frame.can_id_text, frame_id)
            return
        if can.data_page == 0 and can.pgn == PGN_TP_CM:
            frame_id = self._insert_frame(frame, can, "tp_cm")
            self._expire(frame, frame_id)
            self._handle_cm(frame, frame_id, can)
            return
        if can.data_page == 0 and can.pgn == PGN_TP_DT:
            frame_id = self._insert_frame(frame, can, "tp_dt")
            self._expire(frame, frame_id)
            self._handle_dt(frame, frame_id, can)
            return
        # Any other PGN (including data-page-1 frames whose number collides
        # with the TP PGNs) is an ordinary frame; it still advances bus time.
        frame_id = self._insert_frame(frame, can, "ordinary")
        self._expire(frame, frame_id)

    # -- announcements --------------------------------------------------

    def _validate_announcement(self, data: bytes, can: CanId,
                               control: int) -> tuple[dict[str, int] | None, str | None]:
        size = data[1] | (data[2] << 8)
        packets = data[3]
        max_cts = data[4]
        target_pgn = data[5] | (data[6] << 8) | (data[7] << 16)
        if not MIN_TP_BYTES <= size <= MAX_TP_BYTES:
            return None, ("announced total size %d is outside the supported "
                          "%d..%d byte range" % (size, MIN_TP_BYTES, MAX_TP_BYTES))
        expected_packets = (size + 6) // 7
        if packets != expected_packets:
            return None, ("announced packet count %d does not match ceil(%d/7)=%d"
                          % (packets, size, expected_packets))
        if not valid_target_pgn(target_pgn):
            return None, ("target application PGN 0x%05X is invalid (must be 18 "
                          "bit, PDU1 low byte zero, and not TP.CM/TP.DT)"
                          % target_pgn)
        if control == CM_BAM:
            if can.destination != GLOBAL_ADDRESS or max_cts != GLOBAL_ADDRESS:
                return None, ("BAM requires destination address 255 and b5=255, "
                              "got destination=%d b5=%d"
                              % (can.destination, max_cts))
        else:  # RTS
            if can.destination == GLOBAL_ADDRESS:
                return None, "RTS cannot address the global address 255"
            if not 1 <= max_cts <= packets:
                return None, ("RTS b5 max CTS packets=%d must be in 1..%d"
                              % (max_cts, packets))
        return {"size": size, "packets": packets, "max_cts": max_cts,
                "target_pgn": target_pgn}, None

    def _start_transfer(self, frame: ParsedFrame, frame_id: int, can: CanId,
                        mode: str, info: dict[str, int]) -> _Transfer:
        key = (frame.bus, can.source_addr, can.destination)
        old = self.active.get(key)
        if old is not None:
            # A legal new announcement replaces the unfinished channel; the
            # old transfer is never published.
            old.state = "replaced"
            old.end_ns = frame.timestamp_ns
            self._event(
                "replaced",
                "active transfer replaced by a new %s announcement before it "
                "finished; no message is published for it" % mode,
                frame_id, old)
        self.transfer_counter += 1
        if mode == "BAM":
            state, window, deadline = "data", None, frame.timestamp_ns + self.data_gap
        else:
            state, window, deadline = ("await_cts", 0,
                                       frame.timestamp_ns + self.control_gap)
        tr = _Transfer(
            transfer_id=-1, bus=frame.bus,
            src=can.source_addr, dst=can.destination, mode=mode,
            app_pgn=info["target_pgn"], total_bytes=info["size"],
            total_packets=info["packets"],
            max_cts=info["max_cts"] if mode == "RTS" else None,
            announce_frame_id=frame_id, start_ns=frame.timestamp_ns,
            state=state, window_remaining=window, deadline=deadline)
        self._persist_transfer(tr)
        tr.transfer_id = self.conn.execute(
            "SELECT last_insert_rowid()").fetchone()[0]
        self.active[key] = tr
        self._event("transfer_started",
                    "%s transfer started: app_pgn=0x%05X %d byte(s) in %d packet(s)"
                    % (mode, tr.app_pgn, tr.total_bytes, tr.total_packets),
                    frame_id, tr, problem=False)
        self._sync_transfer(tr)
        if old is not None:
            self.conn.execute(
                "UPDATE j1939_transfers SET state='replaced', end_ns=?, "
                "replaced_by_transfer_id=? WHERE transfer_id=?",
                (old.end_ns, tr.transfer_id, old.transfer_id))
        return tr

    # -- TP.CM ----------------------------------------------------------

    def _handle_cm(self, frame: ParsedFrame, frame_id: int, can: CanId) -> None:
        data = frame.data
        control = data[0]
        if control in (CM_BAM, CM_RTS):
            info, problem = self._validate_announcement(
                data, can, control)
            mode = "BAM" if control == CM_BAM else "RTS"
            if info is None:
                # A bad announcement is only recorded; it must not disturb an
                # existing transfer on the channel.
                self._insert_control(frame_id, None, mode, data)
                self._event("announce_rejected",
                            "%s announcement rejected: %s" % (mode, problem),
                            frame_id)
                return
            tr = self._start_transfer(
                frame, frame_id, can, mode, info)
            self._insert_control(frame_id, tr.transfer_id, mode, data)
            return

        if control == CM_CTS:
            self._handle_cts(frame, frame_id, can, data)
            return
        if control == CM_EOM_ACK:
            self._handle_ack(frame, frame_id, can, data)
            return
        if control == CM_ABORT:
            self._handle_abort(frame, frame_id, can, data)
            return
        self._insert_control(frame_id, None, "UNKNOWN", data)
        tr = self._find_reverse(frame.bus, can)
        self._event(
            "unsupported_control",
            "unknown TP.CM control byte 0x%02X; meaning is not inferred from "
            "the remaining bytes" % control, frame_id, tr)

    def _find_reverse(self, bus: str, can: CanId) -> _Transfer | None:
        """Find the transfer whose sender/receiver are swapped relative to frame."""
        return self.active.get((bus, can.ps, can.source_addr))

    def _find_direct(self, bus: str, can: CanId) -> _Transfer | None:
        return self.active.get((bus, can.source_addr, can.ps))

    def _handle_cts(self, frame: ParsedFrame, frame_id: int, can: CanId,
                    data: bytes) -> None:
        count = data[1]
        start = data[2]
        target_pgn = data[5] | (data[6] << 8) | (data[7] << 16)
        tr = self._find_reverse(frame.bus, can)
        if tr is None or tr.app_pgn != target_pgn:
            self._insert_control(frame_id,
                                 tr.transfer_id if tr else None, "CTS", data)
            self._event(
                "orphan_control",
                "CTS for app_pgn=0x%05X does not correspond to an active "
                "transfer in the reverse direction" % target_pgn, frame_id, tr)
            return
        self._insert_control(frame_id, tr.transfer_id, "CTS", data)
        if data[3] != GLOBAL_ADDRESS or data[4] != GLOBAL_ADDRESS:
            self._event("cts_bad_fields",
                        "CTS b4 and b5 must both be 255", frame_id, tr)
            return
        if count == 0:
            self._event("cts_unsupported",
                        "pause permission (CTS packet count 0) is not supported "
                        "by this subset", frame_id, tr)
            return
        if start < tr.next_seq:
            self._event("cts_unsupported",
                        "CTS re-authorizes already received packet %d while the "
                        "next expected is %d; re-authorization is not supported"
                        % (start, tr.next_seq), frame_id, tr)
            return
        if tr.state != "await_cts":
            self._event("cts_not_awaiting",
                        "CTS received while transfer is in state %s, not waiting "
                        "for a permission window" % tr.state, frame_id, tr)
            return
        if not 1 <= count <= tr.max_cts:
            self._event("cts_bad_count",
                        "CTS permits %d packet(s) but RTS b5 allows at most %d"
                        % (count, tr.max_cts), frame_id, tr)
            return
        if start + count - 1 > tr.total_packets:
            self._event("cts_bad_window",
                        "CTS window %d..%d crosses the total packet count %d"
                        % (start, start + count - 1, tr.total_packets),
                        frame_id, tr)
            return
        if start != tr.next_seq:
            self._event("cts_bad_start",
                        "CTS starts at packet %d but the next expected packet "
                        "is %d" % (start, tr.next_seq), frame_id, tr)
            return
        tr.state = "data"
        tr.window_remaining = count
        tr.deadline = frame.timestamp_ns + self.data_gap
        self._event("cts_accepted",
                    "CTS accepted: packets %d..%d permitted"
                    % (start, start + count - 1), frame_id, tr, problem=False)
        self._sync_transfer(tr)

    def _handle_ack(self, frame: ParsedFrame, frame_id: int, can: CanId,
                    data: bytes) -> None:
        ack_bytes = data[1] | (data[2] << 8)
        ack_packets = data[3]
        target_pgn = data[5] | (data[6] << 8) | (data[7] << 16)
        tr = self._find_reverse(frame.bus, can)
        if tr is None or tr.app_pgn != target_pgn:
            self._insert_control(frame_id,
                                 tr.transfer_id if tr else None, "EOM_ACK", data)
            self._event(
                "orphan_control",
                "EOM ACK for app_pgn=0x%05X does not correspond to an active "
                "transfer in the reverse direction" % target_pgn,
                frame_id, tr)
            return
        self._insert_control(frame_id, tr.transfer_id, "EOM_ACK", data)
        if data[4] != GLOBAL_ADDRESS:
            self._event("ack_bad", "EOM ACK b5 must be 255", frame_id, tr)
            return
        if tr.state != "await_ack":
            self._event("ack_not_awaiting",
                        "EOM ACK received while transfer is in state %s; an early "
                        "confirmation cannot publish the message" % tr.state,
                        frame_id, tr)
            return
        if ack_bytes != tr.total_bytes or ack_packets != tr.total_packets:
            self._event(
                "ack_bad",
                "EOM ACK counts %d byte(s)/%d packet(s) do not match the "
                "announcement %d/%d"
                % (ack_bytes, ack_packets, tr.total_bytes, tr.total_packets),
                frame_id, tr)
            return
        self._complete(tr, frame_id, frame_id)

    def _handle_abort(self, frame: ParsedFrame, frame_id: int, can: CanId,
                      data: bytes) -> None:
        reason = data[1]
        target_pgn = data[5] | (data[6] << 8) | (data[7] << 16)
        if data[2] != GLOBAL_ADDRESS or data[3] != GLOBAL_ADDRESS \
                or data[4] != GLOBAL_ADDRESS:
            self._insert_control(frame_id, None, "ABORT", data)
            self._event("abort_rejected",
                        "Abort requires b3=b4=b5=255", frame_id)
            return
        # Either endpoint may abort: try the sender direction first, then the
        # reversed receiver direction.
        tr = self._find_direct(frame.bus, can) or self._find_reverse(frame.bus, can)
        if tr is None or tr.app_pgn != target_pgn:
            self._insert_control(frame_id,
                                 tr.transfer_id if tr else None, "ABORT", data)
            self._event(
                "orphan_control",
                "Abort for app_pgn=0x%05X has no matching active transfer"
                % target_pgn, frame_id, tr)
            return
        self._insert_control(frame_id, tr.transfer_id, "ABORT", data)
        tr.state = "aborted"
        tr.end_ns = frame.timestamp_ns
        self._event("abort_received",
                    "transfer aborted by %s endpoint; reason code 0x%02X saved "
                    "verbatim" % ("sending" if can.source_addr == tr.src
                                  else "receiving", reason),
                    frame_id, tr)
        self.conn.execute(
            "UPDATE j1939_transfers SET state='aborted', end_ns=?, "
            "abort_reason=?, deadline_ns=NULL WHERE transfer_id=?",
            (tr.end_ns, reason, tr.transfer_id))
        self.active.pop((tr.bus, tr.src, tr.dst), None)

    def _insert_control(self, frame_id: int, transfer_id: int | None,
                        control_type: str, data: bytes) -> None:
        target_pgn = data[5] | (data[6] << 8) | (data[7] << 16)
        announce_bytes = data[1] | (data[2] << 8)
        # Columns are type-specific; bytes belonging to another control type's
        # layout are never reinterpreted and stay NULL.
        if control_type in ("BAM", "RTS"):
            size, packets, max_cts = announce_bytes, data[3], data[4]
            cts_count = cts_start = ack_bytes = ack_packets = abort_reason = None
        elif control_type == "CTS":
            size = packets = max_cts = ack_bytes = ack_packets = abort_reason = None
            cts_count, cts_start = data[1], data[2]
        elif control_type == "EOM_ACK":
            size = packets = max_cts = cts_count = cts_start = abort_reason = None
            ack_bytes, ack_packets = announce_bytes, data[3]
        elif control_type == "ABORT":
            size = packets = max_cts = cts_count = cts_start = None
            ack_bytes = ack_packets = None
            abort_reason = data[1]
        else:  # UNKNOWN: only the target PGN slot is decoded, nothing inferred
            size = packets = max_cts = cts_count = cts_start = None
            ack_bytes = ack_packets = abort_reason = None
        self.conn.execute(
            "INSERT INTO j1939_controls(batch_id, transfer_id, frame_id, "
            "control_type, target_pgn, total_bytes, total_packets, max_cts, "
            "cts_count, cts_start, ack_bytes, ack_packets, abort_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, transfer_id, frame_id, control_type,
             target_pgn, size, packets, max_cts, cts_count, cts_start,
             ack_bytes, ack_packets, abort_reason))

    # -- TP.DT ----------------------------------------------------------

    def _handle_dt(self, frame: ParsedFrame, frame_id: int, can: CanId) -> None:
        tr = self.active.get((frame.bus, can.source_addr, can.ps))
        if tr is None:
            self._event(
                "dt_without_transfer",
                "TP.DT from source %d to %d has no active transfer channel"
                % (can.source_addr, can.ps), frame_id)
            return
        seq = frame.data[0]
        chunk = frame.data[1:8]
        if not 1 <= seq <= tr.total_packets:
            self._fail(tr, "dt_seq_out_of_range",
                       "packet number %d is outside 1..%d"
                       % (seq, tr.total_packets), frame_id)
            return
        if seq == tr.next_seq:
            if tr.mode == "RTS" and (tr.state != "data"
                                     or not tr.window_remaining):
                self._fail(tr, "dt_outside_window",
                           "packet %d arrived outside an active CTS window "
                           "(state=%s)" % (seq, tr.state), frame_id)
                return
            self._accept_packet(tr, frame_id, seq, chunk)
            return
        if seq < tr.next_seq:
            previous = tr.packets.get(seq)
            if previous is not None and previous == chunk:
                # Identical retransmission: keep the association, do not
                # reassemble twice, do not consume a CTS slot or a deadline.
                self._event(
                    "duplicate_packet",
                    "packet %d retransmitted with identical 7 bytes; counted "
                    "once, no deadline refresh" % seq, frame_id, tr,
                    problem=False)
                return
            self._fail(tr, "dt_seq_conflict",
                       "retransmitted packet %d carries different bytes; the "
                       "transfer cannot be trusted" % seq, frame_id)
            return
        self._fail(tr, "dt_seq_gap",
                   "packet %d arrived while packet %d is still missing; gaps "
                   "cannot be skipped" % (seq, tr.next_seq), frame_id)

    def _accept_packet(self, tr: _Transfer, frame_id: int, seq: int,
                       chunk: bytes) -> None:
        tr.packets[seq] = chunk
        offset = (seq - 1) * 7
        self.conn.execute(
            "INSERT INTO j1939_packets(batch_id, transfer_id, packet_seq, "
            "frame_id, payload_offset, frame_offset, data_len) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.batch_id, tr.transfer_id, seq, frame_id, offset, 1, 7))
        tr.next_seq = seq + 1
        if tr.mode == "RTS":
            tr.window_remaining -= 1
        tr.deadline = self._frame_time(frame_id) + self.data_gap
        self._event("packet_received", "packet %d accepted (%d byte(s))"
                    % (seq, len(chunk)), frame_id, tr, problem=False)
        if tr.next_seq > tr.total_packets:
            if tr.mode == "BAM":
                self._complete(tr, frame_id, None)
                return
            # All packets are in for a peer transfer: the receiver's EOM ACK
            # is still mandatory before anything is published.
            tr.state = "await_ack"
            tr.window_remaining = 0
            tr.deadline = self._frame_time(frame_id) + self.control_gap
            self._event("awaiting_ack",
                        "all %d packet(s) received; waiting for EOM ACK"
                        % tr.total_packets, frame_id, tr, problem=False)
            self._sync_transfer(tr)
            return
        if tr.mode == "RTS" and tr.window_remaining == 0:
            # Window fully consumed; the next CTS is now required.
            tr.state = "await_cts"
            tr.deadline = self._frame_time(frame_id) + self.control_gap
            self._event("window_complete",
                        "CTS window consumed; waiting for the next permission",
                        frame_id, tr, problem=False)
        self._sync_transfer(tr)

    # -- completion -----------------------------------------------------

    def _complete(self, tr: _Transfer, frame_id: int,
                  ack_frame_id: int | None) -> None:
        ordered = b"".join(tr.packets[i] for i in
                           sorted(tr.packets))[:tr.total_bytes]
        if len(ordered) != tr.total_bytes:
            # Defensive: the state machine only completes when every packet
            # is present, so this can only fire on an internal inconsistency.
            self._fail(tr, "assembly_error",
                       "assembled payload has %d byte(s), expected %d"
                       % (len(ordered), tr.total_bytes), frame_id)
            return
        tr.state = "completed"
        tr.end_ns = self._frame_time(frame_id)
        self.message_counter += 1
        cur = self.conn.execute(
            "INSERT INTO j1939_messages(batch_id, transfer_id, bus, src_addr, "
            "dst_addr, mode, app_pgn, start_ns, end_ns, length_bytes, payload, "
            "announce_frame_id, ack_frame_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, tr.transfer_id, tr.bus, tr.src, tr.dst, tr.mode,
             tr.app_pgn, tr.start_ns, tr.end_ns, tr.total_bytes, ordered,
             tr.announce_frame_id, ack_frame_id))
        message_id = cur.lastrowid
        self._event("message_completed",
                    "message published: %d application byte(s) from %d packet(s)"
                    % (tr.total_bytes, tr.total_packets),
                    frame_id, tr, problem=False)
        self.conn.execute(
            "UPDATE j1939_transfers SET state='completed', end_ns=?, "
            "deadline_ns=NULL, missing_ack_flag=0 WHERE transfer_id=?",
            (tr.end_ns, tr.transfer_id))
        self.active.pop((tr.bus, tr.src, tr.dst), None)

    # -- end of file ----------------------------------------------------

    def finish_batch(self) -> None:
        for tr in list(self.active.values()):
            tr.state = "eof_incomplete"
            tr.end_ns = self.conn.execute(
                "SELECT MAX(timestamp_ns) FROM j1939_frames WHERE batch_id=?",
                (self.batch_id,)).fetchone()[0]
            missing = [seq for seq in range(1, tr.total_packets + 1)
                       if seq not in tr.packets]
            detail = ("missing packet(s) %s" % missing if missing
                      else "all packets present but no EOM ACK was received")
            self._event(
                "eof_incomplete",
                "file ended before the transfer finished: %s" % detail,
                None, tr)
            self.conn.execute(
                "UPDATE j1939_transfers SET state='eof_incomplete', end_ns=?, "
                "deadline_ns=NULL, missing_ack_flag=? WHERE transfer_id=?",
                (tr.end_ns, 1 if not missing and tr.mode == "RTS" else 0,
                 tr.transfer_id))
        self.active.clear()


def _fault_after_frames() -> int | None:
    raw = os.environ.get("GTFS_FAULT_J1939_AFTER_FRAMES")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise GtfsError("GTFS_FAULT_J1939_AFTER_FRAMES must be an integer") from exc
    return value if value > 0 else None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

J1939_DDL = """
CREATE TABLE IF NOT EXISTS j1939_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    data_gap_ns INTEGER NOT NULL CHECK (data_gap_ns > 0),
    control_gap_ns INTEGER NOT NULL CHECK (control_gap_ns > 0),
    total_frames INTEGER NOT NULL,
    total_transfers INTEGER NOT NULL,
    completed_transfers INTEGER NOT NULL,
    published_messages INTEGER NOT NULL,
    problem_events INTEGER NOT NULL,
    all_succeeded INTEGER NOT NULL CHECK (all_succeeded IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS j1939_frames (
    frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    frame_seq INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    bus TEXT NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    can_id_text TEXT NOT NULL,
    can_id INTEGER NOT NULL,
    priority INTEGER,
    reserved_bit INTEGER,
    data_page INTEGER,
    pf INTEGER,
    ps INTEGER,
    source_addr INTEGER,
    pgn INTEGER,
    destination_addr INTEGER,
    frame_class TEXT NOT NULL CHECK (frame_class IN
        ('ordinary', 'tp_cm', 'tp_dt', 'rejected')),
    data_hex TEXT NOT NULL,
    UNIQUE (batch_id, frame_seq)
);

CREATE INDEX IF NOT EXISTS idx_j1939_frames_bus
    ON j1939_frames (batch_id, bus, timestamp_ns);

CREATE TABLE IF NOT EXISTS j1939_transfers (
    transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    bus TEXT NOT NULL,
    src_addr INTEGER NOT NULL,
    dst_addr INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('BAM', 'RTS')),
    app_pgn INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    total_packets INTEGER NOT NULL,
    max_cts INTEGER,
    state TEXT NOT NULL CHECK (state IN
        ('await_cts', 'data', 'await_ack', 'completed', 'failed', 'aborted',
         'replaced', 'timed_out', 'eof_incomplete')),
    next_seq INTEGER NOT NULL,
    window_remaining INTEGER,
    deadline_ns INTEGER,
    announce_frame_id INTEGER NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER,
    failure_code TEXT,
    failure_frame_id INTEGER,
    abort_reason INTEGER,
    missing_ack_flag INTEGER NOT NULL DEFAULT 0,
    replaced_by_transfer_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_j1939_transfers_batch
    ON j1939_transfers (batch_id, state);

CREATE TABLE IF NOT EXISTS j1939_controls (
    control_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    transfer_id INTEGER,
    frame_id INTEGER NOT NULL,
    control_type TEXT NOT NULL CHECK (control_type IN
        ('BAM', 'RTS', 'CTS', 'EOM_ACK', 'ABORT', 'UNKNOWN')),
    target_pgn INTEGER,
    total_bytes INTEGER,
    total_packets INTEGER,
    max_cts INTEGER,
    cts_count INTEGER,
    cts_start INTEGER,
    ack_bytes INTEGER,
    ack_packets INTEGER,
    abort_reason INTEGER
);

CREATE INDEX IF NOT EXISTS j1939_controls_transfer
    ON j1939_controls (transfer_id);

CREATE TABLE IF NOT EXISTS j1939_packets (
    batch_id INTEGER NOT NULL,
    transfer_id INTEGER NOT NULL,
    packet_seq INTEGER NOT NULL,
    frame_id INTEGER NOT NULL,
    payload_offset INTEGER NOT NULL,
    frame_offset INTEGER NOT NULL,
    data_len INTEGER NOT NULL,
    PRIMARY KEY (transfer_id, packet_seq)
);

CREATE INDEX IF NOT EXISTS idx_j1939_packets_frame
    ON j1939_packets (frame_id);

CREATE TABLE IF NOT EXISTS j1939_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    transfer_id INTEGER NOT NULL UNIQUE,
    bus TEXT NOT NULL,
    src_addr INTEGER NOT NULL,
    dst_addr INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('BAM', 'RTS')),
    app_pgn INTEGER NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    length_bytes INTEGER NOT NULL,
    payload BLOB NOT NULL,
    announce_frame_id INTEGER NOT NULL,
    ack_frame_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_j1939_messages_batch
    ON j1939_messages (batch_id, bus);

CREATE TABLE IF NOT EXISTS j1939_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    transfer_id INTEGER,
    frame_id INTEGER,
    event_code TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_j1939_events_transfer
    ON j1939_events (transfer_id);
CREATE INDEX IF NOT EXISTS idx_j1939_events_batch
    ON j1939_events (batch_id, event_seq);
"""
