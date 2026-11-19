# 来源资料约定

预售空间单元沿革、测绘、权利与签约资料。

面积为平方米十进制字符串，份额用分子/分母字符串表达精确比例；所有有效期左闭右开，null截止表示持续有效。

沿革以change_id成组生效，fraction表示旧单元空间中转给目标单元的份额；一个组的明细都列于changes声明的expected_edges内。不同旧单元并入同一新单元时份额分别以各自旧空间为分母。

permit用途约束作用于scope单元及其对应物理空间，restriction同样随空间延续；release引用限制编号才解除，不按房号变化解除。住宅与商业资格独立。

occupation的share是所引用旧单元空间的售出权益比例，contract_area是法律合同记载面积，不随新测绘变化；measurement给出某物理单元的预测或实测面积，不提供合同改写授权。

空间份额还提供source_slice，即原单元内左闭右开的有理数区间；它是权益覆盖标识，不是坐标或面积值。分拆、限制及占用的交叠以此来源标识解释，不能仅凭份额总数猜测交叠。

## 数据载体

每类资料位于 data 中同名的 NDJSON 文件，一行一个原始对象；字段和基本类型见 data/schema.json。data/manifest.json 固定资料行数与字节哈希。资料中的缺失、歧义、未完整交付及冲突声明属于来源事实，不由资料检查程序推断业务结论。

### units

单元与用途。字段：unit_id (string)、project (string)、building (string)、use (string)、valid_from (string)、valid_to (any)。

### changes

空间变更组声明。字段：change_id (string)、effective (string)、expected_edges (integer)。

### edges

前后空间份额。字段：change_id (string)、from_unit (string)、to_unit (string)、fraction (string)、source_slice (array)。

### rights

许可与限制记录。字段：record_id (string)、kind (string)、scope (string)、share (string)、use (string)、effective (string)、reference (any)、source_slice (array)。

### occupations

历史签约占用。字段：contract_id (string)、unit_id (string)、share (string)、contract_area (string)、measurement_id (string)、effective (string)、source_slice (array)。

### measurements

预测与实测。字段：measurement_id (string)、unit_id (string)、basis (string)、area_sqm (string)、effective (string)、received (string)。

