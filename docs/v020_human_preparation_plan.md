# v0.20 下一阶段：真实人工评测的可执行准备

日期：2026-10-05。实施起点为已验收部署的 `0.20.0 / schema16 / 609ac9ba85d3c5215e2e3b79fa57f8dd8fa539fa`，原工作区身份 `9d15f5b78e85c8c7aa1df2a2186bdd3dfaa5b080805f93cf012ec7ca91cc73cf`。

## 评估结论

- 当前服务健康，AI 未接入。原工作区有两个冻结研究 Case，材料人工标注为 0，准确结论草稿为 0。
- Alpha101 Case 已有单候选、`normalized_fixed`、validation 结果，且兼容固定人工对照协议。无需重新运行、另建研究或变更保留测试区间。
- 缺口在评测准备：用户若立即审核，仍需自己创建结论、确认指标路径和整理来源。应由工具从已有准确版本准备这些机械工作。
- 原 Case 中存在一份历史简单审核，它没有五维判断；不能自动升级为材料标签或准确结论审核。
- Momentum 的真实来源绑定已存在；公开作者文件、历史扫描和方法规则须分别复核，不能把所有阻断都笼统归因为数据缺失。

## 本轮交付和边界

交付一个限定于当前 Alpha101 开发案例的评审准备工具：读取冻结结果 → 派生少量待审文字 → 服务端解析指标 → 保存 automation/draft → 导出来源及准确目标 → 核验与操作指引。

本轮不修改运行应用的 API、数据库、worker 或前端；当前验收包继续运行。新客户端脚本与运行服务分别记录代码身份，不将新脚本冒称为已部署应用版本。不增加实际人工判断、评测分数、人工耗时、模型调用或真实行情收益。

## 分工

| 执行者 | 独占文件 | 工作及验收 |
|---|---|---|
| Agent A | `scripts/human_review_packet.py`、`tests/test_human_review_packet.py` | 纯数据准备和离线结构核验；定位准确 Alpha101 候选而非假设数组第一个就是它；复核 Case/结果/材料摘要、公式与论文引文；原文、项目指标和研究限制分开归因；所有文字保持 automation/draft/unverified |
| Agent B | `scripts/prepare_human_review.py`、`tests/test_prepare_human_review.py` | 有界 HTTP 客户端及 prepare/resume/verify CLI；只访问明确 loopback 工作台；写入前确认工作区、输入、真实运行身份及兼容协议；提前保存准确请求和幂等键，丢响应用原键恢复；仅允许预览和创建结论草稿，绝不提交审核或实验 |
| Agent C | `docs/v020_real_data_blockers.md`及独立审查产物目录 | 只读复核 Momentum 的冻结计划、扫描计数、缺失原因和来源字段；分别判断数据不足、项目规则、方法未决和适配实现限制。标明历史核验与当次重算边界，给出下一步有限研究验收条件 |
| 主 Agent | 本计划、集成、交叉审核、实际准备和交付报告 | 先冻结 A/B 接口，审查三包；针对改动测试并在隔离 HTTP 服务做流程核验；保存客户端版本；使用当前验收服务为原 Alpha101 Case 创建少量待审草稿；核对原结果不变、人工记录不增；输出当前可操作的短指引 |

## A/B 接口

`human_review_packet.py` 提供：

- `build_claim_request(case, material) -> dict`：返回 `case_id`、`case_digest`、`claims`，不含提交键。固定少量文字：原文公式、工具指标、项目数据/评估限制；不能新增经济机制的事实断言。
- `validate_inputs(case, material, runtime, health)`：共享完整输入、准确固定协议及执行身份校验；准备与恢复必须在任何 POST 前调用，不能只在写入后构建审核包时检查。
- `build_packet(case, material, claims, targets, runtime, health) -> dict`：冻结上述公开输入及准确关联，`schema_version=1`、`kind=alpha101_human_review_packet`；包内无人工判断，无模型质量成绩。`targets` 是服务端按每个 claim ID 返回的准确目标。
- `verify_packet(packet) -> dict`：校验摘要及内部关联、指标精确路径和值、固定来源和 attribution；结果明确只是快照完整性，不证明人工或原始来源真实性。

B 在导出包保存原 PDF、`packet.json`、有文件摘要的 `manifest.json`、原创建请求、当前运行身份和简短 `report.md`。PDF 摘要必须同时匹配固定材料和原 Case 引文。客户端输入/输出有界，未知工作区、重定向、非固定材料、损坏输入、错误公式/候选/版本均拒绝。完整结果不覆盖；失败保留，未确定是否提交时只能恢复原请求。

## 执行顺序和完成标准

1. 三包并行；A/B 按共享接口实现，C 不修改冻结研究定义或扫描。
2. 必要测试覆盖错候选、错公式/引文、非 validation、错摘要、数值篡改、网络丢响应、原键恢复、错误工作区、输出文件篡改和禁止的人类/实验写入。
3. 隔离工作区使用真实既有 HTTP 服务合同验证工具，不把合成测试标签写入原工作区。
4. 固定客户端脚本和实际代码身份，才操作当前工作台。优先使用原 Case `<local-case-id-omitted>`，原源实验 `<local-run-id-omitted>`。
5. 当前工作台仅新增待审结论和相应请求收据；原 Case、源实验、数值和既有审核均保持不变；不创建人工材料标签、ClaimReview、参考集或流程观测。
6. 输出包含可直接打开的 PDF、审核目标与当前工作台选择步骤。实际本人判断、参考冻结及两路径耗时记录仍由用户完成。
7. 本轮终点是“一个可核验、可直接审核的现有研究任务”和“下一步真实数据阻断清单”，不宣称人工评测或行情论文复现已完成。

## 本轮完成记录

上述自动化准备与工程验收已完成，见 [交付记录](v020_human_preparation_delivery.md)。原任务新增三条待审草稿；47 项核心/合同测试、18 项隔离 HTTP 回归、真实验收包集成和当前只读页面核验通过。人工判断、人工参考及真实流程对照仍由本人实际完成，没有代填。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 174c3ffd036c8336fa04f423e120f671073897cf2652a74aa4029bdfaa248f65.
