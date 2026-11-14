"""Build the real GTFS Realtime .pb samples used by the realtime drill.

Run from the repository root:  python3 -I tools/build_rt_fixtures.py

Every message is serialized with the official gtfs-realtime-bindings
(google.transit.gtfs_realtime_pb2, via gtfs_store.rtbind).  Outputs go to
dist/rt/*.pb plus a manifest.json describing each sample.  The script never
reads its previous outputs; dist/rt is recreated empty.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gtfs_store import rtbind  # noqa: E402

pb = rtbind.bindings().pb

DIST_RT = ROOT / "dist" / "rt"

FULL = pb.FeedHeader.FULL_DATASET
DIFF = pb.FeedHeader.DIFFERENTIAL

# Service dates used by the drill instances.
DATE1 = "20261114"
DATE2 = "20261115"
DATE3 = "20261116"

MANIFEST: dict[str, dict] = {}


def stop(seq: int, *, stop_id: str | None = None, rel: str | None = None,
         arr: dict | None = None, dep: dict | None = None) -> dict:
    return {"seq": seq, "stop_id": stop_id, "rel": rel, "arr": arr, "dep": dep}


def update(entity_id: str, trip_id: str, date: str, *,
           start_time: str | None = None, rel: str | None = None,
           timestamp: int | None = None, stops: list[dict] | None = None) -> dict:
    return {"kind": "update", "id": entity_id, "trip_id": trip_id, "date": date,
            "start_time": start_time, "rel": rel, "timestamp": timestamp,
            "stops": stops or []}


def delete(entity_id: str) -> dict:
    return {"kind": "delete", "id": entity_id}


def build(incrementality, timestamp, entities, *, version: str | None = "2.0",
          with_header: bool = True) -> bytes:
    m = pb.FeedMessage()
    if with_header:
        if version is not None:
            m.header.gtfs_realtime_version = version
        if incrementality is not None:
            m.header.incrementality = incrementality
        if timestamp is not None:
            m.header.timestamp = timestamp
    for spec in entities:
        e = m.entity.add()
        if spec.get("id") is not None:
            e.id = spec["id"]
        if spec["kind"] == "delete":
            e.is_deleted = True
            continue
        if spec["kind"] == "raw":
            # Caller fills the entity afterwards (vehicle/alert/mixed samples).
            spec["fill"](e)
            continue
        tu = e.trip_update
        if spec.get("with_trip", True):
            if spec.get("trip_id") is not None:
                tu.trip.trip_id = spec["trip_id"]
            if spec.get("date") is not None:
                tu.trip.start_date = spec["date"]
            if spec.get("start_time") is not None:
                tu.trip.start_time = spec["start_time"]
            if spec.get("rel") is not None:
                tu.trip.schedule_relationship = spec["rel"]
        if spec.get("timestamp") is not None:
            tu.timestamp = spec["timestamp"]
        for s in spec.get("stops", []):
            stu = tu.stop_time_update.add()
            if s.get("seq") is not None:
                stu.stop_sequence = s["seq"]
            if s.get("stop_id") is not None:
                stu.stop_id = s["stop_id"]
            if s.get("rel") is not None:
                stu.schedule_relationship = s["rel"]
            for which, key in (("arr", "arrival"), ("dep", "departure")):
                fields = s.get(which)
                if fields is None:
                    continue
                ev = getattr(stu, key)
                # Merely accessing a submessage does not mark it present; an
                # explicitly empty event ({}) must still be on the wire.
                ev.SetInParent()
                if "time" in fields:
                    ev.time = fields["time"]
                if "delay" in fields:
                    ev.delay = fields["delay"]
                if "uncertainty" in fields:
                    ev.uncertainty = fields["uncertainty"]
    if spec_needs_partial(entities, with_header, version):
        return m.SerializePartialToString()
    return m.SerializeToString()


def spec_needs_partial(entities, with_header, version) -> bool:
    if not with_header or version is None:
        return True
    return any(s.get("id") is None for s in entities)


def sample(name: str, data: bytes, *, incrementality, timestamp, note: str) -> None:
    (DIST_RT / name).write_bytes(data)
    MANIFEST[name] = {"incrementality": incrementality, "timestamp": timestamp,
                      "bytes": len(data), "note": note}


def main() -> int:
    if DIST_RT.exists():
        shutil.rmtree(DIST_RT)
    DIST_RT.mkdir(parents=True)

    SCHED = pb.TripDescriptor.SCHEDULED
    CANCEL = pb.TripDescriptor.CANCELED
    SKIP = pb.TripUpdate.StopTimeUpdate.SKIPPED
    NODATA = pb.TripUpdate.StopTimeUpdate.NO_DATA

    # -- main drill sequence -------------------------------------------------
    sample("m01-full-a-b.pb", build(FULL, 1000, [
        update("A", "T_WKDY", DATE1, rel=SCHED, timestamp=999, stops=[
            stop(10, arr={"time": 29000, "delay": 60, "uncertainty": 30},
                 dep={"time": 29040}),
            stop(20, arr={"delay": 0}, dep={}),
            stop(30, rel=SKIP),
            stop(40),
        ]),
        update("B", "T_LATE", DATE1, rel=SCHED, stops=[
            stop(1, arr={"time": 91000}),
            stop(2, dep={"delay": -30}),
        ]),
    ]), incrementality="FULL_DATASET", timestamp=1000,
        note="baseline: A (T_WKDY, mixed stop forms) and B (T_LATE)")

    sample("m02-diff-a.pb", build(DIFF, 1010, [
        update("A", "T_WKDY", DATE1, rel=SCHED, timestamp=1005, stops=[
            stop(10, arr={"time": 29010}),
            stop(20),
        ]),
    ]), incrementality="DIFFERENTIAL", timestamp=1010,
        note="replace A only: fewer stops, delay/uncertainty absent this time")

    sample("m03-diff-del-b.pb", build(DIFF, 1020, [delete("B")]),
           incrementality="DIFFERENTIAL", timestamp=1020,
           note="incremental withdrawal of B")

    sample("m04-full-c.pb", build(FULL, 1030, [
        update("C", "T_WKND", DATE2, rel=SCHED,
               stops=[stop(1, arr={"time": 38000, "uncertainty": 15})]),
    ]), incrementality="FULL_DATASET", timestamp=1030,
        note="full dataset without A: A is withdrawn by omission")

    sample("m05-full-empty.pb", build(FULL, 1040, []),
           incrementality="FULL_DATASET", timestamp=1040,
           note="empty full dataset: withdraw everything")

    sample("m06-full-d-cancel.pb", build(FULL, 1050, [
        update("D", "T_TWO_STOPS", DATE1, rel=CANCEL),
    ]), incrementality="FULL_DATASET", timestamp=1050,
        note="D cancels T_TWO_STOPS: a still-valid stop-service notice")

    sample("m07-diff-del-d.pb", build(DIFF, 1060, [delete("D")]),
           incrementality="DIFFERENTIAL", timestamp=1060,
           note="withdraw the cancellation: the source no longer tells us")

    sample("m08-full-x.pb", build(FULL, 1070, [
        update("X", "T_FREQ_EXACT", DATE3, start_time="09:30:00", rel=SCHED,
               stops=[stop(1, arr={"delay": 120})]),
    ]), incrementality="FULL_DATASET", timestamp=1070,
        note="X holds instance (T_FREQ_EXACT, 20261116, 09:30:00)")

    sample("m09-diff-rename.pb", build(DIFF, 1080, [
        delete("X"),
        update("Y", "T_FREQ_EXACT", DATE3, start_time="09:30:00", rel=SCHED,
               stops=[stop(2, arr={"delay": 300})]),
    ]), incrementality="DIFFERENTIAL", timestamp=1080,
        note="same message withdraws X and hands the instance to Y")

    sample("m09b-diff-rename-swapped.pb", build(DIFF, 1080, [
        update("Y", "T_FREQ_EXACT", DATE3, start_time="09:30:00", rel=SCHED,
               stops=[stop(2, arr={"delay": 300})]),
        delete("X"),
    ]), incrementality="DIFFERENTIAL", timestamp=1080,
        note="same change with entities swapped; the result must be identical")

    sample("m10-diff-bad-seq.pb", build(DIFF, 1090, [
        update("E", "T_WKDY", DATE1, stops=[stop(999, arr={"time": 1})]),
    ]), incrementality="DIFFERENTIAL", timestamp=1090,
        note="stop_sequence 999 does not exist in T_WKDY: rejected")

    sample("m11-diff-bad-seq-fixed.pb", build(DIFF, 1090, [
        update("E", "T_WKDY", DATE1, stops=[stop(40, arr={"time": 29300})]),
    ]), incrementality="DIFFERENTIAL", timestamp=1090,
        note="corrected resend at the same timestamp: accepted")

    sample("m12-conflict-full-z.pb", build(FULL, 1080, [
        update("Z", "T_WKND", DATE2, stops=[stop(1, arr={"time": 1})]),
    ]), incrementality="FULL_DATASET", timestamp=1080,
        note="timestamp 1080 is already applied from m09: byte conflict")

    sample("m13-late-diff.pb", build(DIFF, 1075, [
        update("L", "T_WKND", DATE2, stops=[stop(1, arr={"time": 2})]),
    ]), incrementality="DIFFERENTIAL", timestamp=1075,
        note="new bytes older than the head: late record only")

    sample("m14-heartbeat.pb", build(DIFF, 1100, []),
           incrementality="DIFFERENTIAL", timestamp=1100,
           note="empty differential: heartbeat, moves only the head")
    sample("m15-heartbeat.pb", build(DIFF, 1110, []),
           incrementality="DIFFERENTIAL", timestamp=1110,
           note="second heartbeat")

    # -- race samples ----------------------------------------------------------
    sample("race-full.pb", build(FULL, 2000, [
        update("X", "T_WKDY", DATE1, stops=[stop(10, arr={"time": 29000})]),
    ]), incrementality="FULL_DATASET", timestamp=2000,
        note="race: full dataset carrying X")
    sample("race-diff.pb", build(DIFF, 2001, [
        update("Y", "T_LATE", DATE1, stops=[stop(1, arr={"time": 91000})]),
    ]), incrementality="DIFFERENTIAL", timestamp=2001,
        note="race: differential carrying Y (needs the baseline)")

    # -- envelope / structure rejections (all at fresh timestamp 3000) --------
    ok_entity = [update("Q", "T_WKND", DATE2, stops=[stop(1, arr={"time": 1})])]
    sample("bad-version.pb", build(FULL, 3000, ok_entity, version="1.0"),
           incrementality="FULL_DATASET", timestamp=3000, note="version 1.0 is not accepted")
    sample("bad-no-version.pb", build(FULL, 3000, ok_entity, version=None),
           incrementality="FULL_DATASET", timestamp=3000, note="version field missing")
    sample("bad-no-incrementality.pb", build(None, 3000, ok_entity),
           incrementality=None, timestamp=3000, note="incrementality not set")
    sample("bad-no-timestamp.pb", build(FULL, None, ok_entity),
           incrementality="FULL_DATASET", timestamp=None, note="timestamp missing")
    sample("bad-zero-timestamp.pb", build(FULL, 0, ok_entity),
           incrementality="FULL_DATASET", timestamp=0, note="timestamp must be positive")
    sample("bad-no-header.pb", build(FULL, 3000, ok_entity, with_header=False),
           incrementality=None, timestamp=None, note="no header at all")
    sample("corrupt.pb", b"\xff\xfe\xfd\x00not a feed message",
           incrementality=None, timestamp=None, note="garbage bytes")

    def fill_vehicle(e):
        e.vehicle.trip.trip_id = "T_WKND"

    def fill_mixed(e):
        e.trip_update.trip.trip_id = "T_WKND"
        e.trip_update.trip.start_date = DATE2
        e.alert.cause = pb.Alert.Cause.MAINTENANCE

    sample("bad-vehicle.pb", build(FULL, 3000, [
        {"kind": "raw", "id": "V", "fill": fill_vehicle},
    ]), incrementality="FULL_DATASET", timestamp=3000, note="vehicle entity, not trip_update")
    sample("bad-mixed.pb", build(FULL, 3000, [
        {"kind": "raw", "id": "M", "fill": fill_mixed},
    ]), incrementality="FULL_DATASET", timestamp=3000, note="trip_update and alert mixed")
    sample("bad-empty-payload.pb", build(FULL, 3000, [
        {"kind": "raw", "id": "P", "fill": lambda e: None},
    ]), incrementality="FULL_DATASET", timestamp=3000, note="entity carries nothing")
    sample("bad-empty-id.pb", build(FULL, 3000, [
        update("", "T_WKND", DATE2),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="empty entity id")
    sample("bad-no-id.pb", build(FULL, 3000, [
        {"kind": "update", "id": None, "trip_id": "T_WKND", "date": DATE2,
         "start_time": None, "rel": None, "timestamp": None, "stops": []},
    ]), incrementality="FULL_DATASET", timestamp=3000, note="entity id missing")
    sample("bad-dup-id.pb", build(FULL, 3000, [
        update("DUP", "T_WKND", DATE2, stops=[stop(1)]),
        update("DUP", "T_WKND", DATE2, stops=[stop(2)]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="entity id twice in one message")
    sample("bad-deleted-payload.pb", build(DIFF, 3000, [
        {"kind": "raw", "id": "W",
         "fill": lambda e: (setattr(e, "is_deleted", True),
                            setattr(e.trip_update.trip, "trip_id", "T_WKND"),
                            setattr(e.trip_update.trip, "start_date", DATE2))},
    ]), incrementality="DIFFERENTIAL", timestamp=3000,
        note="is_deleted together with a trip payload")
    sample("bad-deleted-in-full.pb", build(FULL, 3000, [delete("W")]),
           incrementality="FULL_DATASET", timestamp=3000,
           note="is_deleted inside a FULL_DATASET")
    sample("bad-unknown-trip.pb", build(FULL, 3000, [
        update("Q", "T_NOPE", DATE1, stops=[stop(1)]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="trip not in the bound edition")
    sample("bad-stop-id-mismatch.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, stops=[stop(10, stop_id="S2")]),
    ]), incrementality="FULL_DATASET", timestamp=3000,
        note="stop_id does not match the sequence's static stop")
    sample("bad-dup-seq.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, stops=[stop(10), stop(10)]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="stop_sequence twice in one entity")
    sample("bad-neg-uncertainty.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, stops=[stop(10, arr={"uncertainty": -1})]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="negative uncertainty")
    sample("bad-canceled-with-stops.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, rel=CANCEL, stops=[stop(10)]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="CANCELED with stop events")
    sample("bad-trip-rel.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, rel=pb.TripDescriptor.ADDED),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="trip relationship ADDED unsupported")
    sample("bad-stop-rel.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1,
               stops=[stop(10, rel=pb.TripUpdate.StopTimeUpdate.UNSCHEDULED)]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="stop relationship UNSCHEDULED")
    sample("bad-no-start-date.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", None),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="start_date missing")
    sample("bad-skipped-with-event.pb", build(FULL, 3000, [
        update("Q", "T_WKDY", DATE1, stops=[stop(10, rel=SKIP, arr={"time": 1})]),
    ]), incrementality="FULL_DATASET", timestamp=3000, note="SKIPPED stop carrying arrival")
    sample("bad-instance-conflict.pb", build(DIFF, 3000, [
        update("G", "T_WKDY", DATE1, stops=[stop(10, arr={"time": 5})]),
    ]), incrementality="DIFFERENTIAL", timestamp=3000,
        note="G claims the instance that E still represents")

    (DIST_RT / "manifest.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print("wrote %d samples to %s" % (len(MANIFEST), DIST_RT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
