# v0.6 结构化研究审核

结构化审核记录用户对具体候选结果的判断。来源和审核人是本地用户声明，不是身份认证；`passed` 表示各项声明通过，不证明经济机制成立、真实市场有效或投资表现。

## 三类证据分别判定

1. 字面引用核验：已有计算工具验证引文是否能在指定论文页中找到；字面匹配不代表研究解释忠于论文。
2. 数值核验：已有独立参考与回归工具在明确支持的公式范围内比较实际计算。未覆盖的公式保留 `not_comparable`，不能由人工的 `accepted` 替换为数值通过。
3. 结构化审核：人工逐项填写引用准确性、假设忠实度、机制归因、字段语义和实现一致性。既有通用审核及回归用例批准继续保留各自含义。

## HTTP 契约

沿用 `POST /api/runs/{run_id}/reviews`，添加可选 `assessment`。省略或 `null` 保持旧接口行为，不产生结构化语义判断。

```json
{
  "candidate_id": "alpha006",
  "verdict": "needs_changes",
  "category": "hypothesis",
  "note": "请根据实际观察填写，不直接提交此示例。",
  "source": "human",
  "assessment": {
    "reviewer": "实际审核人填写",
    "expected_attempt_id": "从当前 review_targets 获取",
    "expected_result_digest": "从当前 review_targets 获取的64位小写十六进制值",
    "dimensions": {
      "evidence_accuracy": {"outcome": "not_assessed", "reason": "尚未检查引文与页码。"},
      "hypothesis_fidelity": {"outcome": "not_assessed", "reason": "尚未比较研究假设与原文。"},
      "mechanism_attribution": {"outcome": "not_assessed", "reason": "尚未区分原文结论、用户修改与模型推测。"},
      "field_semantics": {"outcome": "not_assessed", "reason": "尚未确认字段可得时间和替代含义。"},
      "implementation_alignment": {"outcome": "not_assessed", "reason": "尚未比较公式与候选实现。"}
    },
    "active_intervals": []
  }
}
```

这个片段是含占位目标的文档示意，不是可直接提交的评测结果。请求中所有对象拒绝未知字段；五个维度必须齐全，每项 outcome 为 `passed`、`failed`、`not_assessed`、`not_applicable` 之一。每项 reason 必须包含非空白内容，最多 2,000 字符。reviewer 最多 120 字符且不可空白。

`GET /api/runs/{run_id}` 新增 `review_targets` 数组，元素为 `{candidate_id, attempt_id, result_digest}`。摘要页面会裁剪大表，目标指纹使用未裁剪的完整候选状态计算，因此客户端必须使用服务端提供的指纹，不能对页面摘要重新计算。仅终态且完整性核验通过的实验给出目标；失败但已核验的候选同样可以接受审核。

服务端在 SQLite 写事务中重新检查实验状态、产物完整性、候选归属和预期 attempt/digest。目标变化返回 `409`；不会将迟到的审核绑定到重试后的结果。无效输入返回 `422`，不会新增审核或事件。历史审核保留原 attempt、修订和候选指纹。

## 汇总语义

每条返回的审核包含 `assessment` 和派生的 `assessment_summary`：

| 条件 | semantic_status |
|---|---|
| assessment 为 null | not_assessed |
| 有 assessment，但来源不是 human | not_human |
| human 且任一维 failed | failed |
| human 且五维全部 passed | passed |
| 其余，包括任一 not_assessed 或 not_applicable | incomplete |

`not_applicable` 可以保留理由，但不会自动折算为全项通过。通用 `verdict` 是处理决定，与分项审核状态分开；`accepted` 本身不使审核变为 `passed`。自动测试或导入可以记录分项内容，但不能据此计入人工语义通过率。每条 summary 明确返回 `source_is_declared: true`。

## 主动工作时间

`active_intervals` 必须明确提交，空数组表示未记录时间，返回 `timing_recorded=false` 与 `total_active_seconds=0.0`。这个零不能用作真实零耗时。

每项包含 `started_at` / `ended_at`，使用带 `T` 和显式时区的 ISO 日期时间。最多 100 段；每段必须正时长、已结束，区间不可重叠，所有区间总计最多 24 小时。服务端按 UTC 比较，输入的原始时区和顺序保留。相邻区间允许；不同排序不会绕过重叠检查。

这些区间只描述这次审核中声明的主动工作时间。它们不是论文研究全过程耗时，也不是机器运行时间。重复审核同一候选不能当作多个独立研究样本，未计时记录不能进入平均耗时分母。

## 数据迁移与恢复

SQLite schema 由 3 升级到 4，只新增 nullable `reviews.assessment TEXT`。旧数据原有 verdict/source/note/attempt/digest 不变，assessment 保持 null。迁移与迁移记录在同一事务，失败会回滚；重复初始化幂等，API 与 worker 并发启动仍由既有 schema 锁串行化。

备份工具接受 schema 1–4。恢复保留结构化内容、目标指纹和历史来源；新 schema 不伪造历史人工判断。Alpha101 材料确认仅表示用户使用过该材料，不自动生成审核结论。

## 验证

运行：

```sh
.venv/bin/python -m unittest tests.test_research_assessments tests.test_migrations tests.test_backup -v
```

测试使用明确标注的自动化夹具，覆盖完整候选指纹、旧接口兼容、严格字段、时区/重叠/未来/时长限制、非人工来源隔离、重试竞态、产物篡改、迁移回滚和备份恢复。夹具中的 human 来源仅用于验证分支，不作为真实评测标签或用户已审核的证据。
