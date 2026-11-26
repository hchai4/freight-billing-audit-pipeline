# dbt 压力测试结果（2026-11-25）

操作步骤见 [dbt-stress-test-guide.md](dbt-stress-test-guide.md)。

## 结论

- 合成数据 WMS、EDI 各 200 万行（共 400 万行），dbt 全量重建 **54.88s**。
- 暂存层改成增量后，装入 10% 新数据，两个暂存模型从 4.8s / 3.3s 降到 1.2s / 1.1s（降约 70% 到 75%，各测一次）。
- 增量运行约 97% 的时间花在两个 marts 上，它们仍整表重算。
- SQL 规则检测 5 类注入缺陷：合计召回率 **95.9%**，精确率 **91.4%**。这是 SQL 脚本的结果，不是 dbt 模型或测试的结果。

## 测试环境

| 项目 | 值 |
|---|---|
| 机器 | Apple M4 Pro，14 核，24 GB 内存 |
| Docker | 14 CPU，约 7.7 GiB 内存 |
| Postgres | 16.15，单机容器，`shared_buffers=128MB`，`work_mem=4MB` |
| dbt | dbt-core 1.12.4，postgres adapter 1.11.0，4 线程 |
| 数据 | 困难模式，seed 42，日期 2026-01-26 到 2026-04-19 |
| 增量方式 | `stg_wms_scans`、`stg_edi_invoices` 按 id 高水位线 `delete+insert`。测试时为常开；合并时改成默认关闭，用 `--vars '{incremental_staging: true}'` 打开 |

## 耗时与内存

| 运行 | 数据量 | 耗时 | dbt 峰值内存 | Postgres 峰值内存 |
|---|---|---|---|---|
| 首次全量 | 1.8M + 1.8M | 49.57s | 154.4 MiB | 618.7 MiB |
| 增量 | +0.2M + 0.2M | 42.90s | 146.1 MiB | 661.6 MiB |
| 全量重建（`--full-refresh`） | 2.0M + 2.0M | 54.88s | 146.8 MiB | 686.2 MiB |

Postgres 内存是 `docker stats` 每 2 秒采样的最大值，可能漏掉短暂峰值。

### 各模型耗时（秒）

| 模型 | 首次全量 | 增量 | 全量重建 |
|---|---|---|---|
| `fct_reconciliation_marts` | 38.0 | 29.7 | 42.5 |
| `fct_billing_anomalies` | 7.0 | 11.8 | 7.3 |
| `stg_wms_scans` | 4.3 | 1.2 | 4.8 |
| `stg_edi_invoices` | 2.8 | 1.1 | 3.3 |

其余 view 模型都在 0.1s 以内。模型并行执行，各模型耗时不能直接相加当作总耗时。

### 行数核对

- 增量运行插入 WMS 200,000 行、EDI 199,464 行。
- 最终暂存表：`stg_wms_scans` 2,000,000 行；`stg_edi_invoices` 1,994,397 行，等于首次全量 1,794,933 加增量 199,464，与全量重建一致。核对的是行数，没有逐行比对内容。
- `fct_reconciliation_marts` 2,000,000 行，费率卡、燃油周、zone 全部匹配上，其中 1,871,149 行有承运商账单。

## 读数时要注意

- **不要用总耗时说明增量的收益。** 增量完成后又做过两次没有新数据的重跑，耗时 42.42s 和 43.43s，和增量那次（42.90s）基本相同。全量重建多出的时间主要在 `fct_reconciliation_marts`（42.5s，其他同为 200 万行的运行约 30s），原因未验证。
- 每种运行只测了一次。要引用总耗时，应把全量重建和增量各跑 3 次取中位数。
- 增量只覆盖暂存层。高水位线抓不到 id 小于已有最大值的迟到行。
- `/tmp/dbt_full.log` 和 `/tmp/dbt_incr.log` 已被后来的无新数据重跑覆盖。本文的首次全量和增量数字来自 `/tmp/run_results_full.json` 和 `/tmp/run_results_incr.json`。

## 缺陷检测（`data/stress_eval.sql`，全部 200 万行）

在三次 dbt 运行之后执行。脚本只读原始表和标签表，dbt 只写 `analytics_*`，执行顺序不影响结果。

| 缺陷 | 注入数 | 标出数 | 抓到数 | 召回率 | 精确率 |
|---|---|---|---|---|---|
| DUPLICATE_INVOICE | 60,070 | 54,052 | 54,050 | 90.0% | 100.0% |
| FUEL_TIER_JUMP | 15,705 | 32,753 | 15,255 | 97.1% | 46.6% |
| LATE_ARRIVAL | 39,905 | 38,415 | 38,415 | 96.3% | 100.0% |
| MISSING_KEY | 33,917 | 33,917 | 33,917 | 100.0% | 100.0% |
| TRUNCATED_FIELD | 44,397 | 44,397 | 44,397 | 100.0% | 100.0% |
| **合计** | 193,994 | 203,534 | 186,034 | **95.9%** | **91.4%** |

### 分变体召回率

| 缺陷 | 变体 | 注入数 | 抓到数 | 召回率 |
|---|---|---|---|---|
| DUPLICATE_INVOICE | exact | 38,751 | 34,818 | 89.9% |
| DUPLICATE_INVOICE | formatted | 21,319 | 19,232 | 90.2% |
| FUEL_TIER_JUMP | major | 7,659 | 7,659 | 100.0% |
| FUEL_TIER_JUMP | minor | 3,186 | 2,740 | 86.0% |
| FUEL_TIER_JUMP | one_tier | 4,860 | 4,856 | 99.9% |
| LATE_ARRIVAL | cross_month | 15,931 | 15,931 | 100.0% |
| LATE_ARRIVAL | days_late | 15,949 | 15,949 | 100.0% |
| LATE_ARRIVAL | hours_late | 8,025 | 6,535 | 81.4% |
| MISSING_KEY | blank | 16,925 | 16,925 | 100.0% |
| MISSING_KEY | placeholder | 6,788 | 6,788 | 100.0% |
| MISSING_KEY | sentinel | 10,204 | 10,204 | 100.0% |
| TRUNCATED_FIELD | cut | 31,013 | 31,013 | 100.0% |
| TRUNCATED_FIELD | padded | 13,384 | 13,384 | 100.0% |

### 诱饵行（合法但长得像缺陷）

| 诱饵 | 行数 | 误报数 |
|---|---|---|
| DECOY_GRACE_RATE | 16,987 | 16,987 |
| DECOY_SLOW_BATCH | 78,162 | 0 |
| DECOY_SPLIT_LINE | 36,804 | 2 |

宽限期费率目前完全无法区分，是燃油精确率低的主要原因。

## dbt marts 自身的结果（不能用来算召回率）

`fct_billing_anomalies` 共 124,661 行：

| 异常代码 | 行数 |
|---|---|
| SLA_DELIVERY_FAILURE_REFUND | 109,195 |
| BASE_RATE_OVERCHARGE | 13,436 |
| DUPLICATE_BILLING_COLLISION | 2,030 |

这些是业务规则，定义和注入的缺陷标签不同。

## 决定与后续

- 指南第 7 步（把检测搬进 dbt 测试）暂不做：同样的 SQL 得到同样的数字。只有需要声明"dbt 检测出注入缺陷"时才做。
- 可选后续：重复测耗时；把 marts 改成增量（`fct_reconciliation_marts` 需要看一个包裹的全部账单行）。

## 可引用的表述

> Stress-tested the pipeline on 4M synthetic WMS/EDI rows: dbt full refresh in 55s on single-node Postgres; incremental staging cut staging time by about 70% on a 10% delta. Rule-based SQL checks reached 95.9% recall and 91.4% precision on 5 injected defect types.
