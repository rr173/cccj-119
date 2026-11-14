# 离线 J1939-21 传输层整理（TP.CM / TP.DT 重组）

把车载诊断总线的原始 CAN 扩展帧抓取 CSV，离线还原为可信的完整应用载荷，
并把**批次 → 原帧 → 传输 → 完整消息**四层关系全部落库。它与静态 GTFS、
实时通道、GPS 驻留和匿名化加工并列，是一个独立入口：不从任何旧结果推算
诊断消息，不解释具体故障码（SPN/FMI），也不接入任何真实硬件。

只实现一个明确、收窄的 J1939-21 子集；不声称覆盖整个标准。

## 交接文件契约（文件级校验，整份不通过即整批拒绝）

UTF-8 CSV，表头必须恰好是：

```
bus,timestamp_ns,can_id,data_hex
```

- `bus`：非空原样文本（不 trim、不规范化）。
- `timestamp_ns`：非负整数字面量（无符号、无空白、无科学计数法）；文件按
  时间非递减排列，同刻以文件行次序为准，任何倒退都拒绝整份文件。
- `can_id`：恰好 8 个十六进制字符（大小写均可），表示 29 位扩展标识符；
  原始文本（含前导零）原样保存，解析值另存为整数。
- `data_hex`：恰好 16 个十六进制字符，即 8 字节。
- 缺列、多列、空 bus、坏时间、坏 can_id、坏 data、非 UTF-8 都使**整份文件
  不可加工**——不会把损坏位置之前看似正常的一小段当成一个批次落库。

## 标识符与 PGN

29 位标识符拆为 priority(3)、reserved/EDP(1)、data_page(1)、PF(8)、PS(8)、
source_address(8)。

- `reserved` 位必须为 0；为 1 的帧登记为 `rejected` 原帧并给出
  `identifier_rejected` 诊断，不参与任何传输。
- PF < 240（PDU1）：PS 是目的地址，PGN 低 8 位为 0；PF >= 240（PDU2）：
  PS 属于 PGN，目的地址视为 255。
- TP.CM = PGN `0x00EC00`，TP.DT = PGN `0x00EB00`；二者**只接受
  data_page=0**。data_page=1 的帧即使编号碰撞也只作普通帧登记。
- 其他 PGN 一律登记为普通帧（`ordinary`），不参与长消息重组，也不靠名字
  或帧排列猜地址。

## 支持的传输语义（子集）

活动传输以 `(bus, 源地址, 目的地址)` 区分。同一发送端向两个接收端的传输
互不串包；TP.DT 不带目标 PGN，因此绝不按 PGN 另开并行通道。

### 公告（TP.CM）

8 字节按 b1..b8 命名；b6..b8 是小端 18 位目标应用 PGN，必须满足 PDU1
低字节为 0 且不能是 TP.CM/TP.DT。b2..b3 是小端总字节数，b4 是总包数；
要求总长 9..1785 字节且包数等于 `ceil(字节数/7)`。

- BAM（b1=0x20）：目的地址必须 255、b5=255；广播不等待接收端控制。
- RTS（b1=0x10）：目的地址不能 255；b5 是单次 CTS 授权的最大包数，
  范围 1..总包数。
- 字段非法的公告只记 `announce_rejected`，**不破坏**通道上的旧传输；
  合法新公告占用已有通道时，旧传输记为 `replaced` 且不发布，新传输从
  第 1 包开始。

### 数据（TP.DT）与重复帧

b1 是包号，其余 7 字节是数据，按 1..总包数接收；最后一包只取公告总长
需要的字节，填充字节不进应用载荷。

- 已收包号再次出现且 7 字节完全相同：记 `duplicate_packet`，只保留关联、
  不二次拼载荷、不消耗 CTS 名额、**不刷新等待时限**。
- 同号内容不同：`dt_seq_conflict`，传输立即失败。
- 无活动通道（`dt_without_transfer`）、包号越界（`dt_seq_out_of_range`）、
  跳过下一应收包（`dt_seq_gap`）都有明确诊断；缺包绝不能跳过拼成功。

### 点对点流控（RTS/CTS/EOM ACK/Abort）

控制帧方向与 RTS 相反，处理时交换源/目的地址定位传输，绝不当成接收端
新发起的传输；找不到匹配活动传输时记 `orphan_control`。

- CTS（0x11）：b2 本次许可数、b3 起始包号、b4=b5=255。许可数须 1..RTS 的
  b5，窗口不得越过总包数，起始包号必须等于当前下一应收包号；只有处于
  等待 CTS 状态才接受新 CTS。窗口内按许可收包，收完再等下一份许可；
  窗口外擅自到达的 DT 以 `dt_outside_window` 判失败。
- 暂停许可（许可数 0）与从已收包号重新授权明确报 `cts_unsupported`。
- EOM ACK（0x13）：b2..b3 确认字节数、b4 确认包数、b5=255，方向/PGN/
  总长/包数都要与公告一致。齐包后进入等待确认；**只有收到合法 EOM ACK
  才发布消息**，提前确认（`ack_not_awaiting`）或错误确认（`ack_bad`）
  都不能当成功。
- Abort（0xFF）：b2 原因码原样保存，b3..b5=255；发送端或接收端都能终止
  对应方向的活动传输。未知控制字节报 `unsupported_control`，不凭剩余
  字节推断含义。

## 时限（data_gap_ns / control_gap_ns，均为正整数）

处理每条合法输入行之前，先让**同一 bus** 内 `deadline <= 当前时间戳`
（相等即超时）的活动传输超时，再解释当前帧；普通帧也推进该 bus 时间，
另一 bus 的帧不影响本 bus。

- BAM 公告后、每个新 DT 后：`data_gap_ns` 等下一包。
- RTS 后、一个许可窗口收完后、最后一包收完后：`control_gap_ns` 等 CTS
  或 EOM ACK；接受 CTS 后用 `data_gap_ns` 等第一包，窗口内每个新包刷新
  data 截止时间。
- 重复帧不刷新时限。
- 文件结束仍未结束的传输记 `eof_incomplete`（列出缺失包号或缺失 ACK），
  绝不填零、不只核对包数、不借用别的会话补全。
- 失败/超时/中止/被替换的通道之后可被新公告正常重新使用；已完成的历史
  消息不被后续坏帧撤回。

## 终端入口

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

- `j1939-import`：全部传输成功退出 0；只要本批存在任何失败/未完成传输或
  拒绝/错误诊断，退出 **1**（成功消息仍照常可查）；文件级错误退出 1 且
  不写任何批次；参数错误退出 2。每次导入都是独立批次，不做幂等复用，
  也不维护追加游标。
- `j1939-message`：给出 bus、双方地址、应用 PGN、BAM/RTS 模式、起止时刻、
  长度、原始载荷（`--json` 中为 `payload_hex`）及逐包字节来源。
- `j1939-transfer`：给出状态变化事件、每个控制/包的来源行、缺失包号、
  是否缺 ACK、失败码与 Abort 原因；RTS、各 CTS、ACK 都能按传输查到原始
  八字节。
- `j1939-trace`：接受消息内左闭右开 `[start,end)` 区间，返回与它相交的
  DT 原文件行、帧内偏移与载荷内范围；跨包区间会返回多帧而不是只指第一帧。

## 表结构（四层关系）

- `j1939_batches`：每次导入的摘要（来源摘要、两个 gap、帧数、传输/消息
  计数、问题事件数、`all_succeeded`）。
- `j1939_frames`：每条原帧一行（普通帧/tp_cm/tp_dt/rejected），保留
  `can_id_text` 与 `data_hex` 原文及解析字段、来源行号。
- `j1939_transfers`：每条传输的公告参数、活动/终态、下一应收包、窗口、
  截止时间、失败码、Abort 原因、被哪条传输替换。
- `j1939_controls`：每条 TP.CM 的类型化字段并回链原帧；RTS/CTS/ACK 的
  原始八字节始终可查。
- `j1939_packets`：每个被接受包号一行（重复帧不产生第二行），记录载荷
  偏移与帧内偏移，是字节追查的来源关系。
- `j1939_messages`：仅在成功时写入的完整应用载荷 BLOB（BAM 无 ACK 帧，
  RTS 回链 EOM ACK 帧）。
- `j1939_events`：按传输记录的完整状态/诊断账本（含帧外的超时、EOF 事件）。

## 原子性与崩溃重开

整份输入先做一次完整结构校验，通过后才开启 `BEGIN IMMEDIATE` 写事务；
批次、原帧、传输、控制、包来源、事件、消息在同一事务提交。环境变量
`GTFS_FAULT_J1939_AFTER_FRAMES=N` 可在处理第 N 帧后触发**真实进程退出**
（`os._exit`，无回滚、无清理）：未提交的 WAL 留在磁盘上，新进程打开时
SQLite 恢复回滚整个事务，因此读不到任何缺字节来源的成功消息，也看不到
半个批次。随后重做得到全新的独立 `batch_id`。

## 失败批次里什么仍可查

协议失败与其他成功传输在同一批次一起落定。失败/超时/中止/被替换/EOF
未完成的传输：

- 不会产生 `j1939_messages` 行（没有不可信的“半截消息”）；
- 已收到的包、收到的控制帧、完整事件账本、失败由哪个控制或包造成、还缺
  哪些包或 ACK，全部保留并可按传输回查到原始八字节与文件行；
- 同批次已完成的消息不受影响，照常可列、可追查字节。

## 手工场景与独立复核

`dist/j1939/`（由 `tools/build_j1939_fixtures.py` 生成）与
`j1939-expected.json` 覆盖：手工编写的跨三包 BAM 预期载荷与 29 位标识符；
两轮 CTS 的点对点会话；同一发送端向两个接收端交错；相同 DT 重传；同号
改内容；窗口外数据；错误与缺失 ACK；公告替换后正常完成；时限恰好相等
的超时；只推进另一 bus 的隔离；接收端 Abort；暂停/重授权/未知控制与
孤立控制；EOF 未完成与 reserved 位拒绝。`dist/j1939/bad/` 是七类文件级
损坏样本。

`tools/verify_j1939_output.py` 是**独立小脚本**：只用标准库 `sqlite3`/
`csv`/`json`，不 import 生产重组代码。它从真实输出库逐字节重建每个
载荷、逐区间核对字节追查、并独立比对状态、失败码、缺失包、Abort 原因、
重复帧不重复贡献来源等。复现：

```
python3 -I tools/build_j1939_fixtures.py     # 生成手工样本与期望
python3 -I tools/j1939_walkthrough.py        # 端到端演练（含崩溃重开）
python3 -I -m unittest discover -s tests     # 含 tests/test_j1939.py
```
