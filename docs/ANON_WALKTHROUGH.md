
==============================================================================
0. Copy the hand-built GPS jobs into a fresh working database
==============================================================================
  database: /tmp/anon-walkthrough-6aknpsat/anon-walkthrough.db

==============================================================================
1. Independent reference: hand-computed sequences per level
==============================================================================
  level 0 cell=500000 bucket=60 groups=13 rare=9
    vc=2 [[-2, 2, 2], [-1, 1, 1], [-1, 0, 0]] runs=[(1, 'S7-N1'), (1, 'S7-N2')]
    vc=1 [[100, 114, 660], [101, 112, 661], [102, 111, 662]] runs=[(1, 'S6-UNIQ')]
    vc=2 [[100, 288, 390], [101, 288, 390], [102, 289, 391]] runs=[(1, 'S6-U1'), (2, 'S6-U2')]
    vc=1 [[226, 284, 386], [227, 284, 386], [228, 285, 387]] runs=[(1, 'S1-A-2')]
    vc=1 [[60, 284, 386], [61, 284, 386], [62, 284, 386], [63, 284, 386]] runs=[(1, 'S3-Z2')]
    vc=1 [[60, 284, 386], [61, 284, 386], [62, 284, 386]] runs=[(1, 'S3-Z1')]
    vc=2 [[60, 284, 386], [61, 284, 386], [62, 285, 387]] runs=[(1, 'S1-A-1'), (1, 'S2-X')]
    vc=1 [[60, 284, 386], [61, 284, 386]] runs=[(1, 'S3-Z3')]
    vc=1 [[60, 285, 387], [61, 285, 387], [62, 284, 386]] runs=[(1, 'S2-Y')]
    vc=1 [[66, 284, 386], [67, 284, 386], [68, 285, 387]] runs=[(1, 'S4-Q1')]
    vc=1 [[66, 284, 386], [67, 285, 386], [68, 285, 387]] runs=[(2, 'S4-Q2')]
    vc=2 [[83, 286, 388], [84, 286, 388], [85, 287, 389]] runs=[(1, 'S5-P1'), (1, 'S5-P2')]
    vc=1 [[83, 300, 400], [84, 301, 401], [85, 302, 402]] runs=[(1, 'S5-RARE')]
  level 1 cell=1000000 bucket=120 groups=9 rare=4
    vc=2 [[-1, 1, 1], [-1, 0, 0]] runs=[(1, 'S7-N1'), (1, 'S7-N2')]
    vc=1 [[113, 142, 193], [114, 142, 193]] runs=[(1, 'S1-A-2')]
    vc=5 [[30, 142, 193], [31, 142, 193]] runs=[(1, 'S1-A-1'), (1, 'S2-X'), (1, 'S2-Y'), (1, 'S3-Z1'), (1, 'S3-Z2')]
    vc=1 [[30, 142, 193]] runs=[(1, 'S3-Z3')]
    vc=2 [[33, 142, 193], [34, 142, 193]] runs=[(1, 'S4-Q1'), (2, 'S4-Q2')]
    vc=2 [[41, 143, 194], [42, 143, 194]] runs=[(1, 'S5-P1'), (1, 'S5-P2')]
    vc=1 [[41, 150, 200], [42, 150, 200], [42, 151, 201]] runs=[(1, 'S5-RARE')]
    vc=2 [[50, 144, 195], [51, 144, 195]] runs=[(1, 'S6-U1'), (2, 'S6-U2')]
    vc=1 [[50, 57, 330], [50, 56, 330], [51, 55, 331]] runs=[(1, 'S6-UNIQ')]

==============================================================================
2. Run the real production entry point on the same members
==============================================================================
anon_job=1 k=2 selected_level=1 cell_microdeg=1000000 bucket_s=120 outcome=final_rare_groups_dropped
members=17 published_groups=5 published_trips=13 dropped_trips=4 distinct_vehicles=13
  level=0 cell=500000s bucket=60s groups=13 rare=9 retained_trips=8 dropped_trips=9 retained_vehicles=8 rare_vehicles=9 -> promoted
  level=1 cell=1000000s bucket=120s groups=9 rare=4 retained_trips=13 dropped_trips=4 retained_vehicles=13 rare_vehicles=4 -> final_rare_groups_dropped


==============================================================================
3. Field-by-field comparison: reference vs real CLI ledger
==============================================================================
  level 0 matches: groups=13 rare=9 retained_trips=8 dropped_trips=9
  level 1 matches: groups=9 rare=4 retained_trips=13 dropped_trips=4

==============================================================================
4. Public export vs reference public groups (every field)
==============================================================================
  public_group_no=1 trips=2 vehicles=2 sequence=[[-1, 1, 1], [-1, 0, 0]]
  public_group_no=2 trips=5 vehicles=5 sequence=[[30, 142, 193], [31, 142, 193]]
  public_group_no=3 trips=2 vehicles=2 sequence=[[33, 142, 193], [34, 142, 193]]
  public_group_no=4 trips=2 vehicles=2 sequence=[[41, 143, 194], [42, 143, 194]]
  public_group_no=5 trips=2 vehicles=2 sequence=[[50, 144, 195], [51, 144, 195]]
  every public row also matches dist/anon/anon-expected.json

==============================================================================
5. No internal identifier or precise value leaks externally
==============================================================================
  group object keys exactly: ['public_group_no', 'level_index', 'cell_microdeg', 'bucket_s', 'trip_count', 'vehicle_count', 'sequence']
  banned keys absent from every group: ['anon_job_id', 'internal_group_no', 'job_id', 'manifest', 'observed_utc', 'run_id', 'sample_id', 'seq_hash', 'source_line', 'vehicle_unit']
  sequence triples are plain integer cell numbers only

==============================================================================
6. Scenario ledger (the eight hand-designed situations)
==============================================================================
  S1 same vehicle, multiple trips never reach k (S1-A-2 dropped) S1-A-2   -> dropped (dropped: one vehicle only)
  S2 common cells, unique ordered whole (fine level)             S2-X     -> retained (rare at fine, merged only after coarsening)
  S3 reverse / non-adjacent repeat / prefix never merge          S3-Z3    -> dropped (dropped: prefix is a different whole)
  S4 fine split -> coarse merge                                  S4-Q1    -> retained (published only at coarse level)
  S5 one rare group promotes everyone together                   S5-RARE  -> dropped (promoted then dropped as a whole group)
  S6 still rare at last level -> whole group dropped             S6-UNIQ  -> dropped (dropped whole group)
  S7 negative epochs and boundary cells                          S7-N1    -> retained (published, negative floors correct)
  S8 members span two GPS jobs                                   S4-Q2    -> retained (job 2 member of a published group)
  level 0 outcome=promoted rare_groups=9 -> all members regrouped at level 1 together
  level 1 still processes all 17 members; no group picked its own level

==============================================================================
7. Internal audit expands one public group to raw source rows
==============================================================================
  public_group_no=1 internal_group_no=1 members=2
    job_id=1 run_id=S7-N1 unit=VN1 raw_first=(sample_id=c t=-120 lat=-89.0 lon=-179.0 line=4)
    job_id=1 run_id=S7-N2 unit=VN2 raw_first=(sample_id=c t=-120 lat=-89.0 lon=-179.0 line=4)

==============================================================================
8. Illegal member manifest refuses the whole request, no job written
==============================================================================
  exit code: 2
  stderr: error: manifest line 2: job_id=999 does not exist among the GPS jobs; unselected jobs never participate
  anon job count unchanged: 1

==============================================================================
9. Real process death mid-publish, reopen, independent rerun id
==============================================================================
  hard exit code: 91
  no half job visible after reopen; only job 1 exists and every audit row belongs to it
  rerun created independent anon_job_id: 2

==============================================================================
10. Production scale: >=200k samples, peak RSS, then doubled input
==============================================================================
  200k samples             samples= 200000 peak_rss=    26 MiB db_on_disk=  22.7 MiB groups=96 published_trips=400 dropped=0
  400k samples (doubled)   samples= 400000 peak_rss=    28 MiB db_on_disk=  44.0 MiB groups=188 published_trips=796 dropped=4
  doubling the samples changed peak RSS by +2 MiB while the data on disk grew by 21.3 MiB
  -> peak RSS stays bounded by per-run/per-group working space, not by input size; the growth lands in the on-disk scratch database.

Walkthrough database: /tmp/anon-walkthrough-6aknpsat/anon-walkthrough.db
Scale database: /tmp/anon-walkthrough-6aknpsat/scale.db
