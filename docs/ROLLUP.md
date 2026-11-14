# 带权层级汇总（两个具名维度 → 多粒度精确有理数指标）

运营分析员把运营事实按两套分类层级上卷到若干汇总粒度。一条线路可以按
比例分属两个类别，同一辆车会出现在多个小计里；本工具用**精确有理数
系数**解决"小计直接相加会变大、平均数会算错"的问题：每个格保存五个
精确指标，每个格都能下钻到贡献它的源行及合并后的唯一系数。

本工具**只消费用户整理好的事实与分类关系**：不从班次、GPS、匿名路径
或 CAN 会话推导事实，不处理增量修订，不是账本、不是持续服务、不做断点
恢复。一次构建 = 一份配置 + 一份事实文件 = 一个全新的结果库；已有结果
库绝不覆盖。

## 配置（一份 JSON）

```json
{
  "dimensions": {
    "<维度名>": {
      "levels": [ {"name": "line", "members": ["L", ...]},        // 叶级
                  {"name": "category", "members": ["A", "B"]},
                  {"name": "root", "members": ["R"]} ],            // 根级
      "links":  [ {"from": "line", "to": "category",
                   "edges": [ {"child": "L", "parent": "A",
                               "numerator": 1, "denominator": 2}, ... ]},
                  {"from": "category", "to": "root", "edges": [...]} ]
    },
    "<第二个维度名>": { ... }
  },
  "fields": { "dimensions": {"<维度名>": "<事实JSON字段名>", ...},
              "vehicle": "vehicle_unit", "amount": "amount" },
  "grains": [ {"name": "trunk_by_depot",
               "levels": {"<维度名>": "category", "<第二维度>": "ALL"}}, ... ]
}
```

维度规则（违反即拒绝，构建非零退出）：

- 有且仅有两个具名维度；维度名是非空字符串。
- 每个维度至少两级，按从叶到根排列；每级 `members` 是非空字符串编号的
  非空数组，级内唯一。**不同级可以重用编号**，成员身份 = (维度, 级别, 编号)；
  编号原样比较（`007` ≠ `7`，空字符串不当未知值）。`ALL` 保留为粒度选择器，
  不能当级别名，但**可以是合法成员编号**。
- 最高级恰好一个根成员。
- `links` 逐对相邻级给出；跨级连边、重复 link 块、缺失 link 块都拒绝。
- 每条边 `numerator`/`denominator` 必须是正整数——拒绝布尔、浮点近似、
  零与负数；重复的同一条 (child, parent) 边拒绝；child/parent 必须是
  对应级的已登记成员。
- 每个非根成员的出边比例之和**恰好为 1**（用 `Fraction` 精确求和判定）。
  一个下层成员可以有多个父成员（多路径）。因为每个成员都有和为 1 的
  出边，沿相邻级必达唯一根。

字段规则：`fields.dimensions` 恰好覆盖两个维度名；两个维度字段、车辆
字段、金额字段四个名字互不相同，都是非空字符串。

粒度规则：每个粒度有唯一非空名字；对两个维度各选一个**具体级别名**
或 `"ALL"`（去掉该维度，不是图里的根）。相同的级别组合拒绝——组合按
维度名键控，JSON 键顺序不同不产生新口径。

## 事实文件（JSONL，逐行一个 JSON 对象）

每行必须含配置指定的四个字段，其余字段忽略：

- 两个维度字段：已登记的**叶成员编号**（字符串）或 `null`。`null` 表示
  来源没有分类：沿每一级仍为 null，不分给根，也不当成"所有成员"。
- 车辆字段：字符串或 `null`。空字符串是有效车辆编号；`null` 不参加
  车辆去重。
- 金额字段：任意精度整数或 `null`。允许负冲正和零；拒绝布尔与小数。

完全相同的两行仍是两笔事实，以从 1 开始的物理行号标识。JSON 重复成员、
空行、坏 JSON、坏类型、未知叶编号都会指到**行号与字段名**并使本次构建
失败。

## 系数与格

对一笔事实和某粒度选定的级别：沿所有可达路径相乘边比例，到达同一上层
成员的多条路径比例**先相加合并成唯一目标成员系数**，再交叉两个维度的
目标，格子系数 = 二者乘积。维度选 ALL 时该笔在该维度贡献系数 1；维度值
为 null 时贡献 null 坐标系数 1。映射按 (维度, 级别) 只算一次并缓存复用，
不为每笔事实重新枚举原始路径。

每个格坐标带明确的 `kind` 标签，三种坐标永不混淆：

| kind     | 含义                                   | JSON 表示 |
|----------|----------------------------------------|-----------|
| `member` | 选定级别的真实成员（`ALL`、`007` 也行） | `{"kind":"member","level":...,"value":"007"}` |
| `null`   | 源事实该维度为 JSON null（未分类）      | `{"kind":"null","level":...}` |
| `all`    | 该粒度去掉了这个维度（ALL 选择器）      | `{"kind":"all"}` |

只产生**实际有事实贡献**的格，不枚举维度成员的笛卡尔积。

## 五个指标（全部精确有理数）

- `row_weight`：所有贡献系数之和（可以是分数——分配后的行数）。
- `value_weight`：amount 非 null 的贡献系数之和。
- `amount_sum`：amount × 系数之和；没有非 null amount 时为 `null`；
  正负抵消得到精确的 `0/1`，不会变成 `null`。
- `amount_mean` = `amount_sum / value_weight`；随 `amount_sum` 为 `null`。
- `vehicle_count`：有正贡献的非 null 车辆编号的精确不同值数量（整数，
  **不乘系数**）。同一车辆跨多行、多条路径、多个细格，在上卷格仍只算
  一次；细格车辆数不能相加得到上卷格车辆数，均值也不能先算细格再平均。

公开 JSON 里有理数统一为约分后的 `{"num":"整数","den":"正整数"}`，
零固定 `0/1`；车辆数是整数。

## 落盘结构（结果库，自包含）

| 表 | 内容 |
|----|------|
| `meta` | schema_version、status(`building`/`complete`)、创建时间、配置与事实的 sha256、行数统计、批次/缓存设置 |
| `config_snapshot` | 本次实际配置原文（读回解释用） |
| `facts` | 每笔事实：物理行号、两维原值、vehicle_unit、amount（文本，任意精度） |
| `grains` | 粒度名 → 两维所选级别（NULL 表示 ALL） |
| `cells` | 占用格：坐标(kind,value)×2 + row_w/val_w/sum 的 num/den 文本 + vehicle_count |
| `contrib` | 下钻台账：(cell_id, line_no) 主键 + 合并后唯一系数 num/den |
| `cell_part`/`contrib_stage` | 构建期暂存表，发布前 DROP |

新进程只凭结果库即可查询（查询以 `mode=ro` 只读打开，并校验
`status='complete'`），不需要原始输入文件仍在。

## 构建流程与内存契约

1. 校验配置（可在内存）；按 (维度, 级别) 计算并缓存叶→目标系数映射。
2. 逐行流式读事实：校验 → 批量写入 `facts`；按映射交叉出贡献行写入
   `contrib_stage`，同时在 Python 字典里累计**本批次**的格部分和。
3. 每 `batch_size`（默认 4096）行提交一个事务并清空暂存：Python 常驻
   只有一个批次的贡献行与部分和。
4. 全部读完后，`cell_part` 按坐标 `ORDER BY` 流式归并成 `cells`（SQLite
   排序用磁盘临时结构，Python 一次只看一个格）。
5. 车辆去重是一条 `COUNT(DISTINCT vehicle) GROUP BY 格` 的 SQL，在
   SQLite 内部用临时 B 树完成（`temp_store=FILE`），不进 Python。
6. `contrib_stage` 与 `cells` 一次连接join出 `contrib`，DROP 暂存表，
   `status='complete'`，提交；临时文件硬链接到目标路径（**原子、已存在
   即失败**），删除临时文件。

复杂度：贡献行数 ≈ 事实数 × 粒度数 × 每维目标数（本工具的典型值个位数）；
落盘与构建时间随贡献行数线性；内存只随批次大小与配置大小走。10 万笔
（约 90 万贡献行）实测峰值 RSS ≈ 65 MB，输入翻倍到 20 万笔 RSS ≈ 66 MB
（×1.02），耗时 ×1.94——见 `python3 -I tools/rollup_scale.py`。

调节参数（CLI 与 API 都有）：`--batch-size`（每事务事实数/部分和冲刷
粒度，默认 4096）、`--cache-kib`（构建连接的 SQLite 页缓存 KiB，默认
16384）。

## 失败语义

- 配置或事实任一校验错误：非零退出，目标路径不留任何文件（临时文件
  一并清理），查询入口不会把残骸认作完整结果。
- 目标结果库已存在：明确拒绝覆盖（先检查，发布时用硬链接原子兜底），
  已有字节不变。
- 输入与配置只读；仓库既有入口（load/expand/dwell/anon/j1939/...）不受
  影响。

## 终端入口

```
gtfs-store rollup-build  --config C.json --facts F.jsonl --out R.db \
                         [--batch-size N] [--cache-kib K]
gtfs-store rollup-info   --result R.db
gtfs-store rollup-grains --result R.db
gtfs-store rollup-cells  --result R.db --grain G \
                         [--d1-kind member|null|all [--d1-value V]] \
                         [--d2-kind member|null|all [--d2-value V]] \
                         [--limit N] [--offset M]
gtfs-store rollup-drill  --result R.db --grain G \
                         --d1-kind K [--d1-value V] --d2-kind K [--d2-value V] \
                         [--offset M] [--limit N]
gtfs-store rollup-fact   --result R.db --line N
```

- `rollup-cells` 的坐标筛选里真实 null（`--d1-kind null`）与 ALL
  （`--d1-kind all`）分别可选；成员 `ALL` 用 `--d1-kind member
  --d1-value ALL` 选择，三者互不混淆。
- `rollup-drill` 按源行号递增分页，响应里给出该格**完整**贡献行数与
  五个总指标（不是先分页再算总）。
- `--json` 放在子命令前，输出结构化 JSON（有理数为 `{"num","den"}`）。

## 可调用 API

```python
from gtfs_store.rollup import build, RollupResult, Coord

build("config.json", "facts.jsonl", "out.db", batch_size=4096, cache_kib=16384)
with RollupResult("out.db") as result:
    result.info()                 # 构建元数据 + 粒度列表
    result.grains()               # 粒度与格数
    result.cells("grain", d1=Coord("member", "X"), d2=Coord("null"))
    result.drill("grain", Coord("member", "X"), Coord("member", "D1"),
                 offset=0, limit=100)
    result.fact(3)                # 按物理行号读回事实
```
