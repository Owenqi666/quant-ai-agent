# 项目审核与本轮实现边界

日期：2026-09-22。启动时 `project workspace (local path omitted)` 为空。只读查找发现以下已有材料；没有假设空目录意味着所有代码都要重写。

| 来源 | 本轮开始前已有 | 本轮开始前缺失/未验证 |
|---|---|---|
| `/path/to/legacy-factor-source` | 19 个算子、21 个因子、下载/缓存脚本、开发说明 | 无完整 IC/回测/组合入口；无已保存评估数据或实验结果；下载有导入副作用，存在对齐/数值/未来信息风险 |
| `legacy-research-plan (local path omitted)` | 研究计划及文献/经济解释 | 计划和简历草稿不构成完成证据 |
| `prior-partial-project (local path omitted)` | 19 个文件：Python contracts/evidence/storage、vendor 快照、PDF及两页图片、论文 JSON、原文公式索引、合成市场数据及生成器、pyproject | CLI 引用 `paper_alpha.cli:main`，但该模块不存在；没有计算适配器、评估器、状态机、可运行任务、报告/benchmark或实际测试 |

半成品按 `import_manifest.json` 中逐文件 SHA-256 复制进当前目录，沿用 contracts、evidence、storage、vendor 和示例资料。旧因子代码详情、可复现边界和行号见 `legacy_factor_audit.md`。原始目录不作改写。

## 实施顺序与当前状态

1. **已完成审核与复用**：区别源码、计划和执行证据，固定原始哈希；21 个旧因子的合成前缀检查纳入测试。
2. **已实现显式契约**：引用、假设、候选、数据元信息、时间切分、预算和结果；原文与改动/推测分开。
3. **已打通离线流程**：PDF 重提取核验、三种受限公式模板、字段检查、AST工具、验证区间评估、确定性报告。
4. **已实现有界状态机**：只修已知算子别名；缺字段/来源不符停止；工具超时、调用预算、文件锁、原子状态、恢复和制品校验。
5. **已建立固定工程评测**：预先声明的正常/缺字段/非法表达式/未来位移/错误归因/常量信号/错误引文案例，对照无修复固定流程，全新运行对照指标及逐日结果。
6. **仍缺研究真实性验证**：用户是否研究过示例论文未确认；没有真实数据绩效，没有语义盲审标签、人工研究耗时或 LLM 对比结果。论文题目检索、通用经济解释、跨论文组合、BRAIN 导出/导入及调用留待后续。

首版采用 Python + pandas/NumPy + pypdf + JSON 文件制品，无需 Agent 框架。工具和决策分开，先固定正确性和实验契约，再替换 proposal provider。所谓 `agent` 当前指有状态、有预算、按工具结果分支的确定性研究策略；不将它包装成已完成的 LLM 自主科研系统。

## 验收口径

- **引用准确性**：每段 quote 在指定 PDF 页的 normalized text 命中；PDF digest 与重新提取内容匹配。语义忠实度仍需人工标注，不能从字符串命中率推导。
- **来源归因**：paper_original 必须与相应证据中的公式 AST 一致，允许已声明的算子名映射；修改/推测需要明确 changes。经济机制真伪不由此检查保证。
- **合法性/执行**：固定测试集中逐项检查预声明状态；无法运行的候选应留下可解释原因。合法候选产出率与“正确拒绝非法候选”分别报告。
- **数值/时序**：独立小例子验证 Rank IC、权重、收益标签；未来扰动、完整日历、warmup、常量和非有限值测试；test 不能作为开发评估区间。
- **复现/报告**：同代码和数据的新运行应产生相同指标及逐日明细；报告由结果生成并可逐值检查。环境哈希防止把版本不同的续跑混入同实验。
- **耗时/成本**：固定数据上记录实测 wall time 与工具数；LLM 调用为 0、外部 API 花费为 0；本地计算成本未计价。不能推导对人工耗时提升或推广性能。

固定基准是开发回归集。它是为了验证明确能力而构造，别名样本及语义重复控制使有界修复的作用可解释，但不代表自然论文任务分布，也不用于宣称统计性胜出。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 74089c5cc609a07f24dbb1c049aead1eee8782bc991229d05f2129b3c9ef5e40.
