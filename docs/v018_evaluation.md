# v0.18：审核回流、结构化结论与评测

本轮将已有五维研究审核投影进新创建的 ResearchCase，并提供独立的结构化结论草稿服务。原有四个诊断工具保持原契约。新服务属于应用控制入口，不能创建人工审核、修改计算结果或开放最终测试区间。

## 审核的准确作用域

新建的日频 case 保留已有审核的 `assessment` 和 `assessment_summary`，包括 evidence_accuracy、hypothesis_fidelity、mechanism_attribution、field_semantics、implementation_alignment，以及每项理由、原 attempt/result 绑定、声明审核者和记录的活动区间。普通审核没有五维内容时为 not_assessed；automation 内容为 not_human。当地身份声明不等于身份认证。

月度实验和作者准入研究目前没有五维审核数据，投影 assessment=null、not_assessed，保留它们原有的准确结果作用域。新 case 的审核仍是冻结子集，后续审核不会悄悄进入原 case。v0.17 历史 case 的字段继续缺省，历史摘要和工具账本完全保留；不会回填五维信息或把旧 accepted 解释成语义通过。

## 结构化结论

`ResearchClaims(store)` 提供 preview、create、get、list 和 markdown。HTTP 使用 `/api/research-claims/preview`、`/api/research-claims`、`/{id}`、`/{id}/markdown`。

每项输入包含 id、kind、attribution、text、evidence_ids、metric_references。kind 为 evidence_statement、interpretation、project_rule、metric；attribution 明示 paper_original、author_code、user_modification、model_conjecture、project_convention 或 unresolved。

服务校验证据身份和来源归属。例如引用项目准入规则不能声明 paper_original，解释不能隐式声明作者原结论。这是结构校验：合法 paper ID 配上错误文字仍可能通过，因此所有 narrative_text 均保存为 unverified automation draft。任意文字、伪指令或文字中的数字不成为工具动作、计算指标或人工审批。

metric 引用必须携带准确 case_id、case_digest、result_id、result_digest 和 JSON pointer。示例：

```json
{
  "id": "coverage-count",
  "kind": "metric",
  "attribution": "project_convention",
  "text": "这里是未经语义审核的解释。",
  "evidence_ids": [],
  "metric_references": [{
    "case_id": "research_case_<64 hex>",
    "case_digest": "<64 hex>",
    "result_id": "<exact existing result identity>",
    "result_digest": "<64 hex>",
    "pointer": "/0/result/metrics/evaluated_days"
  }]
}
```

服务端重新核验原来源，解析冻结结果并生成 value、display 和 authoritative_display。输入不能提供 value、semantic_fidelity、actor 或 assessment 等字段。报告可信数字来自 authoritative_display；旁边的 narrative_text 保持未经验证，不把任意自由文字中的数值渲染成工具指标。

支持的指针范围仅为日频候选 `/{index}/result/metrics/{known_metric}`、月度 `summary` 的已知数值指标及每月 strategy 的收益/换手/成本/NAV 代理值、作者准入的计算 summary/monthly counts。配置阈值、原始 source 元数据、任意数据路径不作为计算指标。指针要求规范非负数组索引、有限深度及 RFC6901 转义；非法/缺失路径、标量穿越、null、boolean、非有限值拒绝。允许范围依据现有结果语义，不能推导市场收益等价性。

草稿内容身份与创建收据均可核验，原键重放返回同一记录，改负载冲突；读取时重新验证源。新的人工源审核不自动审批新的结论。任意虚构“human_confirmed”输入均拒绝；当前服务没有结论人工审批端点。

## 固定工程评测

```bash
.venv/bin/python scripts/evaluate_v018.py --out artifacts/v018-evaluation-NEW
```

`evaluation_suites/v018/manifest.json` 在实验开始前固定 13 项软件规则：真实受控任务链、服务端数值引用、数值注入、跨 case 引用、旧 result 摘要、不安全指针、错误来源归属、文字不具权限、伪造审批、完整五维投影、历史 case/工具重放、幂等和源篡改拒绝。

正例通过真实 ResearchJobs 的 validate→commit→submit→队列→原计算工具→observe→complete。固定流程接收相同冻结 task/data/config，并使用原 normalized_fixed 因子引擎；候选完整计算结果进行等值对照。二者共用计算域，因此这是链路一致性基线，不是独立金融回测、AI 优势或人工效率基线。负例使用明确合成的准入计数，不重新认证作者 MAT。

产物保留固定 manifest、草稿、各状态、原实验、相同条件对照、每项拒绝请求/响应、Markdown 报告和文件哈希索引。新的输出目录必须不存在，失败保留 status 和失败项，不覆盖旧产物。最后一项故意篡改隔离负例来源以验证拒绝；该 workspace 是故障证据，不能作为可复用的干净研究例。缺字段、非法表达式、规则未决、执行预算、中断恢复及取消的完整执行行为另由任务 demo 和对应真实边界测试覆盖。

## 人工语义材料

`semantic_cases.json` 包含 7 个来源关联材料：Alpha101 原式、Alpha006 符号改动、错误引文、机制推测被提升为论文事实、turnover 替代 volume、项目多头方向、Momentum 月度窗口。它们保留本地 PDF/source-registry 身份、页码或定位、提案、参考判断草稿及待确认状态。

`human_review_template.json` 提供与现有五维审核一致的空白标注模板。用户选择并使用过 Alpha101 仅确认材料选择，不等于确认这些新解释和负例的语义标签。模板中的 reviewer、attempt/result、确认时间保持空白；软件不能写入人工分数或确认日期。

实际确认操作：打开相应实验的准确候选结果，核对原文页/公式与字段语义，在原审核入口选择 human 并填五维结论及理由；使用当前真实 attempt/result 绑定提交。参考材料的独立标签仍待用户阅读后明确确认，现有源结果审核也不能自动视为这 7 个材料的标签。语义通过率、人工节省时间、LLM token/费用目前均不报告。

单独确认材料的入口：复制模板中的 `standalone_material_annotation` 对象到新的 JSON 文件，保留准确 `material_sha256`，只填写实际阅读过的材料项，填写真实声明审核者、带时区的确认时间及全部五维理由。可删除尚未阅读的项。然后运行：

```bash
.venv/bin/python scripts/confirm_v018_semantics.py \
  --input artifacts/my-v018-human-declarations.json \
  --out artifacts/v018-human-confirmation-NEW
```

该入口只验证显式提交内容、固定材料哈希、身份/时间格式和五维结构，并保存独立不可覆盖产物及每条材料内容摘要。它不修改原模板、不改变工程评测成绩、不推断身份真实性、不替用户填写或提交结论，也不会审批实验或新生成的 claim。未填模板、未知 case、错哈希、重复材料、未来/无时区时间、缺维度或理由均拒绝并保留失败状态。实际人工标签尚未提供，默认材料仍为 pending。
