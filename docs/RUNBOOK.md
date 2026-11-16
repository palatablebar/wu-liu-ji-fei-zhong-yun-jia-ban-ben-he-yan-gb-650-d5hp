# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges      # 计算 + 核对，通过即发布
curl -s http://127.0.0.1:18209/api/reconcile            # 五账与发布状态
curl -s http://127.0.0.1:18209/api/generations          # 代次历史
```

同一份输入重复导入命中同一个 run_id，行数与合计不变；结果逐行一致时沿用已发布代，
不会重复生成分段费用。

## 联运票

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_interline.csv
curl -s -X POST "http://127.0.0.1:18209/api/weights?run=RUN-XXXX"
curl -s -X POST "http://127.0.0.1:18209/api/charges?run=RUN-XXXX"
curl -s "http://127.0.0.1:18209/api/reconcile?run=RUN-XXXX"
```

- 通过：`result.published = true`，账单切换为新代次，旧代次转 `superseded` 仍可查
- 不通过：`result.blocked.code = PRICING_INCOMPLETE`，`blocked.detail` 直接说明卡在第几段；
  已发布代、`amount_total` 与三个导出文件都不变
- 复核：`/reconcile?run=RUN-XXXX` 页看分段账、合同/让利/应收三数与代次历史

## 发布与回滚

```bash
curl -s -X POST "http://127.0.0.1:18209/api/reconcile/publish?run=RUN-XXXX"
```

核对不通过返回 409 且不改动任何已发布数据。需要回到旧版时，直接在数据库把
`runs.published_generation` 指回更早的代次即可；各代次的分段与账单行都保留着。

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
| /api/charges 返回 `blocked.code=PRICING_INCOMPLETE` | 某段提货时点没命中运价，或路线缺段 | 看 `blocked.detail` 的段号与原因码；改完运价或路线后重跑，旧版在此期间仍可导出 |
| 对账 `legs` 账不平 | 整票应收与各段开票之和不一致 | 看分段账 note 里的差额与段号，先排查该段运价窗口 |
| 对账页显示「草案未发布」 | 计算完成但核对未通过 | 修数据后重跑；核对通过会自动发布，也可点「核对并发布」 |
