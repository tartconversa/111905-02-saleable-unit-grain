# 预售房屋拆并与可售面积事实仓

本项目把许可证、空间沿革、测绘批次、销售权限制与签约占用导入 DuckDB，形成
**房屋级可售状态**与**按项目、用途、月份的存量数据集**，并支持版本差异复查。
`data/` 内全部为本地合成记录，不对应任何真实个人或机构。

程序不把房号当作永久主键，而是把每个「房号 × 有效期」视为一个 `unit_period`，
并在它下面还原真实物理空间份额。这样：

* 一套跃层拆成两套、相邻单元并成一套时，同一片物理空间不会既算已售又算待售；
* 改房号或拆分不会让原来的签约面积无处可查，合同面积仍按签约记载保留；
* 限制与许可随空间延续，不会因房号更名自动消失。

## 环境

    python3 -m pip install -r requirements.txt      # duckdb==1.4.1
    python3 -m compileall -q saleable_units tools tests

运行测试（含领域自检）：

    python3 -m unittest discover -s tests -v

也可以安装为命令 `saleable-units`，下文统一用 `python3 -m saleable_units`。

## 五分钟上手

    # 1. 领域检查（只读，不写库；有 blocking 时退出码 10）
    python3 -m saleable_units check data

    # 2. 入仓：生成月度存量、实体状态、阻塞依据与空间份额
    python3 -m saleable_units import data            # 默认写 output/warehouse.duckdb
    # 存在阻塞性资料问题时默认拒绝入库（退出码 10）；
    # 确认要保留阻塞依据供复查时加 --allow-blocked。

    # 3. 某天看到的实体单元
    python3 -m saleable_units query --view as-of --date 2026-11-10

    # 4. 今天的新单元由哪些旧空间组成
    python3 -m saleable_units query --view today

    # 5. 按项目/用途/月份看存量
    python3 -m saleable_units query --view stock

    # 6. 查看版本与阻塞清单
    python3 -m saleable_units versions
    python3 -m saleable_units query --view defects

所有命令都接受 `--db PATH` 指定资料库；`--version` 可传版本号或 `latest`。
输出为 UTF-8 JSON，便于下游程序再加工。

## 生成后续版本并对照

入仓版本由**资料内容**决定：同一目录重复导入得到同一 `version_id`，不会重复写行。
资料修正后再次导入会生成新版本，旧版本保留在库内供复查（不依赖来源目录是否仍存在）。

    cp -r data /tmp/revised                          # 在副本上修正
    # 例如补一条实测面积、补一条 release 记录
    python3 -m saleable_units import /tmp/revised
    python3 -m saleable_units diff --base <旧版本号> --compare latest --month 2026-11

`diff` 输出两部分：

* `rollup`：按项目 + 用途汇总的可售/占用/限制/许可增量；
* `units`：逐房号的变化、原因分类、基准版本与对比版本的空间份额，以及
  `explanation` 文字。分类覆盖 `sales_occupation`（销售占用）、
  `rights_restriction`（权利限制）、`permit_change`（许可）、
  `measurement_revision`（测绘修订）、`measurement_missing`（缺测绘）、
  `evolution_change`（拆并沿革变化）、`lineage_incomplete`（拆并资料不完整）、
  `unit_added` / `unit_removed`。加 `--summary-only` 可只看汇总且不写差异表。

差异结果登记在 `version_comparisons` 与 `version_diffs` 两张表中，重复比较同一对
版本会覆盖旧记录而不会重复堆积。

## 口径说明

这是最容易产生误解的部分，代码与测试都以此为准。

1. **房号只在有效期内有意义。** `units` 的 `valid_from`/`valid_to` 左闭右开。
   引用房号的日期必须解析到唯一有效期，否则相关记录被标为不可用并给出依据。
2. **份额精确。** `fraction`、`share` 用 `分子/分母` 精确表达，面积是十进制字符串。
   程序内部用 `Fraction` 计算，仅在输出时按 4 位小数展示，不参与后续计算。
3. **沿革的 `fraction` 必须等于 `source_slice` 长度。** 同一来源单元在一个变更组
   内的来源区间必须恰好铺满 `[0,1)`；不足、超过或重叠都会阻塞该变更组。
4. **一份变更的前后单元一起成立。** 无效变更涉及的来源与目标期间一起阻塞，
   阻塞只沿沿革关系传播，不按楼栋或项目扩散，无关楼栋照常出数。
5. **权利与占用的 `share` 是相对 scope 旧单元整体空间的权益比例**，`source_slice`
   只负责定位，例如 `share=1/2, source_slice=[0/1,1/2]` 表示半个旧空间的售出权益。
   因此 `share` 与 `source_slice` 长度不必相等。拆分后各新单元按自身覆盖的来源长度
   分摊，分摊之和守恒；合并时同理。
6. **合同面积不随测绘改写。** 实测只替换「当前物理面积」；合同面积按签约记载沿空间
   关系分摊到后继单元，任一快照内各单元分摊之和不超过原记载，绝不复制成两份。
7. **三种面积分开呈现。** 对外查询同时给出 `contract_area`（合同面积）、
   `physical_area`（当前物理面积）、`sellable_area`（可售面积），以及已售、受限、
   被阻塞面积。
8. **测量可见性。** 一条测绘只有在 `effective <= 查询日` 且 `received <= 查询日`
   时可见；后补实测不会回溯改写历史月份已落数的物理面积。
9. **限制随空间延续，只按 `release` 引用限制编号解除。** 许可不能被 `release`
   解除（会报 `release_targets_permit`）。同一物理空间上住宅与商业许可合计超过 1
   时判为 `permit_overlap`，不会把某一用途资格传给全部合并空间。
10. **快照与资料版本一致。** 同一次入仓在同一 `version_id` 下同时计算许可、物理
    空间与签约占用，避免许可已更新而成交事实仍按旧房号解释。

## 阻塞依据

当空间暂不能进入可售口径时，`as-of` 输出的 `blocker_detail` 会给出具体理由与
对应份额（`measure`），`blocked_area` 给出面积。常见代码：

| 代码 | 含义 |
| --- | --- |
| `change_group_invalid` / `change_timeline_inconsistent` | 变更组不成立或时间边界不一致 |
| `source_share_insufficient` / `source_share_overflow` / `source_overlap` | 来源份额不足、超过或重叠 |
| `evolution_cycle` | 沿革成环或位于环的下游 |
| `change_target_unresolved` | 目标期间没有任何成立的变更组创建 |
| `permit_missing` / `use_conflict` / `permit_overlap` | 缺许可 / 跨用途不可继承 / 用途资格冲突 |
| `restriction_*` / `release_*` | 限制无法解析或解除记录无效 |
| `occupation_*` | 签约无法定位空间或落在不可用空间 |
| `measurement_missing` | 截至查询日缺少有效测绘，可售面积无法落数 |
| `physical_overlap` | 同一物理空间同时被多个有效房号覆盖 |

`check` 会把这些缺陷与「计算结果不变量」（份额守恒、面积非负、可售 = 许可 − 占用
− 限制、合同面积不重复等）一起输出；有 blocking 时退出码为 10，可直接用于流水线。

## 目录

    saleable_units/
      source.py       资料装载、格式与身份校验、版本指纹
      domain.py       强类型领域对象、有效期解析、月快照
      fractions.py    精确有理数与左闭右开区间
      engine.py       沿革几何、权利/占用映射、可售状态
      warehouse.py    DuckDB 存储与数据集生成（幂等）
      query.py        按时点/今天查询、版本差异与分类
      checks.py       领域自动检查（资料层 + 计算不变量）
      cli.py          命令行入口
    docs/source-contract.md   来源格式约定
    docs/usage.md             中文使用说明（口径详解与排错）
    tools/inspect_source.py   原始资料字节/行数/字段检查
    tests/                    单元与端到端测试

## 数据表

| 表 | 内容 |
| --- | --- |
| `source_versions` / `source_rows` | 版本指纹与库内原始行，用于离线复查 |
| `unit_periods` | 房号 × 有效期，含内容是否完整 |
| `evolution_groups` / `evolution_edges` | 变更组状态与前后空间份额 |
| `space_atoms` | 每个期间由哪些根空间区间组成（沿革几何） |
| `rights` / `occupations` / `measurements` | 规范化后的来源记录 |
| `entity_states` | 每月快照的房号级可售状态与份额/面积 |
| `state_blockers` | 每个阻塞依据及对应份额 |
| `contract_allocations` | 合同面积向各后继单元的分摊 |
| `stock_monthly` | 按项目、用途、月份的存量 |
| `version_comparisons` / `version_diffs` | 版本对照与差异原因 |
| `domain_defects` | 入仓时发现的资料问题 |
