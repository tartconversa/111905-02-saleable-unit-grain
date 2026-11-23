# 使用说明

本说明给出可复现的入仓、生成数据集、查询与版本对照步骤，并解释口径与排错。
所有示例都在本仓库自带的合成资料上运行。

## 0. 准备

    python3 -m pip install -r requirements.txt
    python3 -m unittest discover -s tests -v

期望结果：28 个测试通过。若依赖未装，`import duckdb` 会失败；请先执行上面的安装。

## 1. 领域自动检查（不写库）

    python3 -m saleable_units check data

* 退出码 0：没有 blocking 依据。
* 退出码 10：存在 blocking 依据，`findings` 会列出 `code`、`severity`、
  `entity_type`、`entity_key`、`message` 与 `detail`。
* 该步同时检查资料层（缺边、引用未解析）与计算不变量（份额守恒、面积非负、
  可售 = 许可 − 占用 − 限制、合同面积不重复分摊）。

## 2. 入仓

    python3 -m saleable_units import data

输出示例：

    {
      "version_id": "8916b699...",
      "status": "imported",
      "entity_state_rows": 23,
      "snapshots": ["2026-01", ..., "2026-11"]
    }

* `version_id` 由资料内容决定。**同一资料目录重复执行 import 返回
  `"status": "already_present"`，不会新增行。**
* 入仓会同时写入：`unit_periods`、`evolution_groups`、`evolution_edges`、
  `space_atoms`、`rights`、`occupations`、`measurements`、`entity_states`、
  `state_blockers`、`contract_allocations`、`stock_monthly`、`domain_defects`，
  以及 `source_rows`（库内原始行，供离线复查）。
* 默认资料库为 `output/warehouse.duckdb`，可用 `--db PATH` 覆盖。`output/` 已被
  `.gitignore` 忽略。

月快照规则：自然月最后一天；若该月晚于资料截止日，则取资料截止日。这样 11 月
不会用 2026-11-09 之后尚未收到的事实去回填。

若资料存在 blocking 问题（例如份额不足、沿革成环、签约定不到空间），`import`
默认拒绝入库并返回退出码 10；确认要把阻塞依据一并保留供复查时，加
`--allow-blocked` 入库，相关单元仍会以 `blocked` 状态和具体 `blocker_detail` 呈现。

## 3. 查询

### 3.1 某天的实体单元

    python3 -m saleable_units query --view as-of --date 2026-11-10

每条记录包含：

* `period_key`（房号@有效期起点）、`unit_id`、`project`、`building`、`use`；
* `status`：`available` / `partial` / `sold_out` / `blocked`；
* 三种面积：`contract_area`（合同面积）、`physical_area`（当前物理面积）、
  `sellable_area`（可售面积），以及 `sold_area`、`restricted_area`、`blocked_area`、
  `permit_area`；
* 精确份额：`measure_total`、`permitted_measure`、`sold_measure`、
  `restricted_measure`、`sellable_measure`、`blocked_measure`（都是实体自身的
  0..1 份额，`measure_total` 恒为 1）；
* `blockers`（代码列表）与 `blocker_detail`（代码、严重度、对应份额、中文说明）；
* `occupancy`（合同面积分摊明细）；
* `lineage`（该单元由哪些旧空间构成）。

示例（合成样本，2026-11-10）：

| 房号 | 物理面积 | 合同面积 | 已售 | 受限 | 可售 |
| --- | --- | --- | --- | --- | --- |
| U2 | 61.20 | 50.00 | 51.00 | 0 | 10.20 |
| U3 | 40.80 | 0 | 0 | 25.50 | 15.30 |
| C1 | 无（缺测绘） | 0 | 0 | 0 | 0（blocked） |

U2 与 U3 来自 U1 拆分：签约 SALE1（合同 50 平方米、占 U1 空间 1/2）只落在
U2 覆盖的 U1 空间 `[0,3/5)` 内，因此合同面积 50 全部归 U2，未复制到 U3；
限制 RES1（占 U1 空间 1/4，位于 `[3/4,1)`）只落在 U3。

### 3.2 今天看到的新单元由哪些旧空间组成

    python3 -m saleable_units query --view today

按根空间份额输出，每行给出 `root_unit_id`、`root_start`、`root_end`、`measure`
及 `share_of_entity`。例如 U2 ← U1 `[0/1,3/5)`，U3 ← U1 `[3/5,1/1)`。

### 3.3 按项目、用途、月份的存量

    python3 -m saleable_units query --view stock

`stock_monthly` 的列为：`unit_count`、`measure_count`、`physical_area`、
`permitted_area`、`sold_area`、`restricted_area`、`sellable_area`、`blocked_area`、
`blocked_unit_count`、`contract_area`。面积均由实体份额 × 当前物理面积换算，
没有测绘的单元不计入面积口径，但仍计入 `unit_count` 与 `blocked_unit_count`。

### 3.4 沿革与缺失

    python3 -m saleable_units query --view lineage --unit U2
    python3 -m saleable_units query --view defects

`lineage` 会给出该房号所有有效期的沿革与阻塞原因；`defects` 给出入仓时发现的
资料问题。

## 4. 生成后续版本并对照

    cp -r data /tmp/revised
    # 在 /tmp/revised 内修正资料（务必同步更新 manifest.json 的 rows 与 sha256）
    python3 -m saleable_units import /tmp/revised
    python3 -m saleable_units versions
    python3 -m saleable_units diff --base <旧版本号> --compare <新版本号> --month 2026-11

`diff` 输出：

* `rollup`：按 `project` + `use` 汇总的 `sellable_delta`、`sold_delta`、
  `restricted_delta`、`permitted_delta` 与 `classifications` 计数；
* `units`：逐房号 `classification`、`classification_label`、各类增量、
  `explanation`、基准/对比版本的空间份额（`base_lineage`、`compare_lineage`）；
* `unchanged_units`：无变化的房号。

两版资料截止日不同时，`diff` 会用两者该月快照日中**较小**的日期评估双方，
避免把资料截止日的差异误读成业务变化；如果某事实在较小日期尚不可见，会如实
体现为差异。

分类判断优先级（从高到低）：新出现拆并/空间完整性阻塞 → 新增房号 → 退出房号 →
缺测绘 → 沿革组成变化 → 销售占用变化 → 权利限制变化 → 许可变化 → 测绘修订 →
其它。使用者可据此从项目汇总下钻到具体空间份额，判断减少来自销售占用、权利
限制、许可，还是拆并资料不完整。

对比结果写入 `version_comparisons` 与 `version_diffs`；对同一对版本重复比较会覆盖
旧记录，不会堆积。`--summary-only` 只输出、不写表。

## 5. 口径与排错

### 5.1 份额与面积

* 内部一律用 `Fraction` 精确计算；面积是十进制字符串换算成 `Fraction`。
* 输出时面积按 4 位小数、份额按 `分子/分母` 展示。展示值不参与后续计算。
* 如果想自查，可用：`可售份额 = 许可份额 − 已售份额 − 受限份额`（且不小于 0）。

### 5.2 为什么某楼栋被阻塞，而另一楼栋仍出数

阻塞只沿沿革关系传播：一个无效变更涉及的前后单元一起阻塞，依赖被阻塞来源的
变更组继续失效，但不会按楼栋或项目扩散。其余楼栋没有受到物理上的牵连，仍按
各自的许可、限制与签约出数。

### 5.3 为什么一个单元同时有多个阻塞依据

`blocker_detail` 会并列所有依据，例如既有 `measurement_missing` 又有
`permit_missing`。每个依据都带 `measure`（受影响份额），便于判断哪一项是主因。

### 5.4 为什么旧版本仍可复查

入仓时把原始行写入 `source_rows`。查询与对照都从库内行重建版本，不依赖来源
目录是否仍在或是否被修改。重建出的 `version_id` 会再次校验，若与库内记录不一致，
说明存储被改动，程序会报错而不会静默使用。

### 5.5 常见错误信息

| 信息 | 处理 |
| --- | --- |
| `资料哈希不符：...` | 修改资料后未同步 `manifest.json` 的 `sha256` |
| `资料行数不符：...` | 修改资料后未同步 `manifest.json` 的 `rows` |
| `资料库结构版本为 ...` | 用旧程序打开新库；请换新建路径或升级程序 |
| `库内原始行重建出的版本指纹 ... 不一致` | 存储内容被改动，该版本不可用于复查 |
| `月份 ... 不在两个版本的可比快照中` | 两个版本没有该月快照，改用共同月份 |
| `房号 ... 在 ... 无法唯一解析` | 该房号在引用日没有唯一有效期，检查 `units` |

### 5.6 写入新资料时的注意事项

* `changes.expected_edges` 必须等于该 `change_id` 的明细条数。
* 同一来源单元在一个变更组内的 `source_slice` 必须恰好铺满 `[0,1)`。
* `source_slice` 端点必须满足 `0 <= start < end <= 1`。
* `release` 只能引用 `restriction`，且用途需一致；许可不能被解除。
* `occupations.contract_area` 是合同记载，不能为了贴合新测绘而改写。

## 6. 数据表速查

| 表 | 粒度 | 主要列 |
| --- | --- | --- |
| `source_versions` | 版本 | `version_id`、`source_dir`、`row_counts` |
| `source_rows` | 版本 × 数据集 × 行 | `payload`（原始 JSON） |
| `unit_periods` | 房号 × 有效期 | `valid_from`、`valid_to`、`content_complete` |
| `evolution_groups` | 变更组 | `status`、`reasons` |
| `evolution_edges` | 变更明细 | 前后期间、份额、来源区间 |
| `space_atoms` | 期间 × 根空间片段 | `root_*`、`coord_*`、`measure_*` |
| `rights` / `occupations` / `measurements` | 来源记录 | 规范化字段 |
| `entity_states` | 版本 × 月份 × 房号 | 状态、三种面积、各份额 |
| `state_blockers` | 版本 × 月份 × 房号 × 依据 | `code`、`measure_*`、`message` |
| `contract_allocations` | 版本 × 月份 × 房号 × 合同 | 分摊合同面积 |
| `stock_monthly` | 版本 × 月份 × 项目 × 用途 | 存量面积与计数 |
| `version_comparisons` / `version_diffs` | 版本对 | 差异与原因分类 |
| `domain_defects` | 版本 | 资料问题 |
