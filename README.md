# GTFS 静态接入与服务日展开（SQLite）

本地、只读 GTFS 静态时刻包 → 规范基础表 → 按服务日展开的站点出发表。
回答两个不同问题，并在结果中明确区分：

- **确定发车**：某个服务日某站的具体发车时刻（`certainty = 'exact'`）。
- **班次密度承诺**：只承诺“某窗口内每 N 秒一班”（`certainty = 'headway'`），
  不虚构具体车次。

不规划乘车路径，不做车辆调度或售票。

运行环境：Python 3.9+（本仓库在 3.11 上开发），仅标准库 + SQLite。
时区数据来自系统 IANA 数据库（`zoneinfo`）。

```
python3 -I -m unittest discover -s tests -v     # 全部测试
python3 -I tools/build_fixtures.py              # 重新生成示例包
```

## 终端入口

`./gtfs-store` 或 `python3 -I -m gtfs_store`。

```
gtfs-store [--db PATH] [--json] load       --feed-key K --edition E --package X.zip
gtfs-store [--db PATH] [--json] rejections --feed-key K [--edition E]
gtfs-store [--db PATH] [--json] status     --feed-key K --edition E [--days N]
gtfs-store [--db PATH] [--json] show       --feed-key K --edition E --table T [--limit N]
gtfs-store [--db PATH] [--json] expand     --feed-key K --edition E --start YYYY-MM-DD --end YYYY-MM-DD
gtfs-store [--db PATH] [--json] departures --feed-key K --edition E --date YYYY-MM-DD [--stop ID]
gtfs-store [--db PATH] [--json] days       --feed-key K --edition E --start … --end …
gtfs-store [--db PATH] [--json] list
```

`--json` 放在子命令之前。`show` 只读某一份 `(feed_key, edition)` 的规范基础表
（`agency`/`stops`/`routes`/`trips`/`stop_times`/`calendar`/`calendar_dates`/`frequencies`）。

退出码：`0` 成功；`1` 包被拒收（rejected）；`2` 用法/请求错误；`3` 展开被中断。

`--json` 可放在子命令前；所有输出均为 UTF-8，站点编号按原始字符串输出
（`007` 与 `7` 是不同站点）。

## 接收范围与校验

只接收这些成员，且只接受顶层文件（压缩包内子目录、绝对路径、`..`、
盘符、NUL、反斜杠一律拒绝）：`agency.txt`、`stops.txt`、`routes.txt`、
`trips.txt`、`stop_times.txt`、`calendar.txt`、`calendar_dates.txt`、
`frequencies.txt`。其他成员直接拒收（`unsupported_member`），不做静默忽略。

- 必需：`agency`、`stops`、`routes`、`trips`、`stop_times`；
  `calendar` 与 `calendar_dates` 至少有一个。
- 同名成员（含仅大小写不同的名字）→ `duplicate_member`；
  单成员 > 64 MiB、整包解压 > 512 MiB、压缩包 > 256 MiB → `size`。
- CSV：识别 UTF-8 BOM，按 RFC 4180 处理引号内逗号与换行；
  记录行号为物理起始行。坏 CSV/非 UTF-8 会带文件名报出。
- 编号是**不透明字符串**：不 trim、不补零、不转数字。
- `agency_timezone` 必填、全包一致、且必须能被 IANA 解析。
- 主键/唯一性：`stops.stop_id`、`routes.route_id`、`trips.trip_id`、
  `calendar.service_id`、`(trip_id, stop_sequence)`、`(service_id, date)` 不得重号。
- 外键：`trips.route_id`、`stop_times.trip_id/stop_id`、`frequencies.trip_id`。
- 星期值只能是 `0/1`，日期必须是真实日历日 `YYYYMMDD`，`start_date ≤ end_date`。
- `calendar_dates` 同一 `service_id/date` 出现两条（无论同型还是 add+remove）都是
  `conflict`，**不按文件顺序取最后一行**。
- `stop_times` 每站 `arrival_time`、`departure_time` 均必填，支持 `HH:MM:SS`
  且小时可大于 23。**不排序时间、不补齐缺时**。
- 每站 `arrival ≤ departure`，且下一站 `arrival ≥ 上一站 departure`。
  违反者**整条 trip 不可用于展开**：包仍然可用，问题写入 `trip_issues` 并出现在
  `status` 输出；基础表保留原始时刻（不修正），展开时跳过该 trip。
- `frequencies`：`headway_secs` 必须为正整数；同一 trip 的频率段不得相交
  （`[a,b)` 与 `[b,c)` 首尾相接合法）；`start_time ≤ end_time`；
  `exact_times` 缺省按 GTFS 默认 `0`。
- 明确不支持的语义会被点名拒绝，而不是猜测：
  `continuous_pickup`/`continuous_drop_off`（连续乘车/下车）、
  `stop_timezone` 与机构时区不同（逐站时区）、未知文件或未知列。

一个问题都不放过：校验会收集全部可识别问题，任何问题都会让整个包**拒收**，
不给它可用状态，也不改变已有版本（`load_rejections` 保留全部问题，
含文件、物理行号、字段名、记录编号）。

### 版本与摘要

`(feed_key, edition)` 唯一。保存的是两个摘要：

- `archive_sha256`：原始 ZIP 字节；
- `content_sha256`：成员名 + 成员内容的规范化摘要。

重送时按 `content_sha256` 比较：内容相同即幂等成功（基础表不动，即使 ZIP 被重新
打包、时间戳不同）；内容不同则冲突并拒绝，必须换一个 edition。不同 `feed_key`
之间即使内部编号完全相同也完全隔离；同一 feed 的不同 edition 基础表也不混用。

载入是单事务：校验失败或任何异常都回滚，读者不会看到半套基础表。

## 时间坐标契约（关键）

取**服务日当地正午**（`12:00:00`，机构时区），转成 UTC，再减 **12 个实际小时**，
得到该服务日的起点 `day_start_utc`；GTFS 时刻里的整数秒直接加在它上面：

```
instant(offset_seconds) = day_start_utc + offset_seconds
```

展示时把该 instant 转回机构时区，写出本地时间与当时的 UTC 偏移
（`departure_local`、`utc_offset`），并**始终另留 `service_date`**。
绝不把 `25:10:00` 交给普通时钟解析，也绝不逐站拼接本地日期。

这套契约让夏令时自然正确：服务日内的 UTC 偏移可以变化，即使发生跳时/回拨也不
需要做 ambiguous/nonexistent 的逐时刻选择。Europe/Berlin 2026 年示例：
2026-03-28 的 `25:10:00` 是 2026-03-29T01:10+01:00，而同一天的 `26:30:00` 是
2026-03-29T03:30+02:00（当地 02:30 不存在）；2026-10-24 的 `26:30:00` 是
2026-10-25T02:30+02:00（歧义时刻的第一次出现）。完整期望值见
`fixtures/expected.json` 的 `service_day_contract`。

## 服务日选择

先定服务日，再算 `service_id` 是否启用：

1. `calendar`：`start_date ≤ date ≤ end_date` 且当天星期位为 1；
2. `calendar_dates` 对该 `(service_id, date)` 覆盖上面的结论：`1` 添加、`2` 移除；
3. 仅由添加日提供服务的 service（没有 `calendar` 行）同样合法。

某日无有效 trip → 空结果。展开结果按日期逐日落库，**不会沿用昨天**。

## frequencies 展开

- 没有 frequencies 的 trip：按 `stop_times` 形成确定发车，`trip_instance_id = T:<trip_id>`。
- 有 frequencies 的 trip 只是模板：**模板本身不再额外输出一次**。
  `start_time` 视为模板首站发车，其余各站按“各自与首站发车的秒差”平移。
- `exact_times=1`：从 `start_time` 起按 `headway_secs` 逐次生成首站发车，
  **右端点不含**。每一班、每一站都有独立身份
  `F:<trip_id>:<freq_row>:<n>`，`n` 为段内序号。
- `exact_times=0`：只生成站点发车窗口
  `[start+offset, end+offset)` 与 `headway_secs`，身份为 `W:<trip_id>:<freq_row>`，
  `certainty='headway'`，不生成任何“具体车次”。窗口端点同样按站间偏移平移。
- 末班是否可以越过零点：允许。例如 `[23:30, 24:45)` 每 30 分钟，最后一班首站
  24:30，其第二站 24:50 已超出窗口末端仍保留（时间契约保证跨零点正确）。

## 展开的原子替换

`expand` 显式要求 edition 与起止日期，只重算该闭区间：

- 先在内存里算完所有服务日，再在**一个事务**里删除并重写这些日期的
  `departures` / `service_days` / `expansions` 行；
- 重复同一请求不增加行（已用行数核对）；
- 中断（异常、断电前回滚）后读者只能看到上一次完整结果（WAL 快照）；
- 中途失败可用环境变量 `GTFS_FAULT_EXPAND_AFTER_DAYS=N` 复现，用于测试；
  重做后与中断前结果逐行一致。

## 表结构

- `feeds`、`editions`（状态、时区、两个摘要、载入时间）
- `load_rejections`（文件、行号、字段、类别、消息）
- `trip_issues`（不可用于展开的 trip 及原因）
- `agency/stops/routes/trips/stop_times/calendar/calendar_dates/frequencies`
  —— 全部以 `(feed_key, edition, …)` 为键
- `expansions`、`service_days`（`day_start_utc`、时区、启用 service、计数、完成时间）
- `departures`（含 `service_date`、`trip_instance_id`、`certainty`、
  `departure_utc`、`departure_local`、`utc_offset`、
  `min/max_departure_seconds`、`headway_secs`）

`departures` 主键 `(feed_key, edition, service_date, trip_instance_id, stop_sequence)`，
所以同一请求重复展开不会产生重复行，且“每班每站”身份唯一。

## 示例数据与人工期望

`fixtures/expected.json` 是为 `dist/demo-v1.zip` 人工写下的预期结果，测试逐条核对。

| 演练项 | 位置 |
| --- | --- |
| 周末服务被移除、另日添加 | `WKND` 在 2026-03-29 移除、2026-03-30 添加；`SAT_REMOVED` 周六正常、`WKND` 在 2026-04-03 添加 |
| 只有 `calendar_dates` 的服务 | `WKND_ONLY_ADDED`（2026-03-30） |
| `007` 与 `7` 并存 | 两个不同站点，各自有发车 |
| 25 点以后发车 | `T_LATE` 25:10、`T_WKND_LATE` 26:30 |
| 非连续 `stop_sequence` | `T_WKDY` 10/20/30/40 |
| `exact_times=1` / `=0` | `T_FREQ_EXACT` / `T_FREQ_WINDOW`；`T_FREQ_DEFAULT` 缺省为 0 |
| 右端点恰好踩中 | `[09:30,12:00)` 与 `[12:00,14:00)` 相接，12:00 只由第二段生成 |
| 首尾相接频率段 | `T_ADJACENT` `[13:00,14:00)` + `[14:00,15:00)` |
| 最后一班越零点 | `T_MIDNIGHT` `[23:30,24:45)`，S2 末班 24:50 |

拒收包：`reject-overlap.zip`（相交频率）、`reject-missing-time.zip`（缺时间）、
`reject-foreign-key.zip`（外键不存在）、`quarantine-bad-timing.zip`
（时刻自相矛盾 → 包可用但该 trip 隔离）。

`other-operator.zip` 是与 demo 内部编号相同的另一运营方，用于核对隔离；
`demo-v2.zip` 用于核对同 feed 不同 edition 不混用。

## 端到端演练（已实际执行，非成功消息）

见 `docs/WALKTHROUGH.md`：其中记录了实际 SQL 查询结果、中断与重做后的行数、
拒收原因和隔离核对。
