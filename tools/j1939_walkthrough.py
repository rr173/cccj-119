"""End-to-end rehearsal of the offline J1939 TP reassembly pipeline.

The transcript is produced from real CLI executions against a fresh SQLite
file and walks through, in order:

* importing the hand-written capture (which contains deliberate failures,
  so the command exits non-zero while still persisting every result);
* the BAM and two-round-CTS point-to-point sessions with their per-window
  permissions, state transitions and raw eight-byte controls;
* same-sender/two-receiver interleaving, identical retransmission, changed
  bytes, out-of-window data, wrong/missing ACK, announcement replacement,
  exact-deadline timeouts and cross-bus time isolation;
* byte tracing of single-packet and cross-packet intervals;
* an independent verifier (standard library only, no production imports)
  that rebuilds every payload from the raw capture and checks provenance;
* a hard process kill in the middle of the write transaction, a reopen that
  proves no half-written message exists, and a subsequent clean import.

Usage:  python3 -I tools/j1939_walkthrough.py [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "j1939"
CLI = str(ROOT / "gtfs-store")
DATA_GAP = 100
CONTROL_GAP = 50


def emit(title: str, lines: list[str] | None = None) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    for line in lines or []:
        print(line)


def run_cli(db: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", CLI, "--db", str(db), *argv],
                          capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    args = parser.parse_args()
    capture = DIST / "j1939-capture.csv"
    if not capture.exists():
        subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "build_j1939_fixtures.py")], check=True)

    tmp = tempfile.mkdtemp(prefix="j1939-walkthrough-") if not args.db else None
    db = Path(args.db) if args.db else Path(tmp) / "j1939.db"
    if db.exists():
        db.unlink()

    emit("1. Import the hand-written capture (failures are expected, exit 1)")
    r = run_cli(db, "j1939-import", "--capture", str(capture),
                "--data-gap-ns", str(DATA_GAP), "--control-gap-ns", str(CONTROL_GAP))
    print(r.stdout)
    print("exit code:", r.returncode, "(non-zero because the batch is not all-success)")
    assert r.returncode == 1

    emit("2. Batch ledger and per-state transfer outcomes")
    r = run_cli(db, "j1939-batches")
    print(r.stdout)
    r = run_cli(db, "j1939-transfers")
    print(r.stdout)

    emit("3. BAM session S1: hand-written 15-byte payload, three packets")
    r = run_cli(db, "j1939-message", "--message-id", "1")
    print(r.stdout)

    emit("4. RTS session S2: two CTS rounds, duplicate DT, raw controls/events")
    r = run_cli(db, "j1939-transfer", "--transfer-id", "2")
    print(r.stdout)

    emit("5. Byte tracing: in-packet and cross-packet intervals")
    for start, end in ((0, 7), (6, 9), (7, 15)):
        r = run_cli(db, "j1939-trace", "--message-id", "1",
                    "--start", str(start), "--end", str(end))
        print(r.stdout)

    emit("6. Failure ledger: conflicts, windows, timeouts, aborts, EOF")
    r = run_cli(db, "j1939-events")
    print(r.stdout)

    emit("7. Independent verifier: rebuild every BLOB from raw capture lines")
    r = subprocess.run([sys.executable, "-I",
                        str(ROOT / "tools" / "verify_j1939_output.py"),
                        "--db", str(db)], capture_output=True, text=True)
    print(r.stdout[-2500:])
    assert r.returncode == 0, r.stderr

    emit("8. Hard kill mid-write (GTFS_FAULT_J1939_AFTER_FRAMES), then reopen")
    crash_db = Path(tmp) / "crash.db" if tmp else db.with_name("crash.db")
    env = dict(os.environ, GTFS_FAULT_J1939_AFTER_FRAMES="20")
    r = subprocess.run([sys.executable, "-I", CLI, "--db", str(crash_db),
                        "j1939-import", "--capture", str(capture),
                        "--data-gap-ns", str(DATA_GAP),
                        "--control-gap-ns", str(CONTROL_GAP)],
                       capture_output=True, text=True, env=env)
    print("killed process exit code:", r.returncode)
    assert r.returncode == 91
    wal = Path(str(crash_db) + "-wal")
    print("uncommitted WAL left behind:", wal.exists(),
          "(%d bytes)" % (wal.stat().st_size if wal.exists() else 0))
    # Reopen in a brand new process WITHOUT deleting the WAL: SQLite recovery
    # must roll the uncommitted transaction back, exposing no batch or message.
    check = run_cli(crash_db, "j1939-batches")
    print("after reopen, batches visible:", repr(check.stdout.strip()))
    assert "no j1939 batches" in check.stdout
    r_check = run_cli(crash_db, "j1939-messages")
    assert "no reassembled messages" in r_check.stdout
    # A new, independent import then succeeds on the same database.
    r2 = run_cli(crash_db, "j1939-import", "--capture", str(capture),
                 "--data-gap-ns", str(DATA_GAP),
                 "--control-gap-ns", str(CONTROL_GAP))
    print(r2.stdout.splitlines()[0])
    assert r2.returncode == 1  # the demo batch itself still contains failures
    verify = subprocess.run([sys.executable, "-I",
                             str(ROOT / "tools" / "verify_j1939_output.py"),
                             "--db", str(crash_db)],
                            capture_output=True, text=True)
    print(verify.stdout.splitlines()[-1])
    assert verify.returncode == 0

    emit("9. File-level rejection: a damaged file never becomes a batch")
    bad = DIST / "bad" / "ok-prefix-but-bad-tail.csv"
    r = run_cli(db, "j1939-import", "--capture", str(bad),
                "--data-gap-ns", str(DATA_GAP),
                "--control-gap-ns", str(CONTROL_GAP))
    print(r.stderr.strip())
    print("exit code:", r.returncode)
    assert r.returncode == 1

    print()
    print("J1939 walkthrough complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
