# 定义契约 freightrate

## 数值

| 列 | 类型 | 量化 | 可空 | 说明 |
|---|---|---|---|---|
| weight_kg | Decimal | 0.001 kg | 否 | 实重，输入单位 g/kg/t 都换算到 kg |
| length_cm / width_cm / height_cm | Decimal | 0.001 cm | 否 | 尺寸，输入单位 cm/m/mm 都换算到 cm |
| volumetric_kg | Decimal | 0.001 kg | 否 | 长×宽×高 ÷ volume_divisor |
| billable_kg | Decimal | 0.5 kg 向上 | 否 | 取实重与体积重的较大值后按台阶进位 |
| first_price / step_price / min_charge | Decimal | 0.01 元 | 否 | 运价表金额 |
| fuel_pct / insurance_pct | Decimal | 小数 | 否 | 8.5% 与 0.085 两种写法都归到小数 |
| declared_value | Decimal | 0.01 元 | 是 | 空值与哨兵都按 0 处理，表示未保价 |
| total_amount | Decimal | 0.01 元 | 否 | 由分项求和得出，不单独舍入 |

金额与用量一律 Decimal，对外 JSON 用字符串。禁止 float 累加。

## 单位

单位表只在 `app/config.py` 定义一份。重量 g=0.001 kg、t=1000 kg；尺寸 m=100 cm、mm=0.1 cm。
带单位后缀的单元格（如 3200g）先剥后缀，单位列非空时以单位列为准。

## 时间

- 存储：UTC，ISO 文本，秒精度，带 Z 后缀
- 输入：接受 Z、+hh:mm、无偏移三种写法；无偏移按 +08:00 解释
- 展示：页面显示 Asia/Shanghai，生成时间与批次时间都带时区名
- 运价版本窗口：`effective_from` 与 `effective_to` 也是 UTC，区间半开 [from, to)

## 空值

- `null` / 空串 / 哨兵 三者语义分开
- 哨兵清单：`-`、`--`、`/`、`N/A`、`NA`、`NULL`、`NONE`、`未知`、`无`、`空`
- 重量与尺寸出现哨兵即进隔离区；声明价值出现哨兵按未保价处理

## 半开区间

运价版本用 `[effective_from, effective_to)`：提货时刻等于 from 命中该版，等于 to 落到下一版。
相邻版本首尾相接时不重不漏。

## 去重键

业务键是运单号 `waybill_no`。同一运单号第二次出现时比较载荷
（承运商、服务、分区、提货时刻、重量、三边尺寸、声明价值、路线）：
载荷相同记 DUPLICATE_WAYBILL，不同记 DUP_CONFLICT，两种都只保留第一条参与计费。

## 联运路线

- 列名 `route`（别名 `路线`、`联运路线`），可选列；留空表示整票单段
- 写法 `承运商/服务/分区>承运商/服务/分区`，分隔符也接受 `->` 与 `→`
- 段号从 1 起，按书写顺序；空段记 ROUTE_GAP，重复相邻段记 ROUTE_DUP_LEG，
  字段数不对记 ROUTE_GAP 或 ROUTE_INVALID，均带 `leg_no`
- 每段独立择版：以整票 `pickup_utc` 在该段 (承运商, 服务, 分区) 的版本窗口里命中
- 整票计费重 = 各段计费重之和（每段各自取大、各自进位）
- 整票应收 = 各段开票之和；第一段收首重，后续段首重记让利 `waiver_amount`
- 交付口径分三数：`contract_total`（合同逐段合计）、`waiver_total`（首重让利）、
  `total_amount`（应收），满足 `contract_total − waiver_total = total_amount`

## 结果代次

- 每次计价写一个草稿代（`generations.status = draft`），按代次整体替换，不叠行
- 核对通过才发布：`status = published`，旧发布代转为 `superseded`，行数据保留
- 展示口径：已进入计价的草稿优先（供复核），未完成的草稿不展示
- 导出口径：只读已发布代；核对不通过时导出仍是上一版
- 重跑同一批数据且结果逐行一致时，丢弃草稿并沿用已发布代

## 空态

- 0 字节文件：拒绝采信，返回 EMPTY_FILE
- 只有表头：采信成功但 rows_in 为 0，页面显示空态
- 全是空行：empty_rows 计数，rows_in 为 0

## 原因码

原因码只在 `app/reasons.py` 定义，分两级：`block` 表示该行不进计费，`info` 表示只做标记。
每条隔离都带原始行号、段号（`leg_no`，非联运为 0）与原因码，可导出 CSV 复核。

联运相关原因码：

| 代码 | 级别 | 含义 |
|---|---|---|
| `INTERLINE_ROUTE` | info | 整票按段计价（路线解析出两段及以上） |
| `FIRST_WEIGHT_WAIVED` | info | 该段首重让利（后续段只收一次首重） |
| `ROUTE_GAP` | block | 路线缺段或缺字段，带段号 |
| `ROUTE_DUP_LEG` | block | 相邻两段完全相同 |
| `ROUTE_INVALID` | block | 段字段数不对或为空 |

计价阶段的段级问题写在 `pricing_issues`（非隔离区），同样带 `waybill_no` 与 `leg_no`，
例如某段提货时点没有命中任何运价版本时记 `RATE_MISSING`。
