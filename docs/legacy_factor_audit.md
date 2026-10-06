# Equity Factor Engine 代码审核

审核日期：2026-09-22。审核对象为 `/path/to/legacy-factor-source` 的 5 个文件及 `legacy-research-plan (local path omitted)`。旧目录保持原样。以下结论区分现有代码、历史文字说明和本次实际检查。

## 实现盘点

| 模块 | 实际状态 | 代码依据及可复用范围 |
|---|---|---|
| 数据下载 | 部分完成 | `ML/data.py:12–86`：静态约 100 只股票，yfinance 逐只下载、调整 OHLC、拼接面板、计算 returns/rf、pickle 缓存。下载器存在，但没有本地缓存、下载日志或版本清单可验证其真实数据执行情况。 |
| 时间序列、截面算子 | 已有实现，需边界加固 | `ML/operators.py:11–93`：19 个算子，输入输出均为日期 × 股票 pandas DataFrame；可复用纯计算实现，见下文风险。 |
| 候选因子 | 已有实现 | `ML/factors.py:14–110`：16 个 Alpha101 + 5 个附加因子；统一六参数签名及显式 registry。本次运行全部 21 个。 |
| 评估、组合、回测 | 缺失 | 目录没有 `ic.py`、`neutralize.py`、`backtest.py`、`combine.py`、`main.py`；`PROJECT_HANDOFF.md:124–138` 明确列为后续工作。不能宣称已经具备可调用回测接口。 |
| 测试、研究结果 | 缺失可复查制品 | 开发日志声称曾运行合成测试（`development_log.md:136,203`），但没有测试文件、命令输出或实验结果；本次新检查只能证明本次检查覆盖的行为。 |
| 论文材料 | 仅有文献名称和人工说明 | 5 个文件内没有论文原文、PDF、页码证据、摘录索引或证据哈希；不能把手写经济解释等同已核验原文证据。 |
| Agent、工具执行与审计 | 旧引擎中缺失 | 没有任务状态、结构化假设、候选校验、预算、重试、实验数据库或报告关联。 |

旧计划中的约 80–100 股票范围与 handoff 的研究目标 500 有出入；实际 `data.py` 是 bring-up 子集。旧计划的 CV bullets（`cross_sectional_alpha_plan.md:63–67`）是带占位符的未来草稿，不能作为已完成经历。旧 handoff 的逐次等待确认约定不应阻碍本次用户明确要求的自主最小流程开发。

## 可复用接口

```python
from factors import factors

# 所有字段必须有严格相同、递增、唯一的日期索引和相同股票列。
# 不要 import data 以免触发网络下载和相对路径缓存写入。
signal = factors["alpha006"](open_, high, low, close, volume, returns)
```

`factors.py:2–3` 使用顶层 `from operators import ...`，迁入包时需改为相对导入，并保留来源哈希和差异记录。所有因子及算子模块导入都会设置全局 NumPy 随机种子（两文件第 4/5 行）；计算逻辑本身不依赖随机数，可在迁入版本去除该副作用。初版应只向表达式执行器开放已校验的算子和字段，不执行任意模型 Python。

最小复用范围是 `operators.py` 的 trailing 算子与 `factors.py` 的指定候选函数；另写具有显式输入的数据适配层和评估层。没有必要复制旧 `data.py` 的 import-time 下载逻辑。

## 正确性风险及本次复现

1. **滚动相关的标签对齐有改善，但输入契约仍缺失。** `operators.py:46–54` 通过 DataFrame 相乘自动按日期/股票标签对齐，交换 `y` 列顺序时，同名股票相关性保持正确。不过不同日期输入会静默取并集；本次两个 3×2 面板输入产生 4×2 输出，不符合“保持同形”的注释。必须在算子入口拒绝不一致的索引或列集合，显式规范列顺序。缺失值需要明确完整窗口策略，不能隐式压缩时间轴。
2. **原点矩公式有消去误差。** 同段代码计算 `E[x²] - E[x]²`。对 `x = 1e10 + [1,2,3,4,5,6]`，`ts_corr(x,x,3)` 最后一项为 NaN，而窗口内中心化的 `np.corrcoef` 为 1。常量窗口的相关性应明确定义为缺失；大基数低方差窗口应使用稳定计算，而非简单剪裁后静默报错。`ts_cov:56–58` 有同类风险。
3. **where 的分支数组按位置使用。** `operators.py:90–93` 使用 `np.where`，若 branch DataFrame 股票列顺序与 condition 不同，会把 B 股票数据标成 A。本次把 `a[['B','A']]` 作为 true branch 后，A 输出变成 B 值。严格的统一面板适配层可避免当前固定因子触发，但开放工具仍需校验。
4. **负位移会取未来数据。** `delay` 和 `ts_delta`（`:11–15`）不校验参数；`delay([1,2,3,4],-1)` 返回 `[2,3,4,NaN]`。固定因子的窗口都是正数，但 Agent 生成表达式时必须拒绝负位移及非法/超预算窗口。不能仅依赖“rolling 是 trailing”的注释宣称整个工具层无泄漏。
5. **条件因子 warmup 混入有效值。** `factors.py:23–25,35–43` 的比较把 NaN 当 false，从而返回 0 或其他 fallback。本次 alpha023 前 3 行均为 0，尽管需要 20 日均值。旧文档在 `PROJECT_HANDOFF.md:97–99` 已记录；统一丢弃头部 252 行能覆盖旧固定因子，但生成组合表达式必须根据依赖链计算 warmup，不能假设每个表达式最多 252。数据中途缺失也不能仅靠头部 buffer 解决。
6. **数据选择使用未来可见性。** `data.py:66–72` 按整个下载区间的观测数筛选股票，再对所有股票 close 取完整日期。这是静态股票名单及全区间历史完整性的选择偏差；任意股票缺失还会删除整个日期，使 shift 的“一行”未必是一交易日。正式数据适配层需要记录原始日历、日期缺口、股票范围和可得性政策。
7. **数值和可交易性未检查。** `log(volume)`、`close*volume`、`high-low` 等运算对零、异常值及极小分母的行为未统一规定；`scale` 全零截面产生 NaN。数据导入应校验价格/成交量、有限性与 OHLC 关系；报告应分别记录不合法输入、warmup、常量因子和有效观测，不能将它们统一填零。
8. **信号与成交时间尚未定义。** 因子可以使用当天完整 OHLCV；评估必须让这些收盘后才能获取的信号在后续可交易时点执行，并按标签结束时间 purge 跨分区样本。旧引擎没有这部分实现，也没有被冻结的最终测试区间。

## 需要修正的旧文字

- `development_log.md:153` 和 `PROJECT_HANDOFF.md:89` 把 population/sample 的常数差同时套到 `ts_corr` 与 `ts_cov`。固定完整窗口下协方差确有分母差；相关系数的协方差和两个标准差中的因子相消，理论上没有该缩放差。数值差异不能用此理由掩盖。
- `factors.py:97–98`、`development_log.md:190,214`、`PROJECT_HANDOFF.md:153` 的“隔夜−日内天然正交于 close-to-close momentum”不是代数保证。简单收益满足 `(1+r_overnight)(1+r_intraday)-1`；即便线性近似，和与差的协方差也取决于两段方差，不能从相减推导零相关。它应被记录为研究动机/待检验假设。
- `development_log.md:217` 的 1e-10 数量级并不接近 float64 underflow；乘 1e6 是显示尺度变化，不能说修复了实际 underflow。正比例缩放不改变截面 rank。
- 文档中的“IC 天然衡量 alpha”“sector neutral 即剥离 beta”等是简化解释。当前无 sector、市场 beta 或收益归因实现，不应写入完成清单。

## 本次检查证据

运行环境：CPython 3.14、pandas 3.0.3、NumPy 2.4.6。仅执行纯计算模块，设置 `PYTHONDONTWRITEBYTECODE=1`，没有导入旧 data.py，也没有请求行情。

在 `default_rng(5)` 生成的 400 日期 × 6 股票合法 OHLCV 面板上：

| 检查 | 本次结果 | 解释范围 |
|---|---|---|
| 注册因子可运行、输出轴与形状一致 | 21 / 21 | 合成正常输入下兼容当前 pandas |
| 修改第 300 行及以后的输入，前 300 行信号不变 | 21 / 21 | 固定因子的未来扰动检查，不等同数据或评估整体无泄漏 |
| 丢弃前 252 行后输出均有限 | 21 / 21 | 此合成样本的覆盖情况，不证明真实数据覆盖 |
| 大基数自相关窗口 | 复现缺陷 | 旧函数 NaN，对照相关系数 1 |
| where 重排列分支 | 复现缺陷 | 股票值被错贴标签 |
| 负 delay | 复现缺陷 | 可以读取未来值 |

这些数量是实际本次工程检查，不是因子有效性、收益或 Agent 对人工耗时的成果。首次未来扰动脚本因为 pandas 3 严格禁止向整数成交量列写浮点值而终止；将合成成交量显式构造为 float 后完成检查，未更改被审代码。

最小边界复现命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import sys
sys.path.insert(0, "/path/to/legacy-factor-source")
import numpy as np
import pandas as pd
import operators as op
x = pd.DataFrame({"A": 1e10 + np.arange(1., 7.)})
print("old corr / centered reference:",
      op.ts_corr(x, x, 3).iloc[-1, 0],
      np.corrcoef(x.A.iloc[-3:], x.A.iloc[-3:])[0, 1])
a = pd.DataFrame({"A": [1., 2., 3.], "B": [10., 20., 30.]})
print("reordered where branch:", op.where(a > 0, a[["B", "A"]], 0))
print("negative delay:", op.delay(a, -1))
PY
```

来源 SHA-256：

| 原始文件 | SHA-256 |
|---|---|
| `ML/operators.py` | `815e8db5ce373febf383446100a617f7ef28f81891eea246463b267ba5c77a6a` |
| `ML/factors.py` | `1fe358f6764607bce71e28e9d310e8a31a52bd302a4006bb5b1a4fb1d7f8e2ef` |
| `ML/data.py` | `4d655db789b4f30cc616b68a5c305b6bb2e1b98c4c8d395d79fb4d120a8cea59` |

## 首版建议

1. 保留这 19 个算子和 21 个因子的来源及统一签名，迁入时加固上述边界；示例只激活有核验证据的少量候选。
2. 采用显式读取的本地 CSV/Parquet 适配器、内容哈希及完整面板校验；合成数据仅用于工程验收，真实论文结论需真实且授权的数据另行验证。
3. 新建按信号可得时点执行、按标签时间分区、保留最终测试区间的最小评估器。不要把已有代码包装成“现成完整回测引擎”。
4. 固定流程与有界 Agent 使用同一论文证据、候选范围、数据和评估器，只比较实际执行成功、证据约束、恢复行为、资源调用及报告一致性；人工耗时在没有实测前标记待测。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 4bc8a9b24506e1e6313cd0f73e44932c41bcdc49e5b3e0cfda051a8ab20a8281.
