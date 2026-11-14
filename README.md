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
python3 -I -m unittest discover -s tests -v     # 全部测试（含票价清分、GPS 驻留与匿名出仓）
python3 -I tools/build_fixtures.py              # 重新生成示例包
python3 -I tools/build_fare_fixtures.py         # 重新生成票价规则包与清分批次
python3 -I tools/fare_walkthrough.py            # 票价清分演练（手工账逐段核对）
python3 -I tools/build_dwell_fixtures.py        # 重新生成 GPS 驻留静态包与采样
python3 -I tools/dwell_walkthrough.py           # GPS 驻留演练（逐条证据与崩溃重开）
python3 -I tools/build_anon_fixtures.py         # 生成匿名出仓手工样本（跨两个 GPS 作业）
python3 -I tools/anon_walkthrough.py            # 匿名出仓演练（独立复核、崩溃重开、20 万采样测内存）
python3 -I tools/build_j1939_fixtures.py        # 生成 J1939 TP 手工抓包样本与期望
python3 -I tools/j1939_walkthrough.py           # J1939 重组演练（逐字节复核、崩溃重开）
python3 -I tools/build_rollup_fixtures.py       # 生成带权层级汇总的手工例子与错误用例
python3 -I tools/rollup_walkthrough.py          # 汇总演练（手写账逐格核对、独立枚举器、错误演示）
python3 -I tools/rollup_scale.py                # 10 万→20 万笔实测：峰值 RSS、格数、耗时
python3 -I tools/build_dictmerge_fixtures.py    # 生成三方字典合并的手工例子与手写预期
python3 -I tools/dictmerge_walkthrough.py       # 三方合并演练：分页审阅、左右/自定义决定、重排与互换稳定性、拒绝演示
python3 -I tools/dictmerge_scale.py             # 10 万→20 万行底稿实测：峰值 RSS、冲突数、耗时
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

# GTFS Realtime 接收通道（TripUpdate → SQLite）

在静态班表之上新增实时接收通道：把**已经下载好的** TripUpdate 二进制报文
（`FeedMessage`）整理成"当前通告 + 站点明细"。本通道只搬运、核对源观测：

- **不做**延误传播、**不做**预计到站计算、**不访问**网络；
- 解析用官方 `gtfs-realtime-bindings`（Protocol Buffers）。静态功能仍只需标准库；
  绑定未安装时自动使用 `vendor/` 里的未改动副本（见 `vendor/README.md`）。

```
python3 -I tools/build_rt_fixtures.py     # 用官方绑定生成 dist/rt/*.pb 样本
python3 -I tools/rt_walkthrough.py        # 实时演练（逐步断言，输出即文档）
python3 -I -m unittest discover -s tests -v
```

## 终端入口（实时子命令）

```
gtfs-store [--db PATH] [--json] rt-register  --source S --feed-key F --edition E
gtfs-store [--db PATH] [--json] rt-sources
gtfs-store [--db PATH] [--json] rt-feed      --source S --message X.pb
gtfs-store [--db PATH] [--json] rt-notices   --source S --now N --max-age N [--include-expired] [--entity ID]
gtfs-store [--db PATH] [--json] rt-receptions --source S [--limit N]
gtfs-store [--db PATH] [--json] rt-reception --id N [--blob OUT.pb]
```

`rt-feed` 退出码：应用 / 重复 / 迟到为 `0`；拒收 / 冲突为 `1`；用法错误为 `2`。

## 来源登记

`rt_sources` 把一条实时通道**永久**绑定到一份可用（ready）的静态
`(feed_key, edition)`：登记后不能换绑（同绑定重复登记是幂等空操作）；不同来源
绑定同一份班表、使用相同实体编号都互不影响。

## 报文契约与集合语义

- 头部：版本必须是 `2.0`；`incrementality` 必须显式填写；`timestamp` 必须为正数。
- 正文：只允许 `trip_update` 实体；实体编号非空且报文内不重号；`vehicle`/`alert`
  等其他类型、混合实体、损坏编码、未初始化的必需字段都留下拒收说明。
- `FULL_DATASET` 整体替换当前集合：遗漏的实体即撤回，空完整包 = 全部撤回。
- `DIFFERENTIAL` 只替换列出的实体；`is_deleted=true` 撤回对应编号（不得同时携带
  车次载荷，也不得出现在完整包里）。开局之前收到增量（尚无基线）须拒绝；
  空增量是心跳，只推进来源头部。
- 一次集合变更连同头部与接收结论在**一个事务**里落定：相互竞争的接收者被
  `BEGIN IMMEDIATE` 串行化，不会把两份报文揉成一个从未存在过的集合。

## 头部时戳与摘要账

头部时戳是来源的先后坐标。`rt_head_ledger`（成功账）记录每个**已应用**的
`(source, timestamp)` 的报文摘要：

1. 先查账：同样的包再来 → `duplicate`，返回原接收结论，不应用第二遍；
   同时戳不同字节 → `conflict`；
2. 再判断新旧：不新于来源头部 → `late`，只留迟到记录；
3. 校验失败的新报文**不进**成功账，修正后可用同一时戳再来；
4. 即使实体早已撤回，旧账仍在。

每一次到达（含拒收/冲突/迟到/重复）都在 `rt_receptions` 留账：原报文 BLOB、
SHA-256、头部时戳、处理类别、诊断；当前实体经 `reception_id` 反查原报文。

## 运行实例与结构化列

- 运行实例 = （运行日期 `start_date`、车次 `trip_id`、可选起始时钟 `start_time`），
  车次必须属于登记的班表；同一实例在当前集合里只能由一个实体代表——同份增量
  撤旧编号、用新编号接管实例可行，实体排列顺序不影响结果。
- `TripDescriptor.schedule_relationship` 只支持 `SCHEDULED` / `CANCELED`：
  取消是仍然有效的停开通告（可读、不带站点事件），撤回则是来源不再提供信息，
  二者读取结果不同。
- 每个 `StopTimeUpdate` 必须有站序且能在该车次找到；附带 `stop_id` 必须与静态
  对应；重复站序、无对应站序、对错车次、不支持的关系枚举都不得进入当前集合。
  站点关系限 `SCHEDULED` / `SKIPPED` / `NO_DATA`，后两种不携带到发事件。
- `arrival` / `departure` 的 `time`、`delay`、`uncertainty` 按 protobuf 字段存在性
  分别保存"有没有给"和整数值：`delay=0` ≠ 没有 delay，`time=0` ≠ 没有 time；
  `uncertainty` 不能为负。缺席字段一律存 NULL，不计算、不复制、不补齐；
  用新载荷替换实体时，本次缺席的可选字段同时清除。
- 未列出的子字段（`TripUpdate.vehicle`、`TripUpdate.delay` 等）不搬运、不落库。

## 新鲜度

默认读取只显示新鲜通告：`now` 与 `max_age`（必须为正）由调用方传入，实体年龄
按**最后一次真正被替换的报文头部时戳**计算，到期边界是
`now >= 实体时戳 + max_age`。更新其他实体或空增量都不刷新这条年龄。
`--include-expired` 另给含过期通告的读法（数据仍留库），但 `now` 落后于来源头部
会被拒绝——该功能不提供历史视图。完整包遗漏与显式撤回不等年龄到期。

## 表结构（新增）

- `rt_sources`（绑定、头部时戳、基线标记）
- `rt_receptions`（每次到达：BLOB、摘要、类别、诊断、原接收引用）
- `rt_head_ledger`（成功账：已应用的 `(source, timestamp)` → 摘要）
- `rt_entities`（当前通告：实例列、关系、实体自身时戳、计龄时戳、reception_id，
  实例唯一索引）
- `rt_stop_events`（站点明细：站序、站号、关系、到发事件的存在位与三个整数值）

## 实时演练与样本

`dist/rt/*.pb` 由 `tools/build_rt_fixtures.py` 用官方绑定真实生成（含 25 个
拒收样本）；`docs/RT_WALKTHROUGH.md` 记录 `tools/rt_walkthrough.py` 的实际输出：
A/B 开局后仅替换 A、停开后撤回、完整包清空、撤旧换号指向同一实例、缺席字段清掉
旧值与显式零读回、坏站序被拒后修正、旧包在实体消失后再次到达、同戳改包、恰好
到期但来源仍有心跳、两个来源同号互不影响、两名接收者争用同一来源（两种串行结局）
以及重开进程后的状态核对。

# 离线 GPS 驻留加工（GPS dwell processing）

在静态班表之上新增**离线** GPS 驻留加工：读采样文件，产出能逐条回查原始采样点
的**站点驻留事实**，供整理实际运行资料。它不匹配道路、不推断路线、不计算预计
到站，也不统计乘客或票款；全程仅 Python 标准库 + SQLite，是与静态/实时/票价
并列的独立模块（`gtfs_store/dwell.py`），不改动旧模块行为。

```
python3 -I tools/build_dwell_fixtures.py     # 生成手工设计的静态包与采样
python3 -I tools/dwell_walkthrough.py        # 端到端演练（输出即 docs/DWELL_WALKTHROUGH.md）
python3 -I -m unittest discover -s tests -v  # 含 37 条驻留专项测试
```

## 终端入口（驻留子命令）

```
gtfs-store [--db PATH] [--json] dwell-process  --samples S.csv --bindings B.csv \
               --enter-m E --exit-m X --min-dwell-s D --max-gap-s G
gtfs-store [--db PATH] [--json] dwell-jobs
gtfs-store [--db PATH] [--json] dwell-job      --job-id J
gtfs-store [--db PATH] [--json] dwell-run      --job-id J --run-id R
gtfs-store [--db PATH] [--json] dwell-dwells   --job-id J [--run-id R] [--stop-id S]
gtfs-store [--db PATH] [--json] dwell-evidence --job-id J --dwell-seq N
```

输入两个 CSV（表头必须逐字一致，UTF-8，支持 BOM 与 RFC4180 引号）：

- 绑定 `run_id,feed_key,edition,trip_id`：为每个 run 指定它可关联的静态版本与
  trip；不同 run 可以使用不同静态版本。重复绑定、空字段直接文件级失败。
- 采样 `run_id,sample_id,observed_utc,lat,lon`：`sample_id` 为 run 内唯一非空
  字符串；时间为整数 UTC 秒；WGS84 度，必须有限，经度 [-180,180]、纬度 [-90,90]。

### 从输入文件到证据的一组可执行命令

```
# 1) 静态版本入库（普通静态通道，坐标缺失的包仍可入库，由驻留层拦截）
for e in v1 v2 v3-bad-coords; do
  gtfs-store --db gtfs.db load --feed-key dwell --edition $e \
    --package dist/dwell/dwell-$e.zip
done

# 2) 一次完整加工（每次作业独立生成，不追加上次驻留）
gtfs-store --db gtfs.db dwell-process \
  --samples dist/dwell/samples.csv --bindings dist/dwell/bindings.csv \
  --enter-m 120 --exit-m 250 --min-dwell-s 60 --max-gap-s 120

# 3) 按 run / 站点查驻留明细
gtfs-store --db gtfs.db dwell-dwells --job-id 1 --run-id R2
gtfs-store --db gtfs.db dwell-dwells --job-id 1 --stop-id 007

# 4) 展开一条驻留依据的原始采样值、精确距离与来源文件行
gtfs-store --db gtfs.db dwell-evidence --job-id 1 --dwell-seq 1
```

## 候选站点与身份

候选站从**实际静态表**取该 trip 经过的**不同 stop_id** 及坐标（`stop_times`
JOIN `stops`，重复经过同一站只保留一个空间候选，不猜第几次途经的计划站序）。
任一候选坐标缺失、非有限或越界，**整个 run 不可加工**，诊断点名是哪个站
（如 `candidate stop 'CHARLIE' ... has missing, non-finite or out-of-range
coordinates`）。站点编号是不透明字符串，`007` 与 `7` 始终是两个站。

## 距离与空间索引

距离统一 Haversine，地球半径 6371008.8 m，**比较不做米级取整**；两个半径边界
均为包含关系（`<=enter_m` 发起、`<=exit_m` 延续）。每个点只在所属 run 的候选
站中查找，**不做点×站笛卡尔积**：

- 生产路径先经可查询的均匀经纬网格空间索引（按 enter 半径保守取盒，经度在
  0..360 归一化网格上折叠，跨 180 度经线两侧都能扫到；纬度盒触及极点时扫描
  全部已占用经度带），再对盒内候选做精确 Haversine；
- 最近者中选，完全同距按 stop_id 的 Unicode 码点顺序，不靠输入顺序；
- 每次作业在 `dwell_job_runs.index_json` 留下索引说明：网格大小、占用单元、
  **精确候选检查次数 `candidate_checks` 与单元扫描数 `cell_scans`**。400 候选、
  40 点的大资料只做 9 次精确检查，而笛卡尔积需要 16000 次；测试与演练另用
  **不走生产索引的全站穷举**逐点复核，候选完全一致。

## 可解释的驻留状态机

1. 空闲点找到 enter 内候选即开始**待确认**串；后续点连续选中同一候选、且每对
   相邻点时间差 `<=max_gap_s`，直到 `当前时间-第一点时间 >= min_dwell_s` 才确认；
   到达时间**回填为串内第一点时间**，不是确认时刻。
2. 待确认期间无候选或换候选，旧串作废，新点可开自己的串；短暂掠过不输出。
3. 确认后**锁定当前站**：只要距该站 `<=exit_m` 就延续，即使点已超出 enter_m 或
   附近另一站更近，也不改站、不重算到达。首次 `>exit_m` 结束，离开时间取此前
   最后一个内点时刻，原因记 `observed_exit`，并保存最后内点与首个外点的
   sample_id；该外点可以开启另一站的待确认串，但**不能同时作为两个站的支持点**。
4. 相邻时间差 `>max_gap_s` 先断开当前过程：已确认驻留以最后内点为最后观测时刻，
   原因 `gap_truncated`，离开时间为空；缺口后从空闲重新处理，不拼接停机前后。
5. 文件结束时仍在站内记 `open_ended`，离开时间同样为空；未达时长的待确认串
   不输出。`==max_gap_s` 仍连续，`==min_dwell_s` 即确认。

## 失败隔离与文件级失败

一个 run 的坏坐标、重复 sample_id、同一时间两点（即使坐标相同也拒绝整 run，
不用输入顺序打破冲突）、未登记绑定、无采样行，都只让该 run 失败并给出指回
**原文件物理行**的诊断，同文件其他合法 run 照常出驻留。缺少 run_id（无法归属）
或文件无法可靠解析（坏 CSV、表头不符）属于**文件级失败**，不产生任何作业。

## 落库、原子性与可回查

每次作业按完整输入独立生成，结果在**一个事务**里落库，作业对外可查之前记录与
证据必须一起提交；异常退出回滚，不会暴露缺证据的半条驻留（环境变量
`GTFS_FAULT_DWELL_AFTER_RUNS=N` 可复现中途崩溃）。失败重跑只是新建作业，不接续
游标、不实时更新旧作业。新表（全部带 `job_id`，与旧表并存）：

- `dwell_jobs`（来源文件与绑定的 SHA-256、四个参数、作业计数、创建时刻）
- `dwell_bindings`（本次实际使用的绑定及绑定来源行）
- `dwell_job_runs`（每 run 状态、诊断 JSON、索引参与说明 JSON）
- `dwell_samples`（全部原始采样值与来源行，含失败 run 的坏行）
- `dwell_facts`（run、静态版本、stop_id/原站名、到达/确认/最后观测/可空离开
  时刻、结束原因、支持点数、首尾证据 sample_id）
- `dwell_evidence`（逐点原始 lat/lon、来源行、精确距离与角色
  `arrival/confirm/inside/last_inside/first_outside`；一个点兼多个角色时每角色一行）

## 手工场景与独立复核

`dist/dwell/samples.csv` 与 `dwell-expected.json` 手工设计并记录期望，覆盖：
正常到站后离开；两站进入圈相交时先按最近选站、确认后不因另一站更近而跳站；
未满时长离开；恰到确认与间隔上限（含活动驻留上 `==max_gap_s` 连续、`+1` 截断）；
缺口前后分别成段；文件结束未离站；180 度经线两侧相邻与近极点候选；`007` 与 `7`
并存；乱序还原后结果不变；同批坏 run 与好 run 隔离。测试先用全站穷举核对候选，
再独立按手写时间线逐条比对状态、时刻、支持点与来源行，而非只核对条数。
`docs/DWELL_WALKTHROUGH.md` 是上述演练的真实转录。

# 离线票价清分管道（Fare clearing）

在静态班表之上新增**离线、只读**的应收复核管道：把已经发生的乘车段关联到静态
线路与上下车站、匹配票价条款，再按同一张票的换乘额度逐段复核合同应收。它不接
支付、不找最便宜路线、不按时刻表猜乘客坐过哪班车，也不用实时预测改写输入时刻。
全程只需 Python 标准库 + SQLite。

## 终端入口（票价子命令）

```
gtfs-store [--db PATH] [--json] fare-install    --package X.zip
gtfs-store [--db PATH] [--json] fare-packages   [--feed-key K] [--edition E]
gtfs-store [--db PATH] [--json] fare-products   --feed-key K --edition E --fare-version V
gtfs-store [--db PATH] [--json] fare-rules      --feed-key K --edition E --fare-version V
gtfs-store [--db PATH] [--json] fare-zones      --feed-key K --edition E --fare-version V
gtfs-store [--db PATH] [--json] fare-rejections --feed-key K --edition E [--fare-version V]
gtfs-store [--db PATH] [--json] fare-settle     --feed-key K --edition E --fare-version V --batch legs.csv
```

`fare-install` 退出码：成功/幂等重送为 `0`；整包拒收为 `1`；版本内容冲突等用法
错误为 `2`。`fare-settle` 对链级业务失败（无规则、混币种等）仍返回 `0`——其他链
正常出结果；只有批次本身解析不了时才返回 `1`。

## 规则包格式（项目自有契约，非 GTFS Fares）

ZIP 内四个顶层 JSON 成员，缺一不可、不接受目录成员：

- `manifest.json`：`feed_key` / `edition` / `fare_version`，绑定一份**已 ready**
  的静态版本；
- `stop_zones.json`：`stop_id -> zone_id`，一个站只能属于一个票区；
- `products.json`：非空字符串 `product_id`、金额字符串、币种、`validity_seconds`、
  `max_legs`、`covers`（必须包含自己）；
- `rules.json`：`rule_id`、整数 `priority`、`route_id`、`from_zone`、`to_zone`、
  `product_id`；三个条件用 JSON `null` 表示任意，空字符串 `""` 是永不命中的
  字面值，绝不通配。

金额只接受十进制定点字符串：CNY/USD 两位小数、JPY 零位小数；拒绝指数写法、
NaN、Infinity、负数、隐式舍入和超精度值；内部一律存整数最小货币单位，`0.00`/`0`
是有效产品。`validity_seconds` 与 `max_legs` 必须是正整数（JSON 布尔不算整数）。
重复编号、悬空覆盖/购票引用、未知字段、重复 JSON 键、映射站或规则线路在绑定静态
版本中不存在，都会在安装时拒绝整包，旧版本一行不改。

### 同优先级交叠预检

安装前按绑定版本的**实际线路集合**与本包出现的**票区集合**做条件集合交集：
同优先级、三列条件可同时成立的两条规则即冲突，输出冲突规则对与一个具体的
route/from_zone/to_zone 见证，即使两条规则指向同一产品也拒绝，不靠文件顺序
断案。不同优先级允许交叠（优先级本来就用来裁决）。不枚举站点对。

### 版本账

`(feed_key, edition, fare_version)` 唯一。同版本同内容重送幂等成功；同版本不同
内容直接报冲突，必须换新版本号。拒收也留痕（`fare_rejections`），且不占用 ready
名称之外的语义；另一个版本的安装永不改动历史已安装包。

## 批次与清分语义

批次 CSV 列：`journey_id, leg_index, trip_id, board_sequence, alight_sequence,
board_utc, alight_utc`（UTC 整数秒）。行可乱序；每链 `leg_index` 必须唯一且从 1
连续。静态表只用于核对身份与顺序：trip 必须存在、两个站序必须存在且上车站序
严格小于下车站序（不要求连续，`007` 原样保留）、上下车站都要有票区映射、
段内下车不早于上车、下一段上车不早于上一段下车。不检查运营日、不重展开班次。

命中的最高优先级规则决定基础产品；无规则则该链失败。一张链的基础产品必须同
币种，否则整链拒绝，不做相加或换汇。票的状态机只看**最近一次购买**的票：

1. 第一段购买基础产品，从上段上车秒计票龄，已用段数 1；
2. 下一段基础产品在覆盖集合内、上车时刻**严格小于**购票时刻+有效秒、已用段数
   **小于**上限，则收费 0 并消耗一段——不续期、不恢复额度、不回头挑旧票；
3. 否则重买并重新计时计数，原因列明 `first_purchase` / `expired` /
   `quota_exhausted` / `product_not_covered`，多个同时成立全部列出。

恰好到期或额度恰好用完都要重买；上车时可用即可，下车越过到期点不补收。失败链
只给可定位到输入行/段号的原因，不返回看似可结算的部分总额，批内其他链照常。
一次读取固定一份规则包与一份静态快照，处理中不混版本。

## 输出与表结构

成功链给币种与总应收（最小单位整数 + 定点文本），逐段给静态线路、上下车站与
票区、命中规则、基础产品、实际付费产品、沿用购票段号、收费、票到期秒与用后
段数。新表：`fare_packages`、`fare_rejections`、`fare_stop_zones`、
`fare_products`、`fare_product_covers`、`fare_rules`，全部以
`(feed_key, edition, fare_version, …)` 为键，与静态/实时表并存且互不影响。

## 示例与人工期望

`dist/fare-v1.zip`、`dist/fare-v2.zip` 与三个整包拒收样本由
`tools/build_fare_fixtures.py` 生成，批次在 `dist/fare-batch.csv`。
`docs/FARE_WALKTHROUGH.md` 记录 `tools/fare_walkthrough.py` 的真实输出：手工逐段
账与引擎逐字段比对、独立极简状态机二次复算、拒收见证、版本冲突、v1/v2 隔离与
重开库逐字节复算。

# 匿名化出仓：整段时空格序列的 k-车辆匿名（Anonymized egress）

在 GPS 驻留作业之上新增**只出不进**的匿名化出仓通道：把已入库采样粗化成
时空格序列，按**整段有序序列**分组，只交付至少有 `k` 个**不同车辆主体**
共同支持的路径。它不是逐位置凑人数——即使一条行程经过的每个格都很常见，
它的完整顺序仍可能独一无二，而稀有组规则保护的正是这种唯一性。全程仅
Python 标准库 + SQLite，独立模块 `gtfs_store/anon.py`，不改动驻留判定与
任何旧模块行为。

```
python3 -I tools/build_anon_fixtures.py     # 生成手工样本、清单与期望（dist/anon/）
python3 -I tools/anon_walkthrough.py        # 端到端演练（输出即 docs/ANON_WALKTHROUGH.md）
python3 -I -m unittest discover -s tests -v # 含 28 条匿名出仓专项测试
```

## 数据来源与成员清单（任何一项不合格就整次拒绝）

采样**只**从已有 GPS 作业表读取（`dwell_job_runs` + `dwell_samples` 的
原始 `observed_utc/lat/lon/sample_id/source_line`）：不重新导入定位文件，
不读取、不派生任何驻留事实，静态表与实时通告一律不参加。

请求通过成员清单 CSV 精确指定本次共享的运行实例，表头必须逐字一致：

```
job_id,run_id,vehicle_unit
1,R1,VA
2,R4,VD
```

- 每个 `(job_id, run_id)` 实例**只能登记一次**；实例必须存在、
  `dwell_job_runs.status='processed'` 且有采样行，否则整次请求拒绝
  （失败 run、零采样 run、未选择的作业都不参加，绝不悄悄跳过）。
- `job_id` 为不带符号、不带前导零的十进制整数；`run_id` 与
  `vehicle_unit` 是**不透明字符串**，不 trim、不补零、不转数字：
  `"007"` 与 `"7"` 始终是两个车辆标识。同一辆车的多个 run 可共用一个
  `vehicle_unit`；组支持数按不同 `vehicle_unit` 计数，**不按 run 数
  （车次数）代替车辆数**。
- 任何空字段、重复实例、不存在的引用或不符的表头都带清单物理行号报错，
  退出码 `2`，且不留下任何匿名作业记录。

## 网格级别参数（不从数据学习任何边界）

调用方给出整数 `k >= 2` 和从细到粗的级别 JSON（数组，或
`{"levels": [...]}`），每级含两个正整数：

```json
[
  {"cell_microdeg": 500000, "bucket_s": 60},
  {"cell_microdeg": 1000000, "bucket_s": 120}
]
```

校验规则：`cell_microdeg` 必须整除 `180000000`；后一级空间宽度是前一级
的整数倍、时间宽度也是前一级的整数倍；每级至少有一项增大，完全相同的
相邻级别被拒绝。

坐标从库里按 REAL 读出后以 `Decimal(str(value))` 重建（按浮点的文本而
非二进制噪声取整），格号与时段号严格按下列契约计算：

```
lat_cell = floor((纬度 + 90)  * 1_000_000 / cell_microdeg)
lon_cell = floor((经度 + 180) * 1_000_000 / cell_microdeg)
bucket   = floor(observed_utc / bucket_s)
```

- 经度 `+180` 与 `-180` 等同（格号 0 的接缝）；纬度 `+90` 落在最北一格，
  不会落到网格之外。
- 时间对负整数秒同样向负无穷取整（`-30/60 -> -1`、`-60/60 -> -1`、
  `-61/60 -> -2`）。
- 每个 run 先按时间排序，映射成 `(时段号, 纬度格号, 经度格号)` 序列，再
  **折叠相邻且三项全同**的元素；被其他元素隔开后再次出现的相同元素保留。
  不对格号集合排序，不丢弃路径顺序、长度与重复经过。

## 逐级提升与整组舍弃

从最细级别开始，**每一级都从原始采样重新生成**每个 run 的完整序列
（上一级已折叠的序列永远不会被当作输入复用）。序列文本完全相同的 run
归为同组；组支持数 = 成员中不同 `vehicle_unit` 的数量。

1. 若该级**所有**组支持数都 `>= k`，选中该级并停止；
2. 只要还有一个稀有组，本次**全部成员一起**提升到下一级重新分组——各组
   不能私自选级；
3. 到最后一级仍有稀有组时，只把这些**整组**（组内全部行程一起）舍弃，
   其余组照常交付。

每一级都在内部账本记录组数、稀有组数、保留/舍弃的行程数与不同车辆数；
这些是内部核对资料，`anon_jobs` 的汇总不会假装每个输入都被发布。

## 终端入口

```
# 加工（成员清单 + k + 级别文件）；成功 0，请求/清单/级别非法 2
gtfs-store [--db PATH] [--json] anon-process --manifest M.csv --k 2 \
               --levels-file levels.json
gtfs-store [--db PATH] [--json] anon-jobs                         # 内部：作业列表
gtfs-store [--db PATH] [--json] anon-job      --anon-job-id N     # 内部：分级账本与组状态
gtfs-store [--db PATH] [--json] anon-export   --anon-job-id N     # 外部：唯一公开读取入口
gtfs-store [--db PATH] [--json] anon-group    --anon-job-id N --public-group-no G
gtfs-store [--db PATH] [--json] anon-member   --anon-job-id N --job-id J --run-id R
```

### 外部入口（固定列，对象全新构造）

`anon-export` 只返回选中级别、`k` 与每个保留组的公开组号、粗化后的完整
格序列、行程数和不同车辆数。公开载荷逐字段新建，列固定为：

```
public_group_no, level_index, cell_microdeg, bucket_s,
trip_count, vehicle_count, sequence
```

不含 `run_id`、`vehicle_unit`、`job_id`、`sample_id`、精确坐标、原始 UTC
秒、来源文件/行号或任何内部组号/哈希；公开组号只在本次加工内有意义。
代码不复用内部行对象再靠调用者删字段，测试与演练实际扫描公开输出确认
没有夹带内部标识。

### 内部核对入口（不出库给外部）

- `anon-group` 展开某公开组的全部成员（清单行号、来源 job/run、车辆标识
  与状态），并回查每个成员在 `dwell_samples` 里的原始来源行；
- `anon-member` 解释某个成员为何被保留（在第几级、哪个公开组、组内不同
  车辆数与 k 的关系）或为何随整组被舍弃（点名稀有组与支持数）；
- `anon-job` 给出每一级的组数、稀有组数与保留/舍弃计数，可核对公开数与
  舍弃数之和等于输入实例数。

## 原子性、独立编号与崩溃重开

校验与磁盘分级全部完成后才开启主库的 `BEGIN IMMEDIATE` 写事务；作业头、
分级账本、组、组序列、成员审计行与固定列公开行在同一事务提交。环境变量
`GTFS_FAULT_ANON_AFTER_GROUPS=N` 可在写出 N 个组后触发**真实进程退出**
（`os._exit`，无清理、无异常处理）：WAL 恢复在新进程回滚整个事务，读者
看不到半个分组，也看不到缺成员审计行的组；重开后重做得到全新的独立
`anon_job_id`，不要求幂等复用，也不恢复上次游标。

## 内存边界与有界批处理（生产路径）

Python 端任何时刻都不会同时持有全部采样、全部完整序列或任何组的车辆
集合：

- 排序、分组、`COUNT(DISTINCT vehicle_unit)` 全部下推到一个**磁盘上的
  SQLite 临时库**（系统临时目录，绝不放在数据库交付目录旁）；临时库
  `page_size=1024`、页缓存上限约 2 MiB、`temp_store=FILE`，聚合与排序
  超预算即落盘；
- Python 只保留**当前一个 run** 的序列构造工作区（逐点流式读取、相邻
  折叠后即追加文本），以及写出阶段**当前一个输出组**所需的行；插入按
  500 行/批 flush，不积累整批数据；
- 实测（见 `docs/ANON_WALKTHROUGH.md`）：20 万采样与加倍后的 40 万采样
  相比，子进程峰值 RSS 仅变化约 2 MiB（26 -> 28 MiB），增长全部落在磁盘
  临时库（约 22.7 -> 44.0 MiB），即内存不随输入总量增长。

## 手工场景与独立复核

`dist/anon/`（由 `tools/build_anon_fixtures.py` 生成，17 个 run 分布在两个
GPS 作业）与 `anon-expected.json` 覆盖八个可看懂分组过程的场景：同车多趟
不够 k；每格人数够而整条路径不够；路径逆序、非相邻重复与序列前缀不能混
组；细格不够而粗格合并成功；一个稀有组促使所有成员换级；最后仍稀有的整
组舍弃；负时间与格边界（含 +90 最北格、±180 接缝）；跨两个作业的成员。
演练里的独立小实现自带一套 Decimal 取整、相邻折叠与分组逻辑（不 import
生产模块），逐字段核对真实 CLI 的分级账本、公开行与舍弃解释。

## 威胁范围（明确不保证什么）

不建设账号系统；威胁模型仅针对拿到公开输出的合作方。本通道**只保证本次
加工内**每个发布的整段序列至少有 `k` 个不同车辆主体支持。它**不是差分
隐私**、不添加噪声，也不抵抗多次/交叉交付之间的关联分析；这些限制在
模块文档与演练输出中均有明示。

# 离线 J1939-21 传输层整理（CAN TP.CM/TP.DT 重组）

与 GPS、班表、匿名化并列的独立离线入口：把诊断总线抓包 CSV 还原为可信的
完整应用载荷，落定**批次 → 原帧 → 传输 → 完整消息**四层关系。不接入硬件、
不解释故障码、不从任何旧加工结果推算诊断消息。完整契约见
`docs/J1939_WALKTHROUGH.md`，实现见 `gtfs_store/j1939.py`。

## 支持范围（明确收窄的子集）

- 输入为 UTF-8 CSV：`bus,timestamp_ns,can_id,data_hex`。bus 原样非空；
  时间为非负整数且全文件非递减（同刻按行序）；can_id 恰好 8 位十六进制
  29 位扩展标识（前导零原文保留）；data_hex 恰好 8 字节。倒退时间、缺列、
  多列、坏字段使**整份文件不可加工**，不把损坏前缀当成批次。
- 标识符拆 priority/reserved/data_page/PF/PS/source_address；reserved 必须
  为 0。PF<240 时 PS 为目的地址且 PGN 低字节为 0；PF>=240 时 PS 属于 PGN、
  目的地址 255。TP.CM=`0x00EC00`、TP.DT=`0x00EB00`，且只接受 data_page=0；
  其他 PGN 原样登记为普通帧，不参与重组、不靠名字猜地址。
- BAM（广播，目的 255、b5=255）与点对点 RTS/CTS/EOM ACK/Abort。公告总长
  9..1785 字节、包数等于 `ceil(总长/7)`；目标 PGN 为 18 位、PDU1 低字节
  为 0 且不能是 TP PGN。
- 通道按 `(bus, 源, 目的)` 区分，同一发送端对两个接收端不串包；DT 不带
  PGN，不按 PGN 开并行通道。合法新公告替换旧通道（旧传输记 `replaced`
  不发布）；非法公告只记拒绝、不破坏旧传输。
- DT 按包号 1..N 收，末包只取总长需要的字节；同号同字节记重复帧（不重拼、
  不占许可、不刷新时限），同号改内容即失败；无通道、越界、跳包都有明确
  诊断，缺包不能跳过拼成功。
- 点对点严格流控：CTS 许可数 1..RTS b5、窗口不越总包数、起始须为下一应收
  包、仅等待 CTS 时可接受；窗口收完再等下一份许可，窗口外 DT 判失败；
  暂停许可与重新授权报不支持。齐包后等 EOM ACK，**只有合法 ACK 才发布**；
  Abort 原因码原样保存，任一端都可终止；未知控制字节报不支持。
- 时限：每条合法行先让同 bus `deadline<=当前时间`（相等即超时）的活动
  传输超时；普通帧也推进本 bus 时间，另一 bus 不影响。data/control 两个
  gap 均为正整数，重复帧不刷新。文件结束未完成记 `eof_incomplete`，不
  填零、不借会话补全。失败通道可被新公告复用，历史成功消息不被撤回。

## 终端入口（J1939 子命令）

```
gtfs-store [--db PATH] [--json] j1939-import    --capture X.csv \
               --data-gap-ns D --control-gap-ns C
gtfs-store [--db PATH] [--json] j1939-batches
gtfs-store [--db PATH] [--json] j1939-messages  [--batch N] [--bus B]
gtfs-store [--db PATH] [--json] j1939-message   --message-id N
gtfs-store [--db PATH] [--json] j1939-transfers [--batch N] [--status S]
gtfs-store [--db PATH] [--json] j1939-transfer  --transfer-id N
gtfs-store [--db PATH] [--json] j1939-events    [--batch N]
gtfs-store [--db PATH] [--json] j1939-trace     --message-id N --start A --end B
```

消息可查 bus、双方地址、应用 PGN、BAM/RTS、起止时刻、长度与原始载荷
BLOB；失败传输可查状态变化、致败控制/包、缺失包或确认；RTS、各 CTS、ACK
都能按传输取回原始八字节。`j1939-trace` 对左闭右开载荷区间返回相交的 DT
原文件行、帧内偏移与载荷内范围，跨包区间返回多帧。

## 失败批次仍可查什么 / 退出码

协议失败与成功传输同批落定：失败/超时/中止/替换/EOF 传输不产生消息行，
但已收包、控制帧、事件账本、失败原因、缺失包/ACK 全部可回查；同批成功
消息不受影响。`j1939-import` 在本批并非全部成功时以**非零**告知（不只
打印成功条数）；文件级损坏退出非零且不写任何批次。

## 原子性、独立批次与崩溃重开

整份输入先完整校验，再在单个 `BEGIN IMMEDIATE` 事务内写全部四层结果；
`GTFS_FAULT_J1939_AFTER_FRAMES=N` 注入真实 `os._exit` 后，未提交 WAL 由
新进程恢复回滚，读不到缺字节来源的成功消息。每次导入独立批次，不幂等
复用、不追加上次游标。

## 手工样本与独立复核

`dist/j1939/`（含七类文件级损坏样本 `dist/j1939/bad/`）覆盖 BAM 与两轮
CTS 点对点、双接收端交错、DT 重传、同号改内容、窗口外数据、错误/缺失
ACK、公告替换、时限恰好相等、只推进另一 bus、Abort、不支持/孤立控制与
EOF 未完成。`tools/verify_j1939_output.py` 只用标准库读真实输出库，逐字节
重建载荷并逐区间核对来源，不 import 生产代码：

```
python3 -I tools/build_j1939_fixtures.py
python3 -I tools/j1939_walkthrough.py
python3 -I tools/verify_j1939_output.py --db <演练库>
python3 -I -m unittest discover -s tests      # 含 tests/test_j1939.py
```

# 带权层级汇总：两个具名维度 × 多粒度精确有理数指标（Rollup）

运营分析员交一份整理好的运营事实（JSONL）和一套分类关系（JSON 配置），
选几种汇总粒度，就能查询各格指标，并下钻看某笔事实按多少比例贡献给
这个格。解决两个老问题：一条线路按比例分属两个类别、同一辆车出现在
多个小计时，**小计不能直接相加**——每个格独立保存五个精确指标，车辆
去重永远在该格内部完成，均值永远是总量/总权而不是细格均值的平均。

事实只来自用户整理好的文件：不从班次、GPS、匿名路径或 CAN 会话推导，
不处理增量修订；一次构建产出一个全新的自包含结果库，已有结果库绝不
覆盖。详见 `docs/ROLLUP.md`（配置/事实契约、落盘结构、去重方式、
复杂度与批次缓存设置）。

## 终端入口（汇总子命令；不使用主 --db）

```
gtfs-store rollup-build  --config C.json --facts F.jsonl --out R.db \
                         [--batch-size N] [--cache-kib K]
gtfs-store rollup-info   --result R.db           # 构建元数据与粒度清单
gtfs-store rollup-grains --result R.db           # 各粒度与格数
gtfs-store rollup-cells  --result R.db --grain G \
    [--d1-kind member|null|all [--d1-value V]] \
    [--d2-kind member|null|all [--d2-value V]] [--limit N] [--offset M]
gtfs-store rollup-drill  --result R.db --grain G \
    --d1-kind K [--d1-value V] --d2-kind K [--d2-value V] [--offset M] [--limit N]
gtfs-store rollup-fact   --result R.db --line N  # 按物理行号读回事实
```

- 坐标三种 `kind` 永不混淆：`member`（真实成员，编号 `ALL`、`007` 也是
  普通成员）、`null`（来源未分类，沿每级仍为 null，不进根）、`all`
  （粒度去掉了该维度）。筛选时真实 null 与 ALL 分别可选。
- 每格五指标：`row_weight`/`value_weight`/`amount_sum`/`amount_mean`
  为约分有理数 `{"num","den"}`（零固定 0/1，抵消得 0 不变 null，无金额
  时 sum/mean 为 null），`vehicle_count` 为整数（不乘系数）。
- 下钻按源行号递增分页，响应给出该格完整贡献行数与总指标；同一格同一
  源行只有一条合并系数。
- 构建失败非零退出且不留可查询的残骸；结果库已存在时明确拒绝覆盖；
  新进程只凭结果库即可查询。

## 手工例子（预期值手写，不用产品输出反推）

`dist/rollup/manual-*`：L 以 1/2、1/2 分到 A、B；A→X，B 以 1/2、1/2 分到
X、Y，因此 L 在 X/Y 级为 X 3/4、Y 1/4；X/Y 再到唯一根。两笔 L 事实（同一
车辆 v，金额 10 与 20）+ 一笔第一维为 null 的事实：

- X 格：row_weight=3/2、amount_sum=45/2、amount_mean=15、vehicle_count=1；
- Y 格：1/2、15/2、15、1；
- A/B 细格车辆数 1+1 不等于根格的 1；根格 row_weight=2 而 ALL 格为 3
  （null 事实不进根）。

`dist/rollup/stress-*` 把两维同时分叉、多路径重合、同车跨叶、两笔完全
重复、null 车辆与空字符串车辆、全 null 金额、负数抵消（精确 0/1）、
超过 64 位的金额、合法成员编号 `ALL` 与 `007` 全部纳入实际运行核对。

## 复现构建、筛选、下钻与全部检查

```
python3 -I tools/build_rollup_fixtures.py     # 生成 dist/rollup/ 全部输入与手写预期
./gtfs-store --json rollup-build --config dist/rollup/manual-config.json \
    --facts dist/rollup/manual-facts.jsonl --out /tmp/manual.db
./gtfs-store --json rollup-cells --result /tmp/manual.db --grain trunk_by_depot \
    --d1-kind member --d1-value X
./gtfs-store --json rollup-drill --result /tmp/manual.db --grain trunk_by_depot \
    --d1-kind member --d1-value X --d2-kind member --d2-value D1 --limit 1
python3 -I tools/rollup_walkthrough.py        # 逐格核对+独立枚举器+错误演示+覆盖拒绝
python3 -I tools/rollup_scale.py              # 10 万→20 万笔：RSS、格数、耗时
python3 -I -m unittest discover -s tests      # 含 tests/test_rollup.py（50 个用例）
```

演练中的参考枚举器（`tools/rollup_walkthrough.py` 与
`tests/test_rollup.py` 内各一份）逐条枚举原始路径暴力求和，不 import
`gtfs_store.rollup` 的任何汇总函数；规模实测里 10 万笔（约 90 万贡献行）
峰值 RSS ≈ 65 MB，输入翻倍后 ≈ 66 MB、耗时 ×1.94，事实、贡献行与车辆
集合全程留在 SQLite。

# 关系数据三方合并：底稿 + 两份修订稿 → 工作库 → 定稿字典（DictMerge）

同一份基础字典交给两个团队核对，拿回两份修改稿：有人改显示名称、有人补
分类、有人删整行。直接覆盖会丢掉另一边的工作，按主键全外连接又分不清
“只有一边改过”和“两边意见不同”。本工具对三份 **UTF-8 JSONL**（每行一个
对象）按调用方给定的固定主键做**关系型三方合并**：先比较出自动通过的行
与真正的冲突，人工逐项决定后，只有所有冲突都被正确处理才写出可入仓的新
字典。它不是文本 diff 或 Git 包装器，不读班表、实时通告、GPS、CAN 或汇总
格，也不维护长期修订服务。详见 `docs/DICTMERGE.md`（数据表、类型与缺席
编码、查找复杂度、批次设置）。

## 两步操作与终端入口（不使用主 --db）

```
# 1) 比较：三份 JSONL + 主键定义 -> 全新自包含工作库（已存在即拒绝）
gtfs-store dict-compare --base B.jsonl --left L.jsonl --right R.jsonl \
                        --key K1 [--key K2 ...] --out work.db \
                        [--batch-size N] [--cache-kib K]
# 分页审阅（每页都给完整总数，不只是当前页）
gtfs-store dict-info       --work work.db
gtfs-store dict-auto       --work work.db [--limit N] [--offset M]
gtfs-store dict-conflicts  --work work.db [--limit N] [--offset M]
gtfs-store dict-conflict   --work work.db --id <conflict_id>   # 三边原值/存在性/来源行号
gtfs-store dict-key        --work work.db --key V1 [--key V2 ...]
# 2) 定稿：工作库 + 人工决定 JSONL -> 最终字典（无冲突时空决定文件合法）
gtfs-store dict-finalize   --work work.db --decisions D.jsonl \
                           --out final.jsonl [--report report.jsonl]
```

## 合并语义要点

- 主键分量为非空字符串，原样比较（`"007"` ≠ `"7"`，不裁空格、不去前导
  零）；复合键分量各存一列、边界有意义，不拼接。缺主键、重复主键、重复
  JSON 成员、空行、坏 JSON 都是整次输入错误。
- 字段**缺席**是独立状态，与显式 `null`、`""`、`0`、`false` 都不同；值限
  字符串/整数/布尔/null，拒绝小数、数组、对象，布尔与整数不混同；整数
  精确保留有符号 128 位范围。
- 两边都保留的行按字段独立合并：两边相同取它；一边等于 base 取另一边；
  其余才是字段冲突——一边改名、另一边补分类可自动合并；一边删字段另一
  边未动自动合并，但删字段对上显式设 null 必须冲突。
- 行级规则：两边都删或“一边删、另一边与 base 完全相同”自动删除；一边删
  而另一边改过则是 **delete_modify** 整行冲突；两边新增同键但内容不同是
  **add_collision** 整行冲突，不把两个不同新对象凑成一行。整行冲突不再
  重复生成字段冲突，冲突行里互不冲突的字段仍给自动建议。
- 冲突身份是对“导致冲突的三个状态”（类别+键+字段+三边带类型状态，无序
  编码）的 SHA-256，与来源行号无关：文件重排身份不变，左右互换摘要不变；
  决定文件带同一状态指纹，冲突内容变了旧决定报过期。
- 决定可表达选哪一边、custom 第三值（`{"value":...}` 或
  `{"missing":true}` 删列）、整行 `delete` 或完整 custom 行（必须保留相同
  主键并通过类型检查，不能选不存在的行版本）。未知身份、重复身份、类别
  不符、坏自定义值、漏决定全部拒绝，绝不默默替用户选边。
- 最终文件按复合键各分量的 Unicode 码点序排列，每键一行；字段删除与行
  删除真实体现；定稿说明按主键记录自动接受与冲突决定，不写单元格级长
  报告。坏决定或写出失败非零退出且原有输出字节不变。

## 手工例子与可复跑演示

`dist/dictmerge/`（由 `tools/build_dictmerge_fixtures.py` 生成）的底稿含
复合键、`007` 与 `7`、显式 null、缺席列、`0` 与 `false`、超 64 位整数；
左边改名称、右边补分类，证明两边贡献都保住，并手工列出五个场景的预期：
同值双改、删除对未改、删除对修改、新增相撞、字段删除对设 null。

```
python3 -I tools/build_dictmerge_fixtures.py     # 生成 dist/dictmerge/ 输入与手写预期
python3 -I tools/dictmerge_walkthrough.py        # 分页审阅→左右/自定义决定→逐行逐字段核对
                                                 # 重排+左右互换身份稳定；五类拒绝且旧输出不变
python3 -I tools/dictmerge_scale.py              # 10 万→20 万行：RSS、冲突数、耗时
python3 -I -m unittest discover -s tests         # 含 tests/test_dictmerge.py（45 个用例）
```

演练与测试中的参考合并器各有一份独立纯字典实现，不 import
`gtfs_store.dictmerge` 的任何合并函数。规模实测：10 万行底稿比较峰值
RSS ≈ 52 MB，翻倍到 20 万行后 ≈ 62 MB（×1.19）、耗时 ×2.14 线性；行、
主键与冲突全程留在 SQLite，Python 只持有当前行/当前键与一个有界批次。
