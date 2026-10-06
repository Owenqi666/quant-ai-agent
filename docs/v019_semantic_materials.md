# v0.19：固定材料的语义标注回流

本模块让使用者读取、标注和追溯已有七项材料，沿用五维判断。材料标签独立于原实验审核和生成结论；软件不会替用户判断，也不会因为材料被标注而批准某次实验或 claim。

## 固定材料与可检查来源

材料为原 `evaluation_suites/v018/semantic_cases.json`，SHA256 固定为 `16db3c42232452d889aa5874ee22fb87d44d4456a089e42fb8d6bf7e5fcff5c5`。原材料、模板、reference_draft 和原来的 pending 确认记录不改变；新的声明进入新数据库表。

`paper_alpha.semantic_materials` 提供独立于服务与脚本的严格模型和校验。读取时核验固定材料、manifest 的材料摘要、来源白名单及实际源文件摘要；拒绝越出软件包和文件路径中的 symlink。Alpha101 来源是包内原 PDF。Momentum 来源是包内 `momentum_sources.json`，并核对其 quote anchor 与材料中的定位；这只重新核验来源登记，`raw_pdf_reverified=false`，不能宣称重新核对原论文 PDF。

材料详情包含 proposal、evidence、页码、定位、固定 source_id、review_focus、原 pending 确认和 reference_draft。参考判断明确为 `unverified_reference_draft`，不是标准答案，也不计入人工覆盖。五维表单必须由使用者实际选择 outcome 和填写理由，不默认 passed。

## 服务与类型

`SemanticAnnotations(store)` 使用 schema15 中的 `semantic_annotations` 与 `semantic_annotation_receipts`，原业务表与冻结 Case 不回填。模型位于 `semantic_annotations_schema.py`，未知字段均拒绝。

| 方法 | 用途 |
|---|---|
| `materials()` | 七项固定材料和限制 |
| `material(case_id)` | 一项材料、引用、来源和内容摘要 |
| `source_file(source_id)` | 固定白名单源 `(Path, media_type)`；不接收路径 |
| `preview(**request)` | 只读校验，不保存 |
| `create(**request)` | 不可变保存及耐久收据 |
| `get(identity)` | 核验后读取原记录和原取代链 |
| `list(limit=20, offset=0, case_id=None, source=None, reviewer=None)` | 有界历史查询 |
| `summary(reviewer=None)` | 人工覆盖、未决维度和冲突；排除自动化标签 |
| `export(identity)` | 准确记录的完整 JSON |

根集成提供 `/api/semantic-materials`、`/api/semantic-materials/{case_id}`、`/api/semantic-material-sources/{source_id}` 和 `/api/semantic-annotations` 下的 preview、list/create、summary、detail/export；准确 API 路由以生成的 OpenAPI 为准。

逐条请求需明确给出 `material_sha256`、`case_id`、`source=human|automation`、`reviewer`、带时区的 `confirmed_at` 和五项 `dimensions`（各含 outcome、非空 reason）。`supersedes_id` 默认 null；保存额外要求 `idempotency_key`。时间不能在未来，缺判断/理由、未知维度、错误材料、伪审批、指标分数和任意来源路径均拒绝。

记录返回 `material_case_digest`、完整原始声明、来源、声明状态与限制。`original_result_approval=false`、`claim_approval=false`、`software_verified_semantic_truth=false` 始终由服务器产生，调用者不能传入。`human` 和 reviewer 均为本地身份声明，系统不认证实际身份。

## 不可变保存、修改与恢复

同键同正文返回原记录；同键改正文为 409。相同 material/case/source/reviewer 已存在时，不允许换键静默覆盖或新增另一条根记录。修改须显式填写当前链末端的 `supersedes_id`，保持材料、case、source、reviewer 不变，确认时间不早于父记录；记录保存原父 ID 和 digest。数据库唯一索引防止分叉，旧记录及旧键重放保持原内容。

创建事务同时提交记录与收据。读取/预检/保存核验耐久收据和记录身份绑定；记录摘要、元数据、来源、取代父记录或收据损坏时拒绝。最多 500 条全局历史、100 层取代链；不会删除历史来恢复预算。分页 limit 为 1–100，offset 为 0–100000；汇总额外限制 16 MiB。超过限制明确报错，不截断覆盖分母。

按 reviewer/source/case 过滤前，会在同一数据库快照中核验全部有界记录的 payload、搜索列和取代链，并缓存已核验记录。篡改过滤列不能把历史条目隐藏成零条，也不能让新创建绕过已有损坏记录。保存收据后、事务提交前还会再次核验原材料和来源，若在记录校验与最后阶段之间发生可观察的来源变化，则记录与收据一起回滚。

数据库与文件系统不能组成一个原子事务。这里明确提供最后提交前的来源核验和后续每次读取核验，不宣称文件在核验后永远不会改变；软件包来源发生变更后读取拒绝，原声明内容保持冻结。

基础前端采用既有原键恢复流程：发送前耐久保存请求，未知结果使用原正文原键重试；切换材料或工作区后不能让旧响应覆盖新选择。成功保存与后续列表刷新分开，刷新失败不意味着原标注失败。

## 汇总的实际含义

历史中 human 和 automation 分别计数。取代后的旧记录保留在历史数量内；覆盖只读取各条链的当前末端。每项材料给出活动人工 ID、pending/passed/failed/incomplete/conflicting，以及未评定/不适用/不同声明间冲突的维度。不同声明者的理由文本不同但 outcome 相同时不算冲突；outcome 不同则明确 conflicting，不自动选择最有利标签。可按具体 reviewer 查看其声明范围。

没有人工记录时七项保持 pending；自动化示例不能增加人工覆盖。`human_fully_assessed_cases` 要求全部五维已判断，`not_assessed` 和 `not_applicable` 均保持未知覆盖。`semantic_quality_score` 始终为 null，因为该模块没有评估模型输出，没有独立验证语义真值，也没有人工效率实验。材料覆盖完成仍只是已声明标签可用。

## 旧 CLI 兼容

`scripts/confirm_v018_semantics.py` 保留原输入、七项 batch 声明、输出内容、文件身份、不可覆盖新目录和失败记录，只重用独立核心 validator。该 CLI 仍写独立产物，不自动导入工作区、不产生实验或 claim 审批。旧 CLI 现增加对原白名单源实际摘要的核验；来源损坏时拒绝，不把材料摘要正常解释成源内容正常。

## 示例与验证

```bash
.venv/bin/python -m unittest tests.test_semantic_annotations tests.test_research_claims_confirmation -q
.venv/bin/python scripts/demo_v019_semantics.py --out artifacts/v019-semantic-demo-NEW
```

示例只写两条 automation 声明：原记录和显式取代，保留原键重放、历史、被拒绝的改载请求、分叉与伪审批，以及固定来源副本。八项规则检验的是软件行为，不是语义质量。结果要求 `human_records=0`、七项人工材料 pending、`semantic_quality_score=null`、无模型调用或实验/claim 审批。实际测试数和发布验收以运行日志为准。

专项测试使用临时工作区，human 分支均明确标为合成测试夹具；不接入真实工作区。覆盖并发、回放、显式取代与分叉、跨 scope、来源/材料/symlink 篡改、记录/元数据/收据篡改、缺失收据与未知维度。完整 HTTP、浏览器恢复及 schema14→15 保留由根集成验收。
