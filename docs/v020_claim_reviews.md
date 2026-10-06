# v0.20：准确版本的 claim 语义审核

本模块审核已经保存的精确结论文字。它不执行新实验，不改写原始 `ResearchClaims` 的 `automation/draft/unverified` 内容，也不把原实验审核或固定材料标签转成新文字的审批。

## 使用链路

1. 完成并验证已有 daily、monthly 或 author-study 来源，创建冻结 ResearchCase。
2. 保存结构化 claim batch。数值只能引用 case 内实际结果和 JSON pointer，服务端计算 `authoritative_display`；叙述中的数字仍是待核对文字。
3. 选择 batch 内的一条 claim，读取服务生成的准确审核目标。目标展示原文字、工具数值、所有 case 证据与定义、数据/方法范围、结果摘要及执行来源。
4. 人工阅读这些输出后，显式填写审核来源、声明身份、带时区的确认时间和五个维度的结果及理由。页面不能预填“通过”，Agent 示范不代填真实人工判断。
5. 预览并提交。请求携 `expected_target_digest`；错版本或原始来源发生变化时拒绝。
6. 判断纠正时指定活动记录的 ID 和准确 digest。历史保留，禁止分叉、跨目标/来源/审核者替代或确认时间倒退。
7. 文字修改通过原 `ResearchClaims` 创建新的不可变 batch，新目标重新审核。即使单条文字和 item ID 相同，批次改变也不继承判断。

## 绑定合同

`ClaimReviewTarget` 包含 batch ID/digest、单条 ID/准确 resolved-claim digest、Case ID/digest、原 source kind/ID/digest、`case_context_digest` 和对应原始范围。全部 case 证据、定义和 provenance 都保留；大结果 payload 不重复保存，结果 ID/digest/summary 与 claim 内服务端数值引用共同绑定它。

`target_digest` 是完整目标投影（除自身 digest 字段）的规范 JSON 摘要。目标每次通过原 `ResearchClaims.get` 和 `ResearchCases.get` 重验。原 claim 必须仍有准确绑定其请求/正文/元数据的耐久创建收据。作者 study 的来源可能只有 eligibility/counts；模块保留这一限制，不补造执行 attempt 或收益数据。

新增 `claim_reviews` 和 `claim_review_receipts` 表。每条记录都有规范正文、内容身份、创建时间元数据摘要、用于查询的 scope 列和准确请求收据。读取、列表、状态及导出先验证整个有界账本，再过滤；修改 reviewer/source/target 列不能隐藏损坏历史。提交收据之后、事务提交之前再次核验原始目标，观察到来源变化时记录和收据一起回滚。SQLite 与外部文件不是一个原子域，这个 fence 是最后可观察的核验，不声明能防止提交之后的外部篡改；后续读取仍复验来源。

账本上限为 500 条记录/收据、100 层替代链、每条正文 512 KiB、总正文 16 MiB；超过上限安全拒绝。现存收据或子链对应的记录缺失时拒绝，不自动重建。若外部同时删除一条没有子链引用的记录及其唯一收据，两个表本身无法证明它曾存在；识别这种删除需要外部可信 checkpoint 或原备份。这轮不声称抵御整个账本的一致性重写。内容摘要用于完整性检查，不是可信作者认证。

## 判断状态与边界

复用原五维 `AssessmentDimensions`：证据准确性、假设忠实度、机制归因、字段语义和实现对齐。每个维度必须有显式结果及非空理由。任意 `failed` 得到 `failed`；全部 `passed` 才得到 `passed`；`not_assessed` 或 `not_applicable` 保留原因并得到 `incomplete`。不把未判断算成失败率或通过率。

活动链的 scope 是准确 target digest + 来源 + 声明审核者。`source=human` 是本地显式声明，不能证明身份。不同人工审核者的维度结果不一致时显示 `conflicting`，相同结果、不同理由不会产生冲突。自动化记录独立保留，但不进入人工覆盖；即使自动化填写全部通过，人工状态仍是 `pending`。当前没有模型调用，`semantic_quality_score` 始终为空。

“人工声明的语义判断”与“工具验证的数值”始终分开。人工通过不会改变源证据核验级别、数据不足、方法未决事项、独立测试范围或原结果的审批，也不构成软件证明的语义真值、组合执行许可、市场收益或论文复现。

## 接口

- `ClaimReviews.target(claims_id, claim_id)` → `ClaimReviewTarget`。
- `preview(**request)` / `create(**request)` → `ClaimReviewPreview` / `ClaimReviewDetail`。
- `get(review_id)`、`list(limit, offset, claims_id, claim_id, source, reviewer)`、`status(claims_id, claim_id)`、`export(review_id)`。
- `GET /api/claim-review-targets/{claims_id}?claim_id=...`；单条 ID 放 query，兼容已有含斜杠的合法 ID。
- `POST /api/claim-reviews/preview`、`POST /api/claim-reviews`。
- `GET /api/claim-reviews`、`GET /api/claim-reviews/status?claims_id=...&claim_id=...`。
- `GET /api/claim-reviews/{review_id}` 与 `/export`。

创建请求必填 `claims_id`、`claim_id`、`expected_target_digest`、`source`、`reviewer`、`confirmed_at`、`dimensions`、`idempotency_key`。替代时 `supersedes_id` 和 `expected_supersedes_digest` 必须成对提供。不接受自造 metric value、provenance、target 正文或 approved 标志。

## 本地示例与验证

```bash
.venv/bin/python scripts/demo_v020_claim_reviews.py --out /private/tmp/paper-alpha-v020-claims-example
.venv/bin/python -m unittest discover -s tests -p test_claim_reviews.py -v
```

输出目录必须是新目录。示例实际运行 Alpha101 的 `normalized_fixed` 路径，在合成开发数据和声明验证区间上生成工具结果，然后保存完整 case、准确 claim、target、自动化审查、同键重放、显式替代、拒绝请求及导出。`result.json` 给出软件合同检查结果，真实人工记录数量为零，AI 调用关闭。数字对应实际工具输出；合成数据结果不作为市场表现。

专项测试用明确命名的隔离合成 human-shaped fixture 覆盖人工状态、冲突和替代分支，这些输入不写入用户工作区，不算真实人工标签。测试还覆盖错目标、新文字不继承判断、并发幂等、防分叉、完整账本/收据/来源篡改拒绝、有界查询、备份恢复，以及实际 daily artifact 在最终 fence 前改变时事务回滚。
