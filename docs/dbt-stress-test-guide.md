# dbt 压力测试手动操作指南

目标：用 400 万行合成数据，测 dbt 全量与增量的耗时，并用 SQL 检测 5 类注入缺陷、算召回率。

已经准备好（不用再做）：

| 项目 | 状态 |
|---|---|
| 压测专用 Postgres | 容器 `stress-postgres`，端口 **5433**，库 `logistics_db`，密码 `postgres`，数据卷 `stress_pgdata`。和项目真实库（5432）完全隔离。 |
| 数据 | `data/stress_output/dbt/`：困难模式，日期落在 2026-01-26 到 2026-04-19，和合约种子的有效期对得上。 |
| 已装载 | 每张表前 90%（各 1,800,000 行）。剩下 10% 留作"增量"。 |
| 标签 | `stress_eval.defect_labels`（含全部 2M 行的标签）。 |
| dbt 仓库 | 暂存层增量已合并进 `master`，默认关闭（见第 3 步）。 |

下面两个目录用简称：

- `AUDIT` = `~/Desktop/DBTProject/freight-billing-audit-pipeline`
- `DBT`   = `~/Desktop/DBTProject/freight-billing-audit-pipeline-dbt`

---

## 0. 先知道的三件事

1. **增量默认关闭。** 默认情况下暂存层是 view，marts 是 table。压测时每次 `dbt run` 都要打开开关（第 3 步），包括第一次全量。
2. **`DBT/.venv/bin/dbt` 的启动脚本指向旧路径，直接运行会报错。** 一律用 `python -m dbt.cli.main` 代替 `dbt`。
3. **dbt 自己的审计结论不能用来算召回率。** marts 的异常是另一套业务规则，定义和注入的缺陷标签不同，生成器的燃油费率表也和种子不同。召回率统一用第 6 步的 SQL 来算。实测结果见 [dbt-stress-test-results.md](dbt-stress-test-results.md)。

---

## 1. 每个终端先设环境变量

```bash
cd ~/Desktop/DBTProject/freight-billing-audit-pipeline-dbt
export POSTGRES_HOST=localhost POSTGRES_PORT=5433
alias dbt='.venv/bin/python -m dbt.cli.main'
dbt debug        # 应该看到 All checks passed
```

`POSTGRES_PORT=5433` 是关键：dbt 的 `profiles.yml` 本来就读这个环境变量，设了它 dbt 就只会连压测库，不会碰真实数据。

dbt 的输出 schema 是 `analytics_staging`、`analytics_intermediate`、`analytics_marts`、`analytics_contract`，全在压测库里。

## 2. 装合约种子（一次）

```bash
dbt seed
```

## 3. 打开暂存层增量

两个大表的增量模式已经写好（`stg_wms_scans`、`stg_edi_invoices`），默认关闭。压测时在命令后面加：

```bash
--vars '{incremental_staging: true}'
```

增量的做法是"高水位线"：`package_id` 和 `edi_record_id` 是补零递增的，所以新行就是 id 大于已有最大 id 的行。

默认关闭是因为本地示例数据会删表重建、id 不变，开着增量会静默跳过新数据。**不要对真实库（5432）打开这个开关。** 某次运行漏加开关，两个模型会变回 view，下一次带开关的运行会重新全量构建，那次的耗时不能当增量记录。

要知道的限制：

- 这只让**暂存层**增量。下游 `int_*` 是 view，`fct_*` 是 table，每次仍会整表重算，所以增量那次的总耗时不会降太多。把这个结论如实记下来，它本身就是有价值的发现。
- 想让 marts 也增量，需要改 `fct_reconciliation_marts.sql`（432 行）。重复计费要看一个包裹的全部历史账单行，所以不能只算新行。这一步建议第一轮测完再决定要不要做。
- 高水位线抓不到"迟到的旧 id"。这正是生成器里 LATE_ARRIVAL 缺陷要考验的点，写简历时可以作为已知限制说明。

## 4. 全量运行，记耗时和内存

终端 A（跑 dbt）：

```bash
cd $DBT   # 同上，别忘了 export 和 alias
/usr/bin/time -l .venv/bin/python -m dbt.cli.main run --vars '{incremental_staging: true}' 2>&1 | tee /tmp/dbt_full.log
cp target/run_results.json /tmp/run_results_full.json
```

终端 B（同时采样 Postgres 的内存和 CPU）：

```bash
while true; do docker stats --no-stream --format '{{.MemUsage}}  {{.CPUPerc}}' stress-postgres; sleep 2; done
```

记录这几项：

- 总耗时：`/tmp/dbt_full.log` 最后的 `Finished running ... in ...`。
- dbt 进程峰值内存：`time -l` 输出里的 `maximum resident set size`（单位是字节）。
- Postgres 峰值内存：终端 B 里 MemUsage 的最大值。
- 每个模型的耗时：

```bash
.venv/bin/python - <<'EOF'
import json
r = json.load(open("/tmp/run_results_full.json"))
for x in sorted(r["results"], key=lambda x: -x["execution_time"]):
    print(f'{x["execution_time"]:8.1f}s  {x["unique_id"]}')
print("elapsed", round(r["elapsed_time"], 1))
EOF
```

注意：用 `dbt run`，不要用 `dbt build`。`build` 会先跑源表测试，你的困难数据里有空 tracking_id，测试会失败，并跳过下游模型。

## 5. 增量运行，记耗时

装入剩下的 10%（各 20 万行）：

```bash
cd ~/Desktop/DBTProject/freight-billing-audit-pipeline
./venv/bin/python -m data.stress_load --phase delta
```

然后在 dbt 里：

```bash
/usr/bin/time -l .venv/bin/python -m dbt.cli.main run --vars '{incremental_staging: true}' 2>&1 | tee /tmp/dbt_incr.log
cp target/run_results.json /tmp/run_results_incr.json
```

公平对比再跑一次**同样 200 万行**的全量重建：

```bash
/usr/bin/time -l .venv/bin/python -m dbt.cli.main run --full-refresh --vars '{incremental_staging: true}' 2>&1 | tee /tmp/dbt_full2.log
```

结果这样填：

| 运行 | 数据量 | 耗时 | dbt 峰值内存 | Postgres 峰值内存 |
|---|---|---|---|---|
| 首次全量 | 1.8M + 1.8M | | | |
| 增量 | +0.2M + 0.2M | | | |
| 全量重建 | 2.0M + 2.0M | | | |

增量那次要同时看两列：暂存层耗时（应显著下降）和总耗时（marts 仍整表重算，可能下降不多）。

## 6. 用 SQL 检测缺陷并算召回率

```bash
cd ~/Desktop/DBTProject/freight-billing-audit-pipeline
docker exec -i stress-postgres psql -U postgres -d logistics_db < data/stress_eval.sql
```

这个脚本把盲检测器的 5 条规则翻成了 SQL，规则都是从数据里学的（ID 形状、月度燃油费率、迟到阈值），不读标签。最后三段查询才去对标签，输出每类缺陷的召回率与精确率、每个变体的召回率、诱饵行的误报数。整个脚本在我这里跑了约 23 秒，可以在第 4 步和第 5 步前后各跑一次，对比结果。

我在初始装载（各 1.8M 行）上跑的结果，你的应该一致：

| 缺陷 | 注入数 | 召回率 | 精确率 |
|---|---|---|---|
| DUPLICATE_INVOICE | 53,982 | 90.1% | 100.0% |
| FUEL_TIER_JUMP | 14,066 | 97.2% | 46.4% |
| LATE_ARRIVAL | 35,889 | 96.3% | 100.0% |
| MISSING_KEY | 30,559 | 100.0% | 100.0% |
| TRUNCATED_FIELD | 39,916 | 100.0% | 100.0% |

分变体看，薄弱点是：小幅燃油漂移 86.2%、22 到 48 小时的边界迟到 81.4%、叠加了第二个缺陷的重复行。燃油精确率低，是因为月初宽限期的诱饵行（15,355 条）全被当成了缺陷。

装完剩下 10% 之后，再跑一遍，分母会自动变成全部 2M 行的标签。

## 7. 可选：想让检测放进 dbt 里

第 6 步的 SQL 是直接读原始表。如果你想让"dbt 抓到缺陷"这件事也成立，可以把每条规则做成 `tests/` 下的单例测试或一个 `analyses/` 文件。建议先不做，等第 4 到 6 步的数字都有了再决定。

## 8. 收尾

```bash
docker stop stress-postgres                                  # 暂停，数据保留
docker start stress-postgres                                 # 恢复
docker rm -f stress-postgres && docker volume rm stress_pgdata   # 彻底删除
```

数据文件 `data/stress_output/` 约 1.6 GB，已被 `.gitignore` 忽略，不需要时可以直接删。

---

## 简历/帖子里怎么写这些数字

- 只写自己亲手测出来的数，并写清数据规模、是合成数据、哪些是 dbt 耗时、哪些是 SQL 检测。
- 召回率写成"在含变体、叠加与诱饵的 400 万行合成数据上，基于规则的 SQL 检测召回率 X%、精确率 Y%"，不要写成产品准确率。
- Postgres 用的是默认配置的单机 Docker 容器。如果你调过 `work_mem` 等参数，要一并写进结果说明里。
