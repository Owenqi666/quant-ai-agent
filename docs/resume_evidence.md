# SWE 工程成果证据

这是项目完成事实与可写作范围清单，不是对用户个人贡献的自动证明。本轮新增代码由 AI 辅助实现；使用简历表述前，需要用户审核、运行、理解并确认其实际负责的设计、修改、调试和交付部分。

| 可检查的项目事实 | 证据 | 可以表达的工程能力 |
|---|---|---|
| 沿用已有因子算子，并补齐严格本地数据/计算适配器 | vendor/PROVENANCE.json、docs/import_manifest.json、expressions.py、旧因子审核 | 代码审核、复用边界、输入契约、数据对齐与数值调试 |
| PDF 引文、假设、候选和结果串联 | examples/alpha101/task.json、artifacts/demo/ | 数据建模、来源追踪、可检查的实验工具 |
| 按错误决定修复或停止，保存失败尝试 | workflow.py、示例 state/events、基准结果 | 状态机、错误处理、预算约束、可恢复执行 |
| 版本快照、逐日贡献和结果生成报告 | manifest、factor.csv、result.json、verify 工具 | 可复现性、制品完整性、测试与交付 |
| 固定流程对照和固定负例评测 | artifacts/benchmark/、tests/、artifacts/acceptance.json | 基线设计、回归测试、明确指标边界 |
| FastAPI 服务、SQLite 持久化队列与独立 worker | paper_alpha/server/、tests/test_server.py、tests/test_worker_integration.py | API 契约、幂等请求、事务、故障恢复和进程生命周期 |
| 冻结结果审核、任务修订、显式批准的回归案例与前后复测 | scripts/demo_workbench.py、runs/http-demo-01.json | 数据回流、版本一致性、可检查的测试闭环 |
| 更强的规范化固定基线、公式含义去重与完整结果比较 | runs/fullstack-benchmark-01/report.md | 公平基线、指标口径、避免夸大 Agent 收益 |
| 科学条件冻结、旧实验语义绑定、通过/失败/不可比较三种回归结果 | server/regression.py、tests/test_regression_service.py | 业务契约、版本兼容性、历史数据一致性 |
| 独立引用与数值校验，参考限定为 Alpha006/101/012 与明确修改的 Alpha033 | tests/test_regression_contract.py、evaluation_suites/v04/manifest.json | 独立参考实现、负例设计、数值容差和可调试诊断 |
| 事务化迁移、离线备份、新目录恢复及原实验不变性 | tests/test_migrations.py、tests/test_backup.py | 并发启动、故障恢复、可移植数据与可重复交付 |
| 本地与 CI 共用验收入口，保存源码清单、日志与基线 | scripts/verify_release.py、artifacts/release-v03-01/acceptance.json | 自动化验收、交付追踪；远端 CI 未运行则不声称通过 |
| v0.4 合成数据上传、验证、显式注册与可恢复尝试 | server/dataset_imports.py、tests/test_dataset_imports.py、test_dataset_api.py | 不可变输入、幂等状态机、有界子进程、锁与恢复 |
| 事件游标、终态补齐、有界缓存与历史分页 | frontend/src/events.ts、tests/test_event_pages.py | 异步竞态处理、可调试前端状态、资源限制 |
| 原审核到修订、具体结果及复测的追加问题历史 | server/feedback.py、tests/test_feedback.py、scripts/demo_v04.py | 业务一致性、历史数据绑定、可重算汇总 |
| 固定三策略工程评测、明确分母及未覆盖项 | evaluation_suite.py、evaluation_suites/v04/、新发布目录reference-evaluation | 评测设计、可复现报告；未证明人工效率或LLM优势 |
| v0.5 HTTP 响应模型、OpenAPI 生成类型及运行时检查 | response_schemas.py、export_api_contract.py、test_api_contract.py | 前后端契约、兼容历史记录、错误边界和漂移检测 |
| 基础研究表单、显式草稿恢复、修订差异和审核关联选择 | frontend/src/components/TaskEditor.tsx、drafts.ts、浏览器测试 | 表单建模、客户端状态、异步竞态与完整用户流程 |
| 白名单源码包、独立目录安装和真实 API/worker/浏览器检查 | build_source_release.py、check_portable_release.py、对应发布目录portable-source | 可重复打包、安装来源验证与可检查交付；不是远端 CI 或公开发布 |
| 只读诊断、有限容量扫描和故障输入检查 | server/diagnostics.py、test_diagnostics.py | 运行可诊断性、资源边界、避免破坏历史数据 |

Full-stack 界面刻意保持基础设计，视觉方案留待后续决定。AI 提案接口仅预留，不填写模型或自动论文理解方面的成果。该版本是本地单用户工程实现，不能写成已上线的多用户生产平台。

## 可供用户确认贡献后的英文草稿

- Built a Python research workflow linking PDF evidence, structured alpha hypotheses and factor experiments, with reproducible input/code snapshots and result-derived reports.
- Implemented a bounded execution policy with validated expressions, tool timeouts, persisted failed attempts and resumable runs; added tests for temporal alignment, missing data and numerical edge cases.
- Benchmarked the workflow against a fixed no-repair baseline on a synthetic regression suite, checking execution outcomes, reproducibility and report consistency.
- Implemented a full-stack research workbench with versioned data imports, bounded background execution, auditable review-to-revision workflows and reproducible regression reports.

实际测试数、候选成功数和耗时以本次保存的 acceptance / benchmark 文件为准；不把重复语义控制算作独立发现的新 Alpha。

## 暂时不能写的成果

- LLM 自动读任意论文、自动发现有效 Alpha或自主跨论文推理。
- 相比人工节省某个百分比时间：尚无人工对照。
- 实盘收益、有效 Sharpe、真实市场 IC 优势或 BRAIN 平台仿真通过。
- 生产部署、服务用户数量、API 使用成本下降或未经用户确认的独立个人实现比例。

下一轮适合积累的个人证据：用户选择并标注已读论文、亲自审阅一个关键错误的最小复现和修复、决定真实数据契约、增加一项独立评测案例，并能解释保留最终测试集的原因。这些行为完成后再写入个人贡献记录。

## v0.6 新增可核查范围

- 在现有审核流中实现分项研究判断、精确输出版本绑定、过期请求拒绝，以及旧记录兼容迁移；通用接受与人工语义判断分开。
- 将导入与计算输入错误统一为结构化诊断，提供稳定规则码、有界行列样本及逻辑记录定位，保持原始数据不变。
- 扩展三个明确归因的 Alpha101 修改式的独立标准库参考；通过篡改和时序负例验证检查器能发现错误，未支持窗口保持不可比较。
- 保存显式审核工作区间并对重复审核/重叠人时去重；这项工程能力不能写成已证明节省了人工时间。

最终测试条数及交付结论以新的 release-v06 验收目录为准；本文不预先填写通过结果。代码由 AI 辅助实施，个人设计、调试和审阅的实际参与仍应在简历定稿前由本人核实。

## v0.7 新增可核查范围

- 对审核与回归用例批准实现事务化幂等收据，重复请求确认原记录；验证服务提交后丢响应、并发重试和备份恢复后的重放。
- 为普通审核补充明确结果前置条件，拒绝将旧页面判断绑定到新尝试；保留旧接口行为和历史记录。
- 保存绑定冻结结果的本机审核草稿、待确认请求和明确结束的工作区间，处理存储失败、刷新中断和成功后清理顺序。
- 将工作区身份与实验监控提取为小型 hooks，保留已有异步竞态验收，并验证健康刷新不会反复重建实验监控。

人工流程对照仍是未执行协议，不能写成实际效率提升。新的工程事实以本轮完整验收目录为证，个人贡献的确认要求保持不变。

## v0.8 新增可核查范围

- 将事务化幂等扩展至研究创建，前端跨刷新保留创建、实验和报告原请求；处理本机存储失败、迟到响应和保存后读取失败。
- 基于保存的论文、数据、配置、历史执行代码与环境检查实验可比性，指标差值绑定到具体结果产物；未知或不兼容条件不展示差值。
- 实现具有内容摘要的研究级冻结报告，包含证据、修订、失败尝试和关联反馈；验证后续审核及备份恢复不改变原导出。
- 在独立源码包安装后重复执行上述真实 HTTP 故障及恢复流程，保存来源清单和验收证据。

实际通过结果以本轮 release-v08 目录为准。AI 辅助实施与本人贡献应如实区分；本轮没有新增人工效率、真实市场或 AI 能力方面的成果数字。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 6543f388ab9321c2dab23b1c2d5dc5b2e463f6387a984a3c33f1f722a72ecaf4.
