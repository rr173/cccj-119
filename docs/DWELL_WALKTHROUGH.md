# 离线 GPS 驻留加工演练（实际执行转录）

本文件由 `python3 -I tools/dwell_walkthrough.py` 生成，内容是对一个全新
SQLite 库的真实终端执行转录（含候选穷举复核、手写时间线逐条比对、落库中途
崩溃后重开、以及独立重跑作业），不是成功提示的汇总。重新生成：

```
python3 -I tools/build_dwell_fixtures.py
python3 -I tools/dwell_walkthrough.py
```

参数固定为 `enter_m=120 exit_m=250 min_dwell_s=60 max_gap_s=120`，
Haversine 地球半径 6371008.8 m，比较不做米级取整。

==============================================================================
1. Load static editions (different runs may use different versions)
==============================================================================
  dwell/v1 -> base rows agency=1 stops=7 routes=1 trips=6 stop_times=8 calendar=1 calendar_dates=0 frequencies=0
  dwell/v2 -> base rows agency=1 stops=7 routes=1 trips=7 stop_times=10 calendar=1 calendar_dates=0 frequencies=0
  dwell/v3-bad-coords -> base rows agency=1 stops=7 routes=1 trips=6 stop_times=8 calendar=1 calendar_dates=0 frequencies=0
  dwell/large -> base rows agency=1 stops=400 routes=1 trips=1 stop_times=400 calendar=1 calendar_dates=0 frequencies=0

==============================================================================
2. Process the hand-written sample file as one independent job
==============================================================================
dwell job=1 source=samples.csv sha256=1ad59c2d00613e7e
params enter_m=120 exit_m=250 min_dwell_s=60 max_gap_s=120
runs total=17 processed=12 failed=5 dwell_facts=15
  run=R1 OK dwells=1 indexed_stops=1 candidate_checks=3 cell_scans=75
  run=R10-BADCOORD FAILED
    line=60 bad_coordinate: lat='nan' lon='13.0000000' is not a finite WGS84 coordinate in range
  run=R10-DUPID FAILED
    line=63 duplicate_sample_id: sample_id=D1 repeats line 62
  run=R10-DUPTS FAILED
    line=65 duplicate_timestamp: observed_utc=9000 repeats line 64; input order must not break the conflict
  run=R10-OK OK dwells=1 indexed_stops=1 candidate_checks=2 cell_scans=30
  run=R11-V2 OK dwells=1 indexed_stops=2 candidate_checks=2 cell_scans=30
  run=R12-BADSTOP FAILED
    line=17 candidate_stops: candidate stop 'CHARLIE' of trip 'TRIP_C' (dwell/v3-bad-coords) has missing, non-finite or out-of-range coordinates (lat=None, lon=None); the run cannot be processed
  run=R14-EMPTY FAILED
    line=- no_samples: run_id=R14-EMPTY is bound but has no sample rows
  run=R15 OK dwells=2 indexed_stops=1 candidate_checks=4 cell_scans=75
  run=R2 OK dwells=2 indexed_stops=2 candidate_checks=8 cell_scans=135
  run=R3 OK dwells=0 indexed_stops=1 candidate_checks=2 cell_scans=60
  run=R4 OK dwells=2 indexed_stops=2 candidate_checks=7 cell_scans=90
  run=R5 OK dwells=1 indexed_stops=1 candidate_checks=2 cell_scans=30
  run=R6 OK dwells=1 indexed_stops=1 candidate_checks=2 cell_scans=18
  run=R7 OK dwells=1 indexed_stops=1 candidate_checks=2 cell_scans=77076
  run=R8 OK dwells=2 indexed_stops=2 candidate_checks=8 cell_scans=60
  run=R9 OK dwells=1 indexed_stops=1 candidate_checks=3 cell_scans=75


==============================================================================
3. Independent cross-check A: full-stop exhaustive candidate scan
==============================================================================
  points cross-checked against every candidate stop: 63
  index vs brute-force candidate mismatches: 0

==============================================================================
4. Independent cross-check B: hand timeline, state by state
==============================================================================
  hand-written dwells: 15  stored facts: 15
  every fact matches the hand-written states, instants, support
  samples, end reason and source lines (not just the row count)

==============================================================================
5. Scenario ledger (the actual stored facts)
==============================================================================
  R1        ALPHA  observed_exit  arrival=1020  confirm=1080  last=1110  departure=1140  support=4
  R10-OK    ALPHA  open_ended     arrival=9000  confirm=9060  last=9090  departure=-     support=3
  R11-V2    ALPHA  open_ended     arrival=9500  confirm=9560  last=9590  departure=-     support=3
  R15       ALPHA  gap_truncated  arrival=10000 confirm=10060 last=10100 departure=-     support=3
  R15       ALPHA  observed_exit  arrival=10500 confirm=10560 last=10600 departure=10640 support=3
  R2        ALPHA  observed_exit  arrival=2020  confirm=2080  last=2110  departure=2140  support=4
  R2        BRAVO  observed_exit  arrival=2170  confirm=2240  last=2270  departure=2300  support=4
  R4        ALPHA  observed_exit  arrival=4000  confirm=4060  last=4060  departure=4120  support=2
  R4        ALPHA  gap_truncated  arrival=4240  confirm=4300  last=4420  departure=-     support=3
  R5        ALPHA  open_ended     arrival=5000  confirm=5060  last=5090  departure=-     support=3
  R6        ANTI   open_ended     arrival=6000  confirm=6060  last=6180  departure=-     support=4
  R7        POLAR  open_ended     arrival=7000  confirm=7060  last=7120  departure=-     support=3
  R8        007    observed_exit  arrival=8000  confirm=8060  last=8060  departure=8100  support=2
  R8        7      open_ended     arrival=8160  confirm=8220  last=8260  departure=-     support=3
  R9        ALPHA  observed_exit  arrival=1020  confirm=1080  last=1110  departure=1140  support=4

==============================================================================
6. Bad runs are isolated; sibling good runs and diagnostics survive
==============================================================================
  R10-BADCOORD   status=failed line=60 bad_coordinate: lat='nan' lon='13.0000000' is not a finite WGS84 coordinate in range
  R10-DUPID      status=failed line=63 duplicate_sample_id: sample_id=D1 repeats line 62
  R10-DUPTS      status=failed line=65 duplicate_timestamp: observed_utc=9000 repeats line 64; input order must not break the conflict
  R12-BADSTOP    status=failed line=17 candidate_stops: candidate stop 'CHARLIE' of trip 'TRIP_C' (dwell/v3-bad-coords) has missing, non
  R14-EMPTY      status=failed line=- no_samples: run_id=R14-EMPTY is bound but has no sample rows
  R10-OK in the same file: status=processed dwells=1

==============================================================================
7. Spatial index actually participates (400 candidates, 40 points)
==============================================================================
dwell job=2 source=samples-large.csv sha256=1a6a9078d3ad44bd
params enter_m=120 exit_m=250 min_dwell_s=60 max_gap_s=120
runs total=1 processed=1 failed=0 dwell_facts=1
  run=LRUN OK dwells=1 indexed_stops=400 candidate_checks=9 cell_scans=36
  index explanation: {"antimeridian_safe": true, "candidate_checks": 9, "cell_scans": 36, "cell_size_degrees": 0.0010791844364694456, "index": "uniform_latlon_grid", "indexed_stops": 400, "occupied_cells": 370, "occupied_lat_bands": 370, "occupied_lon_bands": 1, "pole_safe": true}
  exact Haversine checks via index: 9; a Cartesian scan would be 16000

==============================================================================
8. Crash mid-write leaves no half job; reopen; rerun is independent
==============================================================================
  crashed process exit=2: error: fault injection: dwell job interrupted after 3 run(s)
  jobs visible after reopen: [1, 2]
  crashed job exposed any row: False
  orphan evidence rows: 0
  rerun created job_id: 3 (original sample job: 1 )

==============================================================================
9. File-level failures create no job and name the source line
==============================================================================
  missing run_id -> exit 1 : error: sample file has unattributable row(s): line 2: run_id is empty; the row cannot be attributed to a run
  broken binding -> exit 1 : error: binding line 2: trip_id must not be empty for run_id=RX

==============================================================================
10. Evidence drill-down for one dwell (raw values and source lines)
==============================================================================
job=1 dwell_seq=1 run=R1 stop=ALPHA(Alpha Square) dwell/v1
arrival=1020 confirm=1080 last_observed=1110 departure=1140 reason=observed_exit support_points=4
first_sample=R1-b last_inside_sample=R1-e first_outside_sample=R1-f
  #1  arrival       sample=R1-b   t=1020       lat=52.5192805 lon=13.4050000 line=3   dist=80.0049m
  #2  inside        sample=R1-c   t=1050       lat=52.5200000 lon=13.4050000 line=4   dist=0.0000m
  #3  confirm       sample=R1-d   t=1080       lat=52.5205396 lon=13.4050000 line=5   dist=60.0009m
  #4  last_inside   sample=R1-e   t=1110       lat=52.5213490 lon=13.4050000 line=6   dist=150.0022m
  #5  first_outside sample=R1-f   t=1140       lat=52.5228778 lon=13.4050000 line=7   dist=319.9972m


Walkthrough database: /tmp/dw-final2.db
