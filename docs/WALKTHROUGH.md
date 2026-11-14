# 端到端演练记录（实际查询，非成功消息）

由 `python3 -I tools/walkthrough.py` 生成；下面是该命令的真实输出，所有数字都来自
实际落库行（SQLite 查询），而不是命令的成功消息。

复现：

```
python3 -I tools/build_fixtures.py
python3 -I tools/walkthrough.py
```

要点归纳：

- 第 8–11 步对同一请求记录 `departures` 行数、`service_days` 天数和一个结果内容
  SHA-256：重复请求完全一致；中断（退出码 3）后行数、天数和哈希都不变；单独打开的
  读连接在同一时刻看到的也是上一份完整结果；重做后与基线逐行一致。
- 第 10 步的中断发生在写事务内部；SQLite 回滚后 `departures` 仍是 20782 行、
  `service_days` 仍是 279 天，没有泄漏任何半成品日期。
- 第 12 步核对隔离：`demo/v1` 没有 `v2` 独有的 trip，`v2` 有；`other/v1` 的同名
  `T_WKDY` 发车是 16:00（demo 是 08:05:30），且每条发车的 `stop_id` 都能在同一
  `(feed_key, edition)` 内解析。
- 第 13 步证实被拒收的 edition 不能展开，且没有影响已 ready 的 edition。

## 原始输出

```

==============================================================================
1. Load editions: two versions of one operator, a second operator, three rejected packages
==============================================================================
demo   v1               -> ready     edition loaded
demo   v2               -> ready     edition loaded
other  v1               -> ready     edition loaded
demo   bad-overlap      -> rejected  package rejected; no edition was made available
demo   bad-missing-time -> rejected  package rejected; no edition was made available
demo   bad-foreign-key  -> rejected  package rejected; no edition was made available

==============================================================================
2. Edition state as stored (status, digest, timezone)
==============================================================================
  feed_key | edition | status | content_sha | timezone
  demo | bad-foreign-key | rejected | 0bea9222e4665948 | None
  demo | bad-missing-time | rejected | 1a8a586765d1df8f | None
  demo | bad-overlap | rejected | a5f67cc957330459 | None
  demo | v1 | ready | 1e407c25d2026d8c | Europe/Berlin
  demo | v2 | ready | 547840ec023e264e | Europe/Berlin
  other | v1 | ready | 88047122d710e4cb | Europe/Berlin

==============================================================================
3. Refusal reasons actually recorded (file / line / field)
==============================================================================
  edition | category | file_name | line_number | field | message
  bad-foreign-key | foreign_key | stop_times.txt | 2 | stop_id | stop_times stop_id 'S_GONE' does not exist in stops.txt
  bad-missing-time | missing_time | stop_times.txt | 3 | arrival_time | arrival_time is required (no interpolation is performed)
  bad-overlap | overlapping_frequency | frequencies.txt | 9 | start_time | trip 'T_FREQ_EXACT' frequency intervals overlap: [09:30:00,12:00:00) o
  bad-overlap | overlapping_frequency | frequencies.txt | 3 | start_time | trip 'T_FREQ_EXACT' frequency intervals overlap: [11:00:00,13:00:00) o

==============================================================================
4. Expand a spring-DST and a fall-DST span for demo/v1
==============================================================================
  20260328 services=SAT_REMOVED,WKDY,WKND            instances= 43 rows=116
  20260329 services=-                                instances=  0 rows=  0
  20260330 services=WKDY,WKND,WKND_ONLY_ADDED        instances= 43 rows=116
  20261024 services=SAT_REMOVED,WKDY,WKND            instances= 43 rows=116
  20261025 services=WKND                             instances=  2 rows=  4
  20261026 services=WKDY                             instances= 40 rows=110

==============================================================================
5. Day-start instants (local noon -> UTC -> minus 12 actual hours)
==============================================================================
  service_date | timezone | day_start_utc
  20260328 | Europe/Berlin | 2026-03-27T23:00:00Z
  20260329 | Europe/Berlin | 2026-03-28T22:00:00Z
  20260330 | Europe/Berlin | 2026-03-29T22:00:00Z
  20261024 | Europe/Berlin | 2026-10-23T22:00:00Z
  20261025 | Europe/Berlin | 2026-10-24T23:00:00Z
  20261026 | Europe/Berlin | 2026-10-25T23:00:00Z

==============================================================================
6. Stop 007 on 2026-03-30: certainty split and late-night rows
==============================================================================
  certainty | rows
  exact | 39
  headway | 2
  departure_seconds | departure_local | utc_offset | departure_utc | trip_id | trip_instance_id
  84600 | 2026-03-30T23:30:00 | +02:00 | 2026-03-30T21:30:00Z | T_MIDNIGHT | F:T_MIDNIGHT:5:0
  86400 | 2026-03-31T00:00:00 | +02:00 | 2026-03-30T22:00:00Z | T_MIDNIGHT | F:T_MIDNIGHT:5:1
  88200 | 2026-03-31T00:30:00 | +02:00 | 2026-03-30T22:30:00Z | T_MIDNIGHT | F:T_MIDNIGHT:5:2
  90600 | 2026-03-31T01:10:00 | +02:00 | 2026-03-30T23:10:00Z | T_LATE | T:T_LATE
  95400 | 2026-03-31T02:30:00 | +02:00 | 2026-03-31T00:30:00Z | T_WKND_LATE | T:T_WKND_LATE

==============================================================================
7. 007 and 7 are different stops with different departures
==============================================================================
  stop_id | rows | first_sec
  007 | 41 | 25200
  7 | 1 | 54000

==============================================================================
8. Expand the full span once; row count, day count and a content hash of the result
==============================================================================
  rows=20782 service_days=279 sha256=c14a6315193e8e1face8815668e306bb41389e9615b947ac6b6284f9289be99c

==============================================================================
9. Repeat the identical request: nothing is added, nothing changes
==============================================================================
  rows=20782 service_days=279 sha256=c14a6315193e8e1face8815668e306bb41389e9615b947ac6b6284f9289be99c
  identical to step 8: True

==============================================================================
10. Attempt a replacement that is interrupted after 3 service days
==============================================================================
  rows before attempt: 20782  service_days: 279
  exit=3 stderr='error: fault injection: expansion interrupted after 3 service day(s)'
  rows after interrupted attempt: 20782  service_days: 279
  separate reader still sees 20260330 rows: 116
  reader's own snapshot hash of full span: c14a6315193e8e1face8815668e306bb41389e9615b947ac6b6284f9289be99c
  matches step 8: True

==============================================================================
11. Redo the same request and compare with the step-8 snapshot
==============================================================================
  rows=20782 service_days=279 sha256=c14a6315193e8e1face8815668e306bb41389e9615b947ac6b6284f9289be99c
  identical to step 8: True

==============================================================================
12. Isolation: same internal ids in demo/v1, demo/v2 and other/v1
==============================================================================
  feed_key | edition | rows | instances
  demo | v1 | 116 | 43
  demo | v2 | 118 | 44
  other | v1 | 2 | 1
  feed_key | edition | stop_id | departure_seconds | departure_local | trip_instance_id
  demo | v1 | 007 | 28800 | 2026-03-30T08:00:00 | T:T_WKDY
  demo | v1 | S2 | 29130 | 2026-03-30T08:05:30 | T:T_WKDY
  demo | v1 | S3 | 29400 | 2026-03-30T08:10:00 | T:T_WKDY
  demo | v1 | S4 | 30000 | 2026-03-30T08:20:00 | T:T_WKDY
  demo | v2 | 007 | 28800 | 2026-03-30T08:00:00 | T:T_WKDY
  demo | v2 | S2 | 29130 | 2026-03-30T08:05:30 | T:T_WKDY
  demo | v2 | S3 | 29400 | 2026-03-30T08:10:00 | T:T_WKDY
  demo | v2 | S4 | 30000 | 2026-03-30T08:20:00 | T:T_WKDY
  other | v1 | 007 | 57600 | 2026-03-30T16:00:00 | T:T_WKDY
  other | v1 | S2 | 58020 | 2026-03-30T16:07:00 | T:T_WKDY
  demo/v1 rows for T_WKDY_V2_EXTRA (must be 0): 0
  demo/v2 rows for T_WKDY_V2_EXTRA (must be 2): 2
  every stored departure stop_id resolves in the same feed/edition: True
  other/v1 references only its own stops (S2 times 16:07, demo's S2 is 08:05:30): 1

==============================================================================
13. Rejected edition cannot be expanded and did not disturb ready ones
==============================================================================
  expand bad-overlap -> GtfsError : edition 'bad-overlap' of feed_key 'demo' is rejected and cannot be expanded
  demo/v1 rows still present: True
```
