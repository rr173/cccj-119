"""Shared helpers for the test suite. Run everything with:

    python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store import archive  # noqa: E402,F401
from gtfs_store.cli import main as cli_main  # noqa: E402
from gtfs_store.errors import GtfsError  # noqa: E402,F401
from gtfs_store.store import Store  # noqa: E402

DIST = ROOT / "dist"
FIXTURES = ROOT / "fixtures"


def build_all_fixtures() -> None:
    """Generate fixtures by running the builder as a separate, isolated process."""
    subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "build_fixtures.py")],
                   check=True, capture_output=True, cwd=str(ROOT))


def csv_text(header: list[str], rows: list[list[str]], *, lineterminator: str = "\r\n",
             bom: bool = False) -> str:
    import csv

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator=lineterminator, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return ("﻿" if bom else "") + buf.getvalue()


class StoreTestCase(unittest.TestCase):
    """Base class giving each test a fresh temp directory and database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gtfs-test-")
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "store.db"
        self.store = Store(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    # -- convenience ---------------------------------------------------
    def load(self, feed_key: str, edition: str, package: str | Path) -> dict:
        return self.store.load(feed_key, edition, package)

    def expand(self, feed_key: str, edition: str, start: str, end: str) -> dict:
        return self.store.expand(feed_key, edition, start, end)

    def rows(self, feed_key: str, edition: str, date: str, stop: str | None = None) -> list[dict]:
        return self.store.departures(feed_key, edition, date, stop)

    def sql(self, statement: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.store.conn.execute(statement, params).fetchall()

    def make_zip(self, name: str, members: dict[str, str | bytes], *,
                 compression: int = zipfile.ZIP_DEFLATED) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", compression) as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def make_zip_from_dir(self, name: str, src: Path) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in sorted(src.glob("*.txt")):
                zf.write(member, member.name)
        return path

    def rejected(self, feed_key: str, edition: str, package: str | Path) -> dict:
        result = self.load(feed_key, edition, package)
        self.assertEqual(result["status"], "rejected", result)
        self.assertTrue(result["problems"])
        return result

    def categories(self, result: dict) -> set[str]:
        return {p["category"] for p in result["problems"]}

    def cli(self, *argv: str, expect: int = 0) -> dict | str:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["--db", str(self.db_path), *argv])
        self.assertEqual(code, expect, "stdout=%r stderr=%r" % (out.getvalue(), err.getvalue()))
        text = out.getvalue()
        if "--json" in argv:
            return json.loads(text)
        return text
