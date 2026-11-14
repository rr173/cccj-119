"""Exceptions used by the package."""

from __future__ import annotations

from typing import Any


class GtfsError(Exception):
    """Generic, user-facing error with an exit code."""

    exit_code = 2


class RejectError(GtfsError):
    """A single package validation problem (collectable)."""

    def __init__(self, category: str, message: str, *, file_name: str | None = None,
                 line_number: int | None = None, field: str | None = None,
                 record_id: str | None = None, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.file_name = file_name
        self.line_number = line_number
        self.field = field
        self.record_id = record_id
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "file": self.file_name,
            "line": self.line_number,
            "field": self.field,
            "record_id": self.record_id,
            "context": self.context,
        }

    def describe(self) -> str:
        where = self.file_name or "?"
        if self.line_number is not None:
            where += " line %d" % self.line_number
        bits = [where, self.category]
        if self.field:
            bits.append("field=%s" % self.field)
        if self.record_id:
            bits.append("record=%s" % self.record_id)
        return "%s: %s" % (" | ".join(bits), self.message)
