"""Locate the official GTFS Realtime protocol-buffer bindings.

The realtime channel parses ``FeedMessage`` with the official
``gtfs-realtime-bindings`` package (module ``google.transit.gtfs_realtime_pb2``)
on top of ``protobuf``.  An installed copy is preferred; when neither is
importable (for example under ``python3 -I``, which hides user site-packages),
a vendored copy of both packages inside ``<repo>/vendor`` is used instead.
See ``vendor/README.md`` for provenance.  The static side of this repository
stays standard-library only; these bindings are needed solely for the
realtime channel.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from .errors import GtfsError

_CACHE: SimpleNamespace | None = None


def bindings() -> SimpleNamespace:
    """Return ``SimpleNamespace(pb=gtfs_realtime_pb2, DecodeError=...)``.

    Raises GtfsError with setup instructions when the bindings cannot be
    found either installed or vendored.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        from google.protobuf.message import DecodeError
        from google.transit import gtfs_realtime_pb2 as pb
    except ImportError:
        vendor = Path(__file__).resolve().parent.parent / "vendor"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        try:
            from google.protobuf.message import DecodeError
            from google.transit import gtfs_realtime_pb2 as pb
        except ImportError as exc:
            raise GtfsError(
                "the realtime channel needs the official gtfs-realtime-bindings "
                "package (and protobuf); install them or keep the vendored copy "
                "in vendor/ — original error: %s" % exc) from exc
    _CACHE = SimpleNamespace(pb=pb, DecodeError=DecodeError)
    return _CACHE
