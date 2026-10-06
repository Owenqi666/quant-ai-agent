# v0.20 人工审核准备：交付记录

日期：2026-10-05。本轮按“评估 → 保存计划 → 三 Agent 分工 → 交叉审查 → 隔离集成 → 当前工作台核验”完成。一个既有 Alpha101 研究任务现在有三条可直接查看的自动化待审草稿、原 PDF 和可离线核验的导出包。人工判断仍待本人填写，AI 接口和完整 UI 设计继续后置。

## 现在可使用的结果

工作台：<http://127.0.0.1:8765/>。

选择 **研究任务 → Alpha101：用户选择材料的可复现开发案例 → 带实际数值引用的结论草稿 → 查看结论草稿**。第一轮只需选择 `alpha101-paper-formula`，按 [快捷指引](v020_human_review_quickstart.md) 对照原 PDF 填写判断。

| 草稿 | 归属与实际内容 |
|---|---|
| `alpha101-paper-formula` | `paper_original`；Alpha #101 原文公式，固定 PDF 第 15 页；没有把经济机制推测写成论文结论 |
| `alpha101-validation-metrics` | `project_convention`；四个指标从准确冻结结果解析，数值由服务生成，文字不硬编码指标值 |
| `alpha101-research-limitations` | `project_convention`；合成数据、validation、引擎模式及原评估限制 |

三条均为 `automation / draft / unverified`。原 Case 中的历史简短审核保留；它没有自动变成材料标注或这次文字的五维判断。当前三条准确文字的人工记录均为 0，模型质量为未测量。

- [论文 PDF](../artifacts/v020-human-preparation-live-01/bundle/paper.pdf)
- [完整审核包](../artifacts/v020-human-preparation-live-01/bundle/packet.json)
- [导出摘要及文件清单](../artifacts/v020-human-preparation-live-01/bundle/manifest.json)
- [短报告](../artifacts/v020-human-preparation-live-01/bundle/report.md)
- [交付验收 JSON](../artifacts/v020-human-preparation-live-01/delivery.json)

Case：`<local-case-id-omitted>`。

草稿批次：`<local-claims-id-omitted>`。精确 target 摘要保存在审核包和交付 JSON 中。源结果摘要仍为 `a5d4ad4a6c026d8df3dc7cff71ea6b2ed563e0e970b6bb7ff84f9de6e2ffd4a5`。

## 验证与故障修复

| 验证 | 本次结果 |
|---|---|
| 核心模块与既有结论合同 | 47 项测试通过，含 16 项新审核包检查 |
| 隔离实际 HTTP / ASGI 合同 | 18 项回归通过；测试前后客户端文件摘要一致 |
| 独立交叉审查 | 两项可复现提交顺序 / 保存中断问题已修复并限定复验通过 |
| 验收包真实 HTTP 集成 | 准备、离线核验、只读联机核验与同键重复准备通过；一份草稿批次、一份收据 |
| 当前 Chrome 页面 | 三条文字、实际指标、准确目标可见，人工字段为空；网页写请求 0 |
| 原工作区保全 | 冻结 Case、其他业务表和 469 个业务数据文件摘要不变；仅新增一份草稿批次及收据，worker 心跳正常推进 |

修复并测试了进程退出释放锁、提交后丢响应及截断响应的原键恢复、完整固定协议在写入前校验，以及临时文件原子发布。部分写入注入测试与真实进程终止测试分别保存，不能将其称为真实断电测试。

证据：[核心测试日志](../artifacts/v020-human-preparation-integration-01/core-tests.log)、[HTTP 回归](../artifacts/v020-human-prepare-b/run-02/result.json)、[独立复核](../artifacts/v020-human-client-review-c-01/resolved.json)、[隔离集成](../artifacts/v020-human-preparation-integration-01/integration.json)、[当前页面核验](../artifacts/v020-human-preparation-live-01/live-ui.json)、[原状态保全](../artifacts/v020-human-preparation-live-01/preservation.json)。本轮隔离测试 launcher 已按准确路径 / 端口 / 进程身份关闭，原 8765 服务继续在线。

## 运行身份与交付边界

- 新客户端代码提交：`c645b3c3b52efa9ea89aad09e165665113ca63b3`。两份脚本摘要同时绑定测试、导出包和交付 JSON；提交后的文档更新不改变这些脚本。
- 正在运行的应用：`0.20.0 / schema16 / 609ac9ba85d3c5215e2e3b79fa57f8dd8fa539fa`，继续使用上一轮验收包；本轮没有更新 API、数据库结构、worker 或前端资产，也没有重新发布应用。
- 当前工具固定支持已有合成 Alpha101 validation Case；不提供通用论文检索或 AI 候选生成。离线检查只验证快照及内部关联；数字引用核验不证明语义、身份、投资有效性或论文复现。

## 研究线仍需完成的内容

[Momentum 阻断复核](v020_real_data_blockers.md) 已区分数据覆盖、项目选择、作者方法未决和执行适配限制。旧 166 月计数当次复核通过：完整 MOM 历史每月 2–26 行，达不到冻结项目门槛 30，故 0/166。原 MAT 字节与小切片重新核验通过；没有重扫 166 月，没有通过降低门槛使原计划通过，也没有组合收益结果。

下一步先由本人完成至少一条准确文字审核及一项固定材料判断，再冻结人工参考。实际人工流程对照仍需本人分别执行并记录时间。Momentum 的后续目标还需明确为独立 MOM 研究或完整 MOM+ID/Table8；方法合同和具体来源满足条件后才能开展真实组合评估。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 8ed5f3704568a8929aad570ef8b8e9e92bd87ef275f2fce414c27f9260a2b8be.
