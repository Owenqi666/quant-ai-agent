# v0.14 月度数值契约与独立核对

范围是 controlled_fixture 工程验证。MOM/ID 时间与缺失规则复用 `monthly-mom-id-v1`；组合语义为 `monthly-portfolio-v1`。没有接入真实行情、交易撮合、论文未知 ID 构造或 AI。本文的样本与耗时不能解释为投资表现或真实研究效率提升。

## 输入、形成与持有

`monthly_evaluation.validate_config(config)` 接收封闭的 `schema_version=1,start_month,end_month,cost_bps,min_assets`；目标月份 1905–2099 年、含首尾 1–24 月，费用 0–100 bps，最少共同形成样本 9–128。规则沿用 v0.13 的 1–36 月回看、0–12 月 skip 和 complete/available 约定。原文模式仍 blocked。

`demo_bundle(config,rules)` 从实际规则所需最早月份构造虚构日历，覆盖完整目标月；包含 36 个完整资产、1 个缺行资产、1 个 null 资产。最大 36 月回看 + 12 月 skip + 24 目标月确实有对应历史；不以删行缩窗。

`evaluate(rules,config,bundle)` 的共同形成样本仅取当时 MOM 与 ID 均有效的资产。MOM 与 ID 分别升序三分组。同值按平均秩分组，不用资产名称拆散；精确值不同不做全局 epsilon 或四舍五入。任何必需极端格为空，都不降组数或回填其他资产。

两个比较组合均 gross exposure=1、net exposure=0：

- momentum：MOM 最高组 +0.5、最低组 −0.5，各组内等权。
- mom_id：低 ID 高 MOM +0.25、低 ID 低 MOM −0.25、高 ID 高 MOM −0.25、高 ID 低 MOM +0.25。

这是项目自行定义的归一化比较；九格 long-only 等权收益仅作辅助诊断。两条正式策略使用相同的形成样本。

权重先形成，再计算目标月完整已声明 sessions 的复合日总收益标签。持仓任何缺行、null、不完整月历或数值不可得，则该策略当月 gross 为 null，原权重保留；不按未来标签筛选或重新归一化。非持仓缺标签不影响该策略。信号的 available 设置不放宽持有标签规则。持有月日历声明本身不完整时，coverage 只描述已知 sessions，另列 `holding_calendar_incomplete`。

## 成本与累计的限定

`Q = sum(abs(current_target - previous_target))`，`turnover_proxy=Q/2`，`estimated_cost=Q*cost_bps/10000`，`net_return_proxy=gross-estimated_cost`。首月 previous_target=0，因此首次建立 gross=1 组合，Q=1、turnover=0.5。这里不模拟持仓漂移、融资、借券或滑点。

无法形成权重的月份使前期目标未知；恢复后的首个可形成月保留 gross，但成本/net 为 null，不能假装从零仓恢复。缺持有标签但权重已形成时，仍保存当期目标作为下期成本基准。

NAV 从 1 连乘 `(1+net)`，遇 null、net≤−1 或不可表示的累计值后永久断开该次完整路径，后续单月结果仍保留。完整 terminal NAV/max drawdown 仅在每月累计都有效时提供；不跨缺失月拼接。

`months_evaluated` 是 net 有效月数；`gross_months`、`turnover_months` 分别为 gross 和 turnover 均值的分母。波动率和 Sharpe 使用有效 net 月份、样本标准差、sqrt(12)、零无风险收益；不足两月或零波动时 Sharpe=null。这些是统计代理，缺月后不能解释成完整持有区间业绩。

## 独立参考与失败边界

`monthly_reference.check(rules,config,bundle,result)` 不导入生产 `monthly_evaluation` 或 `research_protocol`。它从输入独立计算月份窗口、覆盖、复合、形成资格、分组、权重、持有标签、成本、累计和汇总：

- 复合使用输入 binary64 的精确 Fraction 乘积；生产使用 log1p/fsum/expm1，并在零/符号边界触发 v0.13 的精确路径。
- 分组用每个资产的 pairwise 小于/等于计数；生产使用排序后的同值块。
- 加权收益和样本方差用独立有理算术；参考标准差用 60 位 Decimal 开方。
- 元数据、状态、成员、组别、空值、原因、警示与对象字段精确匹配。浮点数允许相对 2e-10 或绝对 2e-12 的核对误差；组别和资格不使用此容差。

参考有自己的封闭输入校验，拒绝越界月份、不合法规则、重复键、乱序日历、非有限数值、假冒真实数据和超预算输入。单次有理复合限制 numerator/denominator 总计 2,000,000 bits；预算不足时核对失败，不能发布。v0.13 生产零边界精确路径仍保留自己的 100,000-bit 预算。合法配置边界可执行，不等于任何极端合法数值都保证可发布；数值不可核对时保留失败，不降低检查标准或编造零值。

原文模式返回 `supported=false, passed=null`，同时独立核对完整 blocked 结构；正常 `issues=[]`。它只能证明“未产生未经支持的结果”，不能证明论文计算已复现。伪造指标、解除 blocked、删除警示或增加字段都会产生 issues。工作流拒绝任何 issues；项目模式另要求 supported=true、passed=true。

## 可手算验收

`tests/test_monthly_evaluation.py` 的九资产、三日虚构月份覆盖 3×3 九格，每格一只。持有月各 ID 行从低到高的 MOM 列收益为：

| ID 组 | 低 MOM | 中 MOM | 高 MOM |
|---|---:|---:|---:|
| 低 | −0.02 | 0 | 0.10 |
| 中 | −0.01 | 0.02 | 0.07 |
| 高 | 0.01 | 0.01 | 0.03 |

momentum gross=`(0.10+0.07+0.03)/6−(−0.02−0.01+0.01)/6=11/300`；mom_id gross=`(0.10−(−0.02)−0.03+0.01)/4=0.025`。10 bps 首月成本为 0.001；连续两月相同目标的第二月 Q=0。专项测试另独立检查月度均值、样本波动和 Sharpe。

23 项专项测试覆盖手算、最大目标/预热范围、异窗与异 skip、年份边界、available/complete、最低样本、并列、全损、复合溢出、未来数据隔离、缺标签冻结权重、断链不恢复、成本基准丢失与恢复、原文阻断、逐层篡改以及 oracle 独立性。命令：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_monthly_evaluation tests.test_monthly_reference -v
```

2026-10-03 在本地 `.venv` 单次运行 23/23 通过，用时 2.679 秒。另一次单进程计时（不含导入和 bundle 生成，不是性能 SLA）：

| 虚构输入 | 资产 | sessions | 收益行 | 目标月 | 生产秒 | 独立参考秒 | 核对项 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 默认 11 月/skip1 | 38 | 391 | 14,840 | 6 | 0.121241 | 0.066612 | 5,052 |
| 36 月/skip12，最大目标范围 | 38 | 1,566 | 59,436 | 24 | 1.691428 | 0.727903 | 20,046 |

这两个输入均 evaluated 且独立参考 passed；它们只验证受控 fixture 链路。接口默认 min_assets=18；资产数量高于此阈值并不保证必需分组一定可形成。
