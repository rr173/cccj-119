"""ZIP intake: member allow-list, path safety, size limits, duplicate names."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import RejectError

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024        # compressed package
MAX_MEMBER_BYTES = 64 * 1024 * 1024          # one uncompressed member
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024   # all members together
MAX_MEMBERS = 10000

ALLOWED_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "frequencies.txt",
)
REQUIRED_FILES = ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt")

_SUPPORTED_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


@dataclass
class PackageMember:
    name: str
    data: bytes
    size: int


@dataclass
class PackageContents:
    members: dict[str, bytes] = field(default_factory=dict)
    names_by_sha: dict[str, str] = field(default_factory=dict)
    archive_size: int = 0

    def get(self, name: str) -> bytes | None:
        return self.members.get(name)


def _unsafe_reason(name: str) -> str | None:
    if name == "":
        return "empty member name"
    if "\x00" in name:
        return "member name contains NUL"
    if "\\" in name:
        return "member name contains a backslash"
    if name.startswith("/"):
        return "member name is absolute"
    # Reject Windows drive letters and any ':' as a drive/ADS marker.
    if len(name) >= 2 and name[1] == ":":
        return "member name contains a drive letter"
    path = PurePosixPath(name)
    if path.is_absolute():
        return "member name is absolute"
    for part in path.parts:
        if part in ("..", "."):
            return "member path escapes the package root"
    if len(PurePosixPath(name).parts) != 1:
        return "member is inside a directory; only top-level GTFS files are accepted"
    return None


def read_package(path: str | Path, raw_archive: bytes | None = None) -> PackageContents:
    """Validate and read an archive; raise RejectError on the first hard problem.

    Every check here is structural: the archive as a whole is refused, never
    partially ingested.
    """
    path = Path(path)
    if raw_archive is not None:
        archive_size = len(raw_archive)
    else:
        try:
            archive_size = path.stat().st_size
        except OSError as exc:
            raise RejectError("archive", "cannot read package: %s" % exc,
                              file_name=path.name) from exc
    if archive_size > MAX_ARCHIVE_BYTES:
        raise RejectError(
            "size",
            "archive is %d bytes, limit is %d" % (archive_size, MAX_ARCHIVE_BYTES),
            file_name=path.name,
        )
    if archive_size == 0:
        raise RejectError("archive", "archive is empty", file_name=path.name)

    source: io.BufferedReader | io.BytesIO
    if raw_archive is not None:
        source = io.BytesIO(raw_archive)
    else:
        source = open(path, "rb")  # noqa: SIM115 - closed by the with block below
    try:
        with source:
            try:
                zf = zipfile.ZipFile(source)
            except (zipfile.BadZipFile, OSError) as exc:
                raise RejectError("archive", "not a readable ZIP archive: %s" % exc,
                                  file_name=path.name) from exc
            with zf:
                return _read_members(zf, path.name)
    except RejectError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise RejectError("archive", "cannot read ZIP member: %s" % exc, file_name=path.name) from exc


def _read_members(zf: zipfile.ZipFile, archive_name: str) -> PackageContents:
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise RejectError("size", "archive has %d members, limit is %d" % (len(infos), MAX_MEMBERS),
                          file_name=archive_name)

    seen_exact: set[str] = set()
    seen_folded: dict[str, str] = {}
    member_infos: dict[str, zipfile.ZipInfo] = {}
    total = 0

    for info in infos:
        name = info.filename
        if info.is_dir():
            continue  # directory entries carry no data
        reason = _unsafe_reason(name)
        if reason:
            raise RejectError("unsafe_path", "%s: %r" % (reason, name), file_name=archive_name)
        if name in seen_exact:
            raise RejectError("duplicate_member", "duplicate member %r" % name,
                              file_name=archive_name, record_id=name)
        folded = name.casefold()
        if folded in seen_folded and seen_folded[folded] != name:
            raise RejectError(
                "duplicate_member",
                "members %r and %r differ only by case" % (seen_folded[folded], name),
                file_name=archive_name,
            )
        seen_exact.add(name)
        seen_folded[folded] = name
        if name not in ALLOWED_FILES:
            raise RejectError(
                "unsupported_member",
                "member %r is outside the accepted GTFS file set (%s)"
                % (name, ", ".join(ALLOWED_FILES)),
                file_name=archive_name,
            )
        if info.flag_bits & 0x1:
            raise RejectError("unsupported_member", "encrypted member %r" % name,
                              file_name=archive_name, record_id=name)
        if info.compress_type not in _SUPPORTED_METHODS:
            raise RejectError(
                "unsupported_member",
                "member %r uses unsupported compression method %d" % (name, info.compress_type),
                file_name=archive_name, record_id=name,
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise RejectError(
                "size",
                "member %r is %d bytes uncompressed, limit is %d"
                % (name, info.file_size, MAX_MEMBER_BYTES),
                file_name=archive_name, record_id=name,
            )
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise RejectError(
                "size",
                "uncompressed members exceed the %d byte total limit" % MAX_TOTAL_UNCOMPRESSED,
                file_name=archive_name,
            )
        member_infos[name] = info

    contents = PackageContents(archive_size=len(infos))
    for name, info in member_infos.items():
        try:
            with zf.open(info, "r") as fh:
                data = fh.read(MAX_MEMBER_BYTES + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise RejectError("archive", "cannot read member %r: %s" % (name, exc),
                              file_name=archive_name, record_id=name) from exc
        if len(data) > MAX_MEMBER_BYTES:
            raise RejectError("size", "member %r exceeds its uncompressed limit" % name,
                              file_name=archive_name, record_id=name)
        contents.members[name] = data
    return contents
