# 关系数据三方合并（base / left / right JSONL → 工作库 → 定稿 JSONL）

数据整理人员把同一份基础字典交给两个团队核对，拿回两份修改稿：有人改显示
名称、有人补分类、有人删整行。本工具对三份 **UTF-8 JSONL**（每行一个对象）
做**关系型三方合并**，不是文本 diff，也不是 Git 包装器；不读取班表、实时
通告、GPS、CAN 或汇总格，也不维护长期修订服务。操作明确分成两次：

1. **比较（compare）**：共同底稿 `base` + 修订稿 `left`/`right` + 固定主键
   字段名 → 一个全新的、可重新打开的 SQLite 工作库；
2. **定稿（finalize）**：工作库 + 人工决定 JSONL → 只有所有冲突都被正确
   处理时才写出最终字典 JSONL（与定稿说明）。

## 输入契约

- 三份文件按同一组主键字段解释；主键至少一列，每列必须是**非空字符串**，
  原样比较：不裁空格、不去前导零（`"007"` ≠ `"7"`）。复合键各分量存入
  独立列，分量边界有意义，**绝不拼接**。
- 某行缺主键分量、文件内主键重复、JSON 重复对象成员、空行、坏 JSON、非
  UTF-8、非对象行：都是**整次输入错误**，比较非零退出且不留可审阅的库。
- 非键字段允许缺席；存在时值限字符串、整数、布尔或 JSON null。拒绝小数、
  数组、对象；布尔与整数不混同（`true` ≠ `1`）。整数按文本精确保存，至少
  支持有符号 128 位范围。字段名可含空格、点、双引号或 SQL 样式文字
  （建表统一用双引号转义）。
- **缺席是独立状态**，与显式 `null`、空字符串、`0`、`false` 均不同；新增
  字段无需预先登记。

## 合并规则（以“行是否存在”为起点）

某主键不在 base：

| 情况 | 结果 |
|------|------|
| 只在一边新增 | 自动收下 |
| 两边新增且字段、类型完全相同 | 收一次（auto_source=`both`） |
| 两边新增但内容不同 | **新增相撞（add_collision）整行冲突**，不把两个不同对象凑成一行；其中恰好相同的字段仍给出自动建议 |

某主键在 base：

| left | right | 结果 |
|------|-------|------|
| 删 | 删 | 自动删除 |
| 删 | 与 base 完全相同 | 自动删除（反向同理） |
| 删 | 改过任意非键字段（含字段存在状态与标量类型差异） | **删除与修改（delete_modify）整行冲突**，不静默挑边 |

两边都保留时，对三份行中出现过的**每个非键字段独立比较**，缺席作为可
比较状态：left 与 right 相同就用它；否则 left 等于 base 用 right；right
等于 base 用 left；其余才是**字段冲突（field_conflict）**。因此一边改名、
另一边补分类可自动合并；一边删字段另一边未动也自动合并（结果缺席）；但
一边删字段、另一边显式设 `null` 必须冲突。整行冲突不再额外生成字段冲突，
同一件事不要求决定两次；冲突行中互不冲突的字段仍保留自动合并建议。

所谓“改主键”按旧键删除、新键插入处理，不推测两行是同一实体。

## 冲突身份与状态指纹

每个冲突有稳定 64 位十六进制身份，是对**导致冲突的三个状态**的规范 JSON
做 SHA-256：类别、主键、适用字段名、三边的存在性与带类型值。三边状态以
**无序集合**编码，所以左右互换后摘要不变；来源文件行号只用于展示、不进
摘要，文件重排后业务身份不变。仅用“主键加列名”不能区分同键不同状态，
本身份绑定完整状态，天然满足这一点。

人工决定文件每行一个对象，必带 `conflict_id`、`state_fingerprint`（取该
冲突当前身份摘要）、`choice`：

- 字段冲突：`choice` ∈ `base`/`left`/`right`/`custom`；custom 给
  `{"value": <标量>}` 或 `{"missing": true}`（**删除一列**，不用 null 代替）。
- 整行冲突：`choice` ∈ `base`/`left`/`right`/`delete`（**删除整行**）/
  `custom`；custom 给 `{"row": {完整对象}}`，必须保留相同主键分量并通过
  字段类型检查。不能选不存在的行版本。
- 每条决定带状态指纹：冲突内容变化后旧决定报“指纹过期”；另一工作库的
  决定只有身份与状态都相同才能继续使用。未知身份、同身份重复、类别不符、
  无效自定义值、漏掉待决冲突全部明确拒绝；工具绝不替用户默默选 left、
  right 或最新输入。无冲突时空决定文件合法。

## 落盘结构（工作库自包含）

| 表 | 内容与类型 |
|----|-----------|
| `meta` | schema_version、`status`（仅 `complete` 可审阅）、创建时间、三文件 sha256/行数、键字段、批次与缓存设置、定稿标记 |
| `key_def` | 主键字段名及顺序（ordinal） |
| `input_snapshot` / `input_chunk` | 三份输入的文件名、sha256、字节数与**按 1 MiB 分块的原文**；原文件移走后仍可审阅、定稿 |
| `keys` | 每边每键一行；主键各分量独立 TEXT 列，`UNIQUE(side, 键列…)` 负责重复键检测 |
| `fields` | 全部出现过的非键字段名（无需预登记） |
| `records` | (side, key_id) → 物理来源行号 |
| `values_eav` | 长表：(side, key_id, field_id, val_type, val_text)。**缺席 = 没有 EAV 行**，与显式 null 永不混淆 |
| `merged_rows` | 每个 distinct 键一行：`status`(auto/conflict)、`outcome`(keep/delete)、冲突数 |
| `merged_values` | 自动接受值与冲突行的逐字段建议，含 `auto_source`(base/left/right/both) |
| `conflicts` | 冲突身份、稳定 ordinal、类别、键、可空字段 id（部分唯一索引保证一键一字段最多一个字段冲突） |
| `decisions` | 定稿后落库的决定审计（choice、custom_json、指纹） |

**缺席与类型编码**：`val_type` ∈ `null`/`str`/`int`/`bool`；整数与布尔的
文本分别是十进制原文与 `true`/`false`，字符串原样，null 的 `val_text`
为 NULL。读出时整数 `int(文本)` 精确还原（超 64 位也不变型），布尔单独
还原，落 SQLite 不会改变 JSON 类型。

## 查找复杂度与批次

- 装载：每行主键走 `keys` 唯一索引（O(log n)），字段值批量插入 EAV，每
  `batch_size`（默认 4096）行一个事务；重复主键由 SQL 索引拒绝，Python
  不保存全量键集合。
- 合并：`SELECT DISTINCT 键列 … ORDER BY 键列` 由 SQLite 在磁盘临时 B 树
  排序（`temp_store=FILE`），Python 游标一次只取一个键；每键三次索引点查
  取三边 key_id，再按 key_id 取该键的字段小集合。复杂度约 O(n log n)，
  常驻内存只含当前键的三份字段映射与一个插入批次。
- 审阅：`LIMIT/OFFSET` 分页走 `merged_rows`/`conflicts` 索引，每页都先
  `COUNT(*)` 给出**完整待决数量**，不只报当前页；主键次序即 SQLite
  BINARY（UTF-8 字节序 = Unicode 码点序）。
- 定稿：决定文件流式入临时表，全部校验通过后才在一个事务内写入
  `decisions` 并流式产出结果；输出先写临时文件再硬链接落位，坏决定或写
  失败时调用方原有输出字节不变、进程非零退出。
- 可调项：`--batch-size`（默认 4096）、`--cache-kib`（默认 16384）。

## 终端入口

```
gtfs-store dict-compare --base B.jsonl --left L.jsonl --right R.jsonl \
                        --key K1 [--key K2 ...] --out work.db \
                        [--batch-size N] [--cache-kib K]
gtfs-store dict-info       --work work.db
gtfs-store dict-auto       --work work.db [--limit N] [--offset M]
gtfs-store dict-conflicts  --work work.db [--limit N] [--offset M]
gtfs-store dict-conflict   --work work.db --id <conflict_id>
gtfs-store dict-key        --work work.db --key V1 [--key V2 ...]
gtfs-store dict-finalize   --work work.db --decisions D.jsonl \
                           --out final.jsonl [--report report.jsonl]
```

`dict-finalize` 的定稿说明按主键记录：自动接受了哪些非 base 字段、经过
哪些冲突决定（类别/字段/选择）、哪些行被删除；不为每个未变单元格写长
报告。`--json` 放在子命令前输出结构化 JSON。

## Python API

```python
from gtfs_store.dictmerge import compare, finalize, WorkLibrary, info

compare("base.jsonl", "left.jsonl", "right.jsonl",
        ["org", "code"], "work.db", batch_size=4096)
with WorkLibrary("work.db") as work:
    work.list_conflicts(limit=50, offset=0)   # 含 total/open 全量计数
    work.conflict("<conflict_id>")           # 三边存在性、真实值、来源行号
    work.key_detail(("A3", "F2"))
finalize("work.db", "decisions.jsonl", "final.jsonl",
         report_path="report.jsonl")
```

## 失败语义

- 比较目标已存在：拒绝覆盖（先检查，发布时硬链接原子兜底）。
- 解析/校验失败：临时库删除，没有 `status=complete` 的残骸可被审阅。
- 定稿遇到坏决定或写出失败：非零退出，工作库不写定稿标记，已有输出字节
  不变；一个工作库只允许定稿一次，重跑需要重新比较。
- 输入只读；仓库既有入口（load/expand/dwell/anon/j1939/rollup/...）不受
  影响。

## 实测规模（见 `python3 -I tools/dictmerge_scale.py`）

10 万行共同底稿 + 两份稀疏修改稿（约 10 万行的修订字典、仅少量编辑）：
比较峰值 RSS 约 52 MB；翻倍到 20 万行后峰值约 62 MB（×1.19），耗时约
×2.14 线性增长——全量行、键与冲突留在 SQLite，Python 不装全量资料。
