# 实时通道端到端演练记录（实际查询，非成功消息）

由 `python3 -I tools/rt_walkthrough.py` 生成；下面是该命令的真实输出。所有状态都来自
实际落库行（SQLite 查询）或 API 读回，而不是命令的成功消息；脚本对每一步都做了确定
的断言，任何一处不符都会以非零退出。

复现：

```
python3 -I tools/build_fixtures.py        # 静态示例包（dist/*.zip）
python3 -I tools/build_rt_fixtures.py     # 实时样本（dist/rt/*.pb，官方绑定生成）
python3 -I tools/rt_walkthrough.py
```

要点归纳：

- 第 2 步：`FULL_DATASET {A,B}` 建立基线；`DIFFERENTIAL` 只替换 A——B 的列值与计龄
  时戳（1000）原样保留，A 的旧站点明细被新载荷整体替代（seq 30/40 消失，seq 10 的
  delay/uncertainty 被清空而不是沿用）。
- 第 3 步：显式 `delay=0` 与“没有 delay”是两回事——reception #1 的原始 BLOB 与文件
  逐字节一致，重放可见 `HasField` 的区别。
- 第 4–5 步：增量 `is_deleted` 才移除 B；完整包遗漏即撤回 A；空完整包撤回全部；
  成功账（`rt_head_ledger`）在实体消失后仍然保留 1000..1040。`CANCELED` 是仍然有效
  的停开通告（可读、无站点事件），撤回则是来源不再提供信息（读不到）。
- 第 6 步：同一份增量撤 X、用 Y 接管同一运行实例；换序样本在另一来源上结果一致。
- 第 7 步：坏站序（999）被拒，头部不动、成功账无 1090；修正报文用同一时戳被接受。
- 第 8 步：摘要账先于新旧判断——同样的包再来是 duplicate（返回原结论，不复活实体）；
  同戳改包是 conflict；较旧的新包只留 late 记录。
- 第 9 步：空增量只推头部（心跳）；实体年龄按“最后一次真正被替换的报文时戳”计算，
  到期边界 `now >= ts + max_age` 恰好取等；`now` 落后于来源头部的含过期读取被拒绝；
  `max_age <= 0` 被拒绝。
- 第 10 步：25 个坏样本各自留下拒收说明，当前集合、头部与成功账分毫不动。
- 第 11 步：收包账（`rt_receptions`）记录每一次到达的类别、头部时戳、摘要与大小；
  当前实体经 `reception_id` 反查到产生它的原报文。
- 第 12 步：`regional` 与 `metro` 使用相同实体编号互不影响；`latejoin` 开局即收增量
  被拒（没有可依赖的集合）。
- 第 13 步：两名接收者争用同一来源。完整包先行：两份都应用（X@2000、Y@2001，
  头 2001）；增量先行：增量被拒（无基线），完整包应用（只有 X，头 2000）。线程竞跑
  的实际结局必是两者之一，且头部、实体、成功账彼此对应。
- 第 14 步：进程重新打开数据库后，上述结局保持同一状态。
- 第 15 步：静态展开与实时读取在同一个数据库文件里互不影响。

## 原始输出

```

==============================================================================
1. Static base and permanent source registrations
==============================================================================
  load demo/v1: ready
  load demo/v2: ready
  registered: metro, regional, swap, latejoin, race-ab, race-ba, race (all bound to demo/v1)
  rebind metro->demo/v2 refused: realtime source 'metro' is already bound to demo/v1 and cannot be rebound to demo/v2
  same-binding re-registration is an idempotent no-op                OK

==============================================================================
2. Baseline FULL_DATASET {A,B}, then DIFFERENTIAL replacing only A
==============================================================================
  m01-full-a-b.pb                -> applied   ts=1000  upserts=2 withdrawals=0 stop_events=6 
  entities (id|trip|date|start|rel|entity_ts|head_ts|reception):
    A | T_WKDY | 20261114 |  | SCHEDULED | 999 | 1000 | 1
    B | T_LATE | 20261114 |  | SCHEDULED | None | 1000 | 1
  stop events (entity|seq|stop_id|rel|has_arr|time|delay|unc|has_dep|time|delay|unc):
    A | 10 | None | SCHEDULED | 1 | 29000 | 60 | 30 | 1 | 29040 | None | None
    A | 20 | None | SCHEDULED | 1 | None | 0 | None | 1 | None | None | None
    A | 30 | None | SKIPPED | 0 | None | None | None | 0 | None | None | None
    A | 40 | None | SCHEDULED | 0 | None | None | None | 0 | None | None | None
    B | 1 | None | SCHEDULED | 1 | 91000 | None | None | 0 | None | None | None
    B | 2 | None | SCHEDULED | 0 | None | None | None | 1 | None | -30 | None
  m02-diff-a.pb                  -> applied   ts=1010  upserts=1 withdrawals=0 stop_events=2 
  entities (id|trip|date|start|rel|entity_ts|head_ts|reception):
    A | T_WKDY | 20261114 |  | SCHEDULED | 1005 | 1010 | 2
    B | T_LATE | 20261114 |  | SCHEDULED | None | 1000 | 1
  stop events (entity|seq|stop_id|rel|has_arr|time|delay|unc|has_dep|time|delay|unc):
    A | 10 | None | SCHEDULED | 1 | 29010 | None | None | 0 | None | None | None
    A | 20 | None | SCHEDULED | 0 | None | None | None | 0 | None | None | None
    B | 1 | None | SCHEDULED | 1 | 91000 | None | None | 0 | None | None | None
    B | 2 | None | SCHEDULED | 0 | None | None | None | 1 | None | -30 | None
  A replaced: only seq 10/20 remain, delay/uncertainty of seq 10 cleared OK
  B untouched, still aged from ts=1000                               OK

==============================================================================
3. Explicit zero vs absent (read back of m01 before it was replaced)
==============================================================================
  reception #1 blob is byte-identical to the file                    OK
  m01 A seq20 arrival: delay given and == 0                          OK
  m01 A seq20 arrival: time not given                                OK

==============================================================================
4. Incremental withdrawal of B; then a full dataset without A
==============================================================================
  m03-diff-del-b.pb              -> applied   ts=1020  upserts=0 withdrawals=1 stop_events=0 
  after del B                                                        OK
  m04-full-c.pb                  -> applied   ts=1030  upserts=1 withdrawals=1 stop_events=1 
  full {C} withdraws A by omission                                   OK
  m05-full-empty.pb              -> applied   ts=1040  upserts=0 withdrawals=1 stop_events=0 
  empty full dataset withdraws everything                            OK
  success ledger keeps 1000..1040 even with no entities left         OK

==============================================================================
5. CANCELED is a notice; withdrawal is absence
==============================================================================
  m06-full-d-cancel.pb           -> applied   ts=1050  upserts=1 withdrawals=0 stop_events=0 
  D is a readable CANCELED notice without stop events                OK
  D has no stop events                                               OK
  m07-diff-del-d.pb              -> applied   ts=1060  upserts=0 withdrawals=1 stop_events=0 
  after withdrawal the source simply has nothing                     OK

==============================================================================
6. Rename: same message withdraws X and hands the instance to Y
==============================================================================
  m08-full-x.pb                  -> applied   ts=1070  upserts=1 withdrawals=0 stop_events=1 
  m09-diff-rename.pb             -> applied   ts=1080  upserts=1 withdrawals=1 stop_events=1 
  only Y remains, holding the (T_FREQ_EXACT, 20261116, 09:30:00) instance OK
  m08-full-x.pb                  -> applied   ts=1070  upserts=1 withdrawals=0 stop_events=1 
  m09b-diff-rename-swapped.pb    -> applied   ts=1080  upserts=1 withdrawals=1 stop_events=1 
  swapped entity order on a second source gives the identical set    OK

==============================================================================
7. Bad stop_sequence rejected; corrected resend reuses the timestamp
==============================================================================
  m10-diff-bad-seq.pb            -> rejected  ts=1090  unknown_stop_sequence
  m10 rejected with unknown_stop_sequence                            OK
  head did not move                                                  OK
  1090 not in the success ledger                                     OK
  m11-diff-bad-seq-fixed.pb      -> applied   ts=1090  upserts=1 withdrawals=0 stop_events=1 
  corrected message at the same timestamp applied                    OK

==============================================================================
8. Digest ledger: duplicate, conflict, late
==============================================================================
  m01-full-a-b.pb                -> duplicate ts=1000  original=#1 (applied) duplicate_delivery
  redelivered m01 is a duplicate of reception #1, nothing applied    OK
  the duplicate did not resurrect A/B                                OK
  m12-conflict-full-z.pb         -> conflict  ts=1080  timestamp_conflict
  same timestamp 1080 with different bytes is a conflict             OK
  m13-late-diff.pb               -> late      ts=1075  stale_message
  new bytes older than the head only leave a late record             OK
  set unchanged after duplicate/conflict/late                        OK

==============================================================================
9. Heartbeats move only the head; freshness boundary is exact
==============================================================================
  m14-heartbeat.pb               -> applied   ts=1100  upserts=0 withdrawals=0 stop_events=0 
  m15-heartbeat.pb               -> applied   ts=1110  upserts=0 withdrawals=0 stop_events=0 
  head is now 1110                                                   OK
  E still aged from its replacing message at 1090                    OK
  now=1189: E (expires 1190) fresh, Y (expires 1180) hidden          OK
  now=1190 == ts+max_age: E is expired, default read is empty        OK
  include-expired read still shows E and Y, flagged expired          OK
  include-expired at now=1105 refused: now=1105 is behind the source head timestamp=1110; the include-expired read does not provide a historical view
  now behind the head is refused                                     OK
  max_age=0 refused: max_age must be a positive integer number of seconds
  max_age=0 refused                                                  OK

==============================================================================
10. Every bad sample is rejected with a reason; nothing changes
==============================================================================
  bad-version.pb                 -> rejected  ts=None  unsupported_version
  bad-no-version.pb              -> rejected  ts=None  missing_version
  bad-no-incrementality.pb       -> rejected  ts=None  missing_incrementality
  bad-no-timestamp.pb            -> rejected  ts=None  missing_timestamp
  bad-zero-timestamp.pb          -> rejected  ts=None  non_positive_timestamp
  bad-no-header.pb               -> rejected  ts=None  missing_header
  corrupt.pb                     -> rejected  ts=None  encoding
  bad-vehicle.pb                 -> rejected  ts=3000  unsupported_entity
  bad-mixed.pb                   -> rejected  ts=3000  mixed_entity
  bad-empty-payload.pb           -> rejected  ts=3000  missing_payload
  bad-empty-id.pb                -> rejected  ts=3000  empty_entity_id
  bad-no-id.pb                   -> rejected  ts=3000  missing_entity_id
  bad-dup-id.pb                  -> rejected  ts=3000  duplicate_entity_id
  bad-deleted-payload.pb         -> rejected  ts=3000  deleted_with_payload
  bad-deleted-in-full.pb         -> rejected  ts=3000  deleted_in_full_dataset
  bad-unknown-trip.pb            -> rejected  ts=3000  unknown_trip
  bad-stop-id-mismatch.pb        -> rejected  ts=3000  stop_id_mismatch
  bad-dup-seq.pb                 -> rejected  ts=3000  duplicate_stop_sequence
  bad-neg-uncertainty.pb         -> rejected  ts=3000  negative_uncertainty
  bad-canceled-with-stops.pb     -> rejected  ts=3000  canceled_with_stop_events
  bad-trip-rel.pb                -> rejected  ts=3000  unsupported_trip_relationship
  bad-stop-rel.pb                -> rejected  ts=3000  unsupported_stop_relationship
  bad-no-start-date.pb           -> rejected  ts=3000  missing_start_date
  bad-skipped-with-event.pb      -> rejected  ts=3000  event_on_non_scheduled_stop
  bad-instance-conflict.pb       -> rejected  ts=3000  instance_conflict
  after all rejections the set is still {E, Y}                       OK
  head is still 1110                                                 OK
  success ledger has no 3000 entry                                   OK

==============================================================================
11. Reception ledger: every arrival is accounted for
==============================================================================
  id | category | header_incrementality | header_timestamp | applied | sha | size_bytes
  1 | applied | FULL_DATASET | 1000 | 1 | ff3f913d92d2 | 140
  2 | applied | DIFFERENTIAL | 1010 | 1 | 7605697d68f4 | 58
  3 | applied | DIFFERENTIAL | 1020 | 1 | 60c667a1f350 | 19
  4 | applied | FULL_DATASET | 1030 | 1 | 934c09acf17f | 53
  5 | applied | FULL_DATASET | 1040 | 1 | c6d773943d58 | 12
  6 | applied | FULL_DATASET | 1050 | 1 | 7f0eded85d58 | 46
  7 | applied | DIFFERENTIAL | 1060 | 1 | 28a9339f6c7b | 19
  8 | applied | FULL_DATASET | 1070 | 1 | 656da3951d89 | 65
  9 | applied | DIFFERENTIAL | 1080 | 1 | 0cc7587f6335 | 73
  12 | rejected | DIFFERENTIAL | 1090 | 0 | ace4a8e10aa7 | 48
  13 | applied | DIFFERENTIAL | 1090 | 1 | 6296daf6c542 | 49
  14 | duplicate | FULL_DATASET | 1000 | 0 | ff3f913d92d2 | 140
  15 | conflict | FULL_DATASET | 1080 | 0 | c4fd27b1bf58 | 47
  16 | late | DIFFERENTIAL | 1075 | 0 | 171e0ff79c5f | 47
  17 | applied | DIFFERENTIAL | 1100 | 1 | 786ba32c6143 | 12
  18 | applied | DIFFERENTIAL | 1110 | 1 | e5718ea38bb7 | 12
  19 | rejected | None | None | 0 | 033e28f4f8ab | 47
  20 | rejected | None | None | 0 | c47f4b0f2e73 | 42
  21 | rejected | None | None | 0 | 66b3e5c70076 | 45
  22 | rejected | None | None | 0 | 6a9311ab4998 | 44
  23 | rejected | None | None | 0 | 8ade280a15d9 | 46
  24 | rejected | None | None | 0 | 9ff0e64b36fd | 35
  25 | rejected | None | None | 0 | d7abf592fa53 | 22
  26 | rejected | FULL_DATASET | 3000 | 0 | 1d4b0ecbe0ef | 29
  27 | rejected | FULL_DATASET | 3000 | 0 | 9b4bd82b4f0d | 43
  28 | rejected | FULL_DATASET | 3000 | 0 | 03ef2cbd7700 | 17
  29 | rejected | FULL_DATASET | 3000 | 0 | 2380a4958cf2 | 38
  30 | rejected | FULL_DATASET | 3000 | 0 | 9a82c0a97844 | 36
  31 | rejected | FULL_DATASET | 3000 | 0 | c03cdd603f19 | 78
  32 | rejected | DIFFERENTIAL | 3000 | 0 | 9fded8f2a46a | 41
  33 | rejected | FULL_DATASET | 3000 | 0 | 4a36ed3679a9 | 19
  34 | rejected | FULL_DATASET | 3000 | 0 | 2ae6677ec6a7 | 43
  35 | rejected | FULL_DATASET | 3000 | 0 | e6713f3173f5 | 47
  36 | rejected | FULL_DATASET | 3000 | 0 | 68cae4572122 | 47
  37 | rejected | FULL_DATASET | 3000 | 0 | 4669cc9b1b23 | 56
  38 | rejected | FULL_DATASET | 3000 | 0 | b8e07c7905e6 | 45
  39 | rejected | FULL_DATASET | 3000 | 0 | d221281b4cb9 | 41
  40 | rejected | FULL_DATASET | 3000 | 0 | 839487ca6c89 | 45
  41 | rejected | FULL_DATASET | 3000 | 0 | fa6fbfa32792 | 29
  42 | rejected | FULL_DATASET | 3000 | 0 | 2f1c0b289f3d | 49
  43 | rejected | DIFFERENTIAL | 3000 | 0 | 415551f9a66b | 47
  header_timestamp | sha | reception_id
  1000 | ff3f913d92d2 | 1
  1010 | 7605697d68f4 | 2
  1020 | 60c667a1f350 | 3
  1030 | 934c09acf17f | 4
  1040 | c6d773943d58 | 5
  1050 | 7f0eded85d58 | 6
  1060 | 28a9339f6c7b | 7
  1070 | 656da3951d89 | 8
  1080 | 0cc7587f6335 | 9
  1090 | 6296daf6c542 | 13
  1100 | 786ba32c6143 | 17
  1110 | e5718ea38bb7 | 18
  current entities point back at their producing reception:
  entity_id | reception_id | head_timestamp
  E | 13 | 1090
  Y | 9 | 1080

==============================================================================
12. Two sources, same entity ids, fully independent
==============================================================================
  m01-full-a-b.pb                -> applied   ts=1000  upserts=2 withdrawals=0 stop_events=6 
  m02-diff-a.pb                  -> applied   ts=1010  upserts=1 withdrawals=0 stop_events=2 
  m03-diff-del-b.pb              -> applied   ts=1020  upserts=0 withdrawals=1 stop_events=0 
  regional has its own A (replaced at 1010)                          OK
  metro is unaffected (still {E, Y})                                 OK
  m03-diff-del-b.pb              -> rejected  ts=1020  no_baseline
  differential before any baseline is rejected                       OK

==============================================================================
13. Competing receivers: one full and one differential, both orders
==============================================================================
  sequential, full first:
  race-full.pb                   -> applied   ts=2000  upserts=1 withdrawals=0 stop_events=1 
  race-diff.pb                   -> applied   ts=2001  upserts=1 withdrawals=0 stop_events=1 
  race-ab: both applied                                              OK
  race-ab holds X@2000 and Y@2001, head 2001                         OK
  sequential, differential first:
  race-diff.pb                   -> rejected  ts=2001  no_baseline
  race-full.pb                   -> applied   ts=2000  upserts=1 withdrawals=0 stop_events=1 
  race-ba: differential rejected (no baseline), full applied         OK
  race-ba holds only X@2000, head 2000                               OK
  threaded race on source 'race' (both receivers start together):
  outcomes: full=applied diff=rejected
  stored state: entities=['X'] head=2000 ledger=[2000]
  threaded race landed exactly on one of the two serial outcomes     OK

==============================================================================
14. Reopen the database: head, entities and ledger still agree
==============================================================================
  metro state identical across reopen                                OK
  race outcome identical across reopen                               OK

==============================================================================
15. Static features keep working in the same database
==============================================================================
  expand demo/v1 2026-03-30: {'service_date': '20260330', 'enabled_services': ['WKDY', 'WKND', 'WKND_ONLY_ADDED'], 'trip_instances': 43, 'departure_rows': 116}
  departures at 007 on 2026-03-30: 41 row(s), first trip T_FREQ_WINDOW
  static expansion unaffected by realtime tables                     OK
  realtime reads unaffected by expansion                             OK

Drill finished: every expectation held.  Database: /tmp/gtfs-rt-walkthrough-abpcpt7i/rt-walkthrough.db
```
