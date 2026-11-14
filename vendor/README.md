# Vendored runtime dependencies of the realtime channel

The realtime channel (`gtfs_store/realtime.py`) parses GTFS Realtime
`FeedMessage` blobs with the **official** `gtfs-realtime-bindings` package,
which itself needs the `protobuf` runtime.  The static side of this
repository stays standard-library only; these two packages are needed solely
for `rt-*` commands, `tools/build_rt_fixtures.py`, `tools/rt_walkthrough.py`
and `tests/test_realtime.py`.

This directory holds an unmodified copy of both packages so the feature also
works where they are not installed (the test suite runs under `python3 -I`,
which hides user site-packages).  `gtfs_store/rtbind.py` first tries a normal
import and only falls back to this directory.

Contents (copied from an unmodified `pip install gtfs-realtime-bindings`
on CPython 3.11 / Linux aarch64):

- `google/transit/gtfs_realtime_pb2.py` — gtfs-realtime-bindings 3.0.0
  (Apache License 2.0, <https://github.com/MobilityData/gtfs-realtime-bindings>)
- `google/protobuf/**` — protobuf 7.36.2 (BSD-3-Clause,
  <https://github.com/protocolbuffers/protobuf>), including the `upb`
  C-extension for this platform
- `PROTOBUF-LICENSE` — the protobuf license text

To refresh: `pip download gtfs-realtime-bindings protobuf`, unpack the
wheels, and replace the `google/` tree here.  If the packages are installed
into the environment instead, those win and this directory is ignored.
