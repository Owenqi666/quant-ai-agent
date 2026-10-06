# v0.22：准确研究样本与确定性评测

本工具把一条准确研究结论及其 Case、Claims、审核目标、来源文件和审核历史冻结为独立样本。首版明确支持实际日度 Alpha101 和行业 MOM 两个开发域；不把受控月度夹具、作者准入结果或行业替代案例转成原论文个股复现。

它检查软件合同和保存结果的一致性。论文忠实度、经济机制是否可信、本人是否满意，仍需要本人阅读材料并在已有五维入口提交判断。当前没有模型调用或模型预测；所有样本为 `model_not_run`，`semantic_quality_score` 和 `model_accuracy` 均为 null。

## 最短操作

先使用 `scripts/prepare_case_review.py prepare` 将已有准确结论导出为阅读材料包。该操作只有 GET，不产生真人记录。它的 `packet.json` 保存 Case、Claims、单条 target、状态及该目标的全部审核历史，`sources/` 保存白名单原文件。

在已安装项目环境运行：

```sh
.venv/bin/python -B scripts/evaluate_research_sample.py build \
  --packet artifacts/my-review-materials \
  --out artifacts/my-research-sample
.venv/bin/python -B scripts/evaluate_research_sample.py verify \
  --out artifacts/my-research-sample
```

输出目录必须是新目录，且不能与输入相同、嵌套或互相包含。已有目录不会覆盖。完整样本可以复制到其他位置再执行 `verify`；历史绝对路径只作为原请求记录保存，不会重新打开或执行其内容。

## 保存合同

`sample.json` 为 `schema_version=1`、`kind=research_evaluation_sample`，绑定全部阅读材料快照、准确样本 ID/digest、实现源摘要及 Python/Pydantic 版本。样本 ID 是规范化正文 SHA256，改变准确文字、指标引用、目标、参考历史或实现便形成不同样本。

`report.json` 为 `kind=research_sample_evaluation`，包含：

- Case/Claims/target/source/result 的摘要、指针与归因检查。
- 实际工具指标值及其 `result_id/result_digest/pointer`；叙述文字不会成为数字来源。
- 来源字节核验状态。纯函数未传来源 bytes 时，技术结果为 `incomplete`，不得声称源文件已核验。
- 五个人工参考维度的 `eligible/pending/unknown/conflicting` 状态，以及活动、取代记录的 ID/digest。
- 已有 automation 与 human 本地声明的可比较范围及一致率；没有可比较维度时一致率为 null。
- `model_not_run`、`frozen_result_pointer_values_only`、`not_rerun`、`reserved_evaluated=false` 和完整限制。

导出目录另含 `input-material-packet.json`、`invocation.json`、完整 `material/`、16 个评测及依赖源文件的只读副本、JSON/Markdown 报告和关闭文件清单的 `manifest.json`。manifest 绑定请求、样本及报告摘要；不执行归档代码、不请求旧路径、不运行实验。

构造失败后保存原请求、已读取的输入和 `error.json`。修复输入后使用新输出目录重试，保留失败尝试。离线核验拒绝缺文件、额外文件、链接、重复 JSON key、非有限数字、超限文件/目录、来源与目标准确身份漂移、错数值、错 pointer、损坏或分叉的取代链和报告漂移。审核历史最多 500 条，每条取代链含根节点最多 100 条；样本 JSON 16 MiB、每文件 16 MiB、输出最多 128 文件/96 MiB，均显式有界。

## 两个域的区别

| 域 | 检查范围 | 保留边界 |
|---|---|---|
| `daily_alpha101` | 用实际候选 `id=alpha101` 定位，允许其位于非首位置；结果必须使用声明的 validation 配置，目标指标不能引用其他候选 | 不评价 test；合成日度样本只证明软件行为 |
| `industry_mom` | 行业组合、固定 MOM 项目修改合同、实际 gross 指标、配置和来源身份、已归档论文/方法/作者文件 | 不评价最终保留期；不是个股原论文复现，未建模成本与实际执行 |

每个样本保留自己的数据、算子及配置，不宣称两域指标可以等价比较。扩展其他计算域须新增明确适配和对应固定回归，当前不静默强转。

## 人工参考与声明比较

只从同一准确 target 下、由服务导出并复验的 `ClaimReview(source=human)` 取得参考。automation 来源即使全部 passed，也不能补充人工参考。活动人类记录某维度无值为 pending，唯一 `not_assessed/not_applicable` 为 unknown，多个不同 outcome 为 conflicting；只有活动人类形成同一 passed/failed 声明的维度可比较。取代记录留在历史中，不继续参与活动判断。

对照量是 `local_declaration_agreement_not_model_accuracy`：仅比较活动人类二元共识与活动 automation 二元共识。unknown、冲突或没有预测时逐维保存不可比较原因。这不是模型输出评分，也不是客观论文正确率。原 Claims 始终保持 automation/draft/unverified，计算通过不会改成真人已认可。

本地 source/reviewer 都是声明，不认证真人身份。离线包没有原数据库创建收据，也无法证明被整个遗漏的历史；一套被整体一致重写的本地包不能靠自身无密钥摘要证明可信。服务核验、快照一致性、独立数值参考、再次运行复现、研究语义正确和人工身份认证是不同证据。

## 可复用 Python 接口

```python
from paper_alpha.research_evaluation import (
    build_sample, evaluate_sample, verify_sample, export_sample, verify_bundle,
)

# source_files 为 {白名单 source_id: 原始 bytes}；不接收 caller 文件路径。
sample = build_sample(packet, source_files=source_files)
report = evaluate_sample(sample, source_files=source_files)
verify_sample(sample, source_files=source_files)

# CLI 对应接口：必须完整读取并核验 A 导出的材料包。
export_sample(material_bundle_directory, new_output_directory)
verify_bundle(relocated_output_directory)
```

固定回归使用真实服务对象、真实 Case/Claims/ClaimReview 投影和隔离的实际计算输出。行业论文/作者 bytes 在该 fixture 中是明确测试 stub；人类形状记录均为隔离控制标签，不是用户质量判断或市场结果。正式真实材料和本人人工判断由主 Agent 的发布验收与后续用户操作分别记录。
