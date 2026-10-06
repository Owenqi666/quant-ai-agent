# v0.20：冻结语义参考与声明一致性

本模块把既有七项开发材料的人工声明冻结成可核验版本，连接「材料标注 → 参考快照 → 完整声明核对 → 离线导出」。它没有调用模型、生成真人标签或批准实验。原 v0.18 的 13 项工程评测、原材料、旧确认 CLI 和六阶段耗时协议保持各自边界。

## 使用流程

1. 在原「语义评测」中查看准确材料、论文证据和未验证参考稿，真人自行提交五维判断及理由。`source=human` 和审核者是本地声明，系统没有认证这个身份。可以暂不判断，未知不能默认为通过。
2. 选择要冻结的材料案例并预览参考集。服务返回选中案例的**全部活动人工 ID/摘要**和每一维的状态；不同审核者不会被静默筛掉。提交携带全部准确 ID/摘要。预览后标注发生变化时，新提交返回 409，需重新预览。
3. 保存后得到 `semantic_evaluation_set_*` 和摘要。快照含准确案例、原材料、白名单来源、所有相关人工/自动化历史与取代链接。后续合法新增或取代标注产生新快照，不改变旧版，也不使旧版失效；原记录、收据或来源篡改会使读取和重放拒绝。
4. 核对一组已经存在的声明时，明确提交准确 set ID/摘要、`human` 或 `automation` 来源、声明者、带时区时间、纯文本执行参考和选中**所有案例、全部五维**的判断及理由。执行参考不会打开路径、联网或被提升为实际运行证明。少案例、重复案例、错摘要和漏维度均拒绝。
5. 导出有界 JSON bundle，保存原始快照和核对结果，再运行离线核验。导出不会新增标签、收据、结果审核或 claim。

## 如何解释状态和数字

| 状态 | 判定 | 核对处理 |
|---|---|---|
| `pending` | 没有活动人工声明 | 排除，保留 `reference_pending` |
| `unknown` | 人工声明一致为 `not_assessed` 或 `not_applicable` | 排除，保留 `reference_unknown` |
| `conflicting` | 同一维不同人工审核者的 outcome 不同 | 排除，保留 `reference_conflicting` 及全部 outcome、未知 outcome |
| `eligible` | 所有活动人工声明在该维一致为 `passed` 或 `failed` | 可比较；提交方未知时仍排除为 `declaration_unknown` |

理由文本不同而 outcome 相同不会制造冲突。人工声明与自动化声明始终分开，自动化只作为审计附件。`not_applicable` 不被当成真值或通过。

核对返回匹配维数、可比较维数、总维数、排除数量及原因。`declaration_agreement_rate = matched_dimensions / comparable_dimensions`；没有可比较维度时为空。该数字是**所提交声明的一致性**，没有证明模型质量、论文解释的客观正确性或独立最终测试表现。`semantic_quality_score` 始终为空。执行参考及真人身份的验证标志为 false；材料标签与核对不授予任何实验或结论审批权。

## 导出与独立核验

四项来源只有固定白名单。原材料、v018 manifest、Momentum 来源登记表以 UTF-8 文本冻结，每项不超过 512 KiB；Alpha101 PDF 只冻结摘要，不放入 HTTP JSON。整个 set/bundle 不超过 16 MiB，创建/核对请求不超过 256 KiB，各类记录和收据分别不超过 500 项。所有 set 和 comparison 正文合计有 16 MiB 预算；读取前用 SQL 字节统计拒绝非文本、单条超限或总量超限，提交前也检查新增量。Momentum 原 PDF 不在本模块当次核验范围。HTTP 不接受本地路径。

本地导出只读既有已升级工作区，要求显式新文件，不能覆盖旧文件或写入权威工作区；它不会初始化、迁移或种入示例数据：

```sh
.venv/bin/python scripts/export_semantic_set.py \
  --workspace /absolute/path/workspace \
  --set-id semantic_evaluation_set_<digest> \
  --out /absolute/path/new-reference.json

.venv/bin/python scripts/verify_semantic_set.py /absolute/path/new-reference.json
```

默认核验包含的 UTF-8 内容、固定材料/来源摘要、原标注及取代链、参考状态、比较数值、ID/摘要、时间元数据和 bundle manifest，并比对安装包内四项原文件。`--source-root /absolute/path/bundle` 使用另一明确来源根目录。`--snapshot-only` 只验证已包含的快照及摘要，不声称复验未导出的 PDF 字节。CLI 拒绝重复 JSON key、非有限值、符号链接和超限文件。

摘要能检测内容变化，不能证明某个真人确实写过该标签，也不能证明某个被隐瞒的审核者存在。完整活动人工引用由工作区账本在冻结时验证；外部导入的自制 JSON 不能据此获得认证身份或评测真值。

## 恢复和测试

创建与核对各有独立耐久幂等收据。同键同请求返回原资源；同键不同内容返回 409。并发提交通过 SQLite 事务串行化。资源与收据在一次事务提交，最后再次核验源文件及原标注。SQLite 与文件不能组成绝对原子域，最后 fence 覆盖可观察的提交前变化。

专项测试用临时工作区里的明确合成标签覆盖全部人工参考分支，没有实际标注。八项端到端例子只写 automation，真人记录为零：

```sh
.venv/bin/python -m unittest tests.test_semantic_evaluation_sets -v
.venv/bin/python scripts/demo_v020_semantic_sets.py --out artifacts/new-v020-semantic-demo
.venv/bin/python scripts/verify_semantic_set.py \
  artifacts/new-v020-semantic-demo/semantic-evaluation-bundle.json
```

未来接入模型时，可把准确模型响应及调用来源转换为本合同的完整声明，再另行定义模型质量、固定基线和独立保留集；本轮不产生这些数字。真实效率基线仍来自六阶段人工观测，不能由材料标签、系统运行时间或上述一致性率推断。
