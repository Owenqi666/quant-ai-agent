# Alpha101 第一轮辅助审核计划

日期：2026-10-05。用户在人工审核准备交付后要求“开始”；本轮推进现有任务的实际阅读和审核引导，不另建研究、不重算实验。

## 已确认状态

工作台 `0.20.0 / schema16`，工作区 `9d15f5b78e85c8c7aa1df2a2186bdd3dfaa5b080805f93cf012ec7ca91cc73cf`。已有三条 automation/draft 草稿，当前材料人工标注和准确结论审核均为 0。已有输出为合成数据 validation 结果。

本轮固定 Case：`<local-case-id-omitted>`；草稿批次：`<local-claims-id-omitted>`。首先展示 `alpha101-paper-formula`。

## 分工

| 执行者 | 独占产物 | 工作 |
|---|---|---|
| Agent A | `artifacts/v020-assisted-review-01/paper/` | 复核固定 PDF 字节、第 15 页引用及原文公式，保存摘录与页面供阅读；工具核对不作为本人语义判断 |
| Agent B | `artifacts/v020-assisted-review-01/metrics/` | 只读核对导出包、准确指标路径、三条草稿和限制，保存可检查依据；不重新评估或填写人工结果 |
| Agent C | `artifacts/v020-assisted-review-01/implementation/` | 只读检查冻结代码中实际表达式、输入字段与运算语义，定位工程证据及尚需人工判断事项 |
| 主 Agent | 本计划、操作引导、汇总和当前 UI | 打开原任务及第一条准确草稿，整合自动核对清单；本人判断只接受用户明确提供的结果或用户在页面实际填写的记录 |

## 完成标准

1. 原论文、准确文字、数据及方法范围能直接阅读；工具事实核对与本人判断明确分开。
2. 原输出、草稿及人工记录不被工具改写。不开新实验、不产生人工耗时、模型成绩或真实收益声明。
3. 用户可以直接完成一次准确文字版本的五维判断；缺失判断保持待填，不代填通过。
4. 原记录读回核对只在用户实际提交后进行；没有新人工记录时，交付阅读材料和可操作页面，并如实标记审核待完成。

AI 接口与 UI 设计仍后置。本轮不用新的方法合同或数据源替换当前 Alpha101 审核任务。

## 本轮实际完成状态

三位 Agent 已完成各自只读核对，并保存原页渲染、指标路径核验和原 v0.9 实现行号；主 Agent 已打开现有任务的 `alpha101-paper-formula` 表单，所有人工判断仍为空。四项冻结指标一致，原 PDF 及审核包未改写，原实验输入与输出文件摘要复核一致。本轮没有重算实验或重跑历史测试。

汇总及产物摘要保存在 `artifacts/v020-assisted-review-01/summary.json` 和 `manifest.json`。`live-status.json` 是本轮读取时的状态快照，显示三条准确文字的人工记录及材料人工记录均为 0；后续本人提交可使该快照过时。实际人工审核、材料参考集及流程耗时对照尚待完成，不列为本轮成果。

下一步按 [阅读指引](v020_assisted_review_guide.md) 实际对照原文、填写本人五维判断并预检保存。只有用户实际提交或明确提供判断后，才读回检查保存结果。

> Public metadata projection: local source paths or personal runtime/profile details omitted; historical engineering facts retained. Original private document SHA256: 5d50eb9b11516b2958e56ada4ac85ebda71ae7563409f907ea52bdb03e2b6f55.
