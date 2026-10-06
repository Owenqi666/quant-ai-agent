# v0.9 六阶段观测回流

此模块将已经实际填写的观测保存为不可变记录。它不会生成真实人工实验，不根据 HTTP、worker 或 E2E 日志估算人工研究耗时；`source=human` 和参与者身份均为用户声明，不是身份认证。自动化验收记录使用 `automation` 并排除在人工对照之外。

## 数据结构与入口

业务类为 `paper_alpha.server.workflow_observations.WorkflowObservations(store)`，通过 Store 组合复用已有工作区。版本化模型位于 `workflow_observations_schema.py`，未知字段被拒绝。schema 7 新增 `workflow_observations` 表，既有论文、修订、报告、审核和请求回执保留原始内容；迁移不创建任何观测样本。

API：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/workflow-observation-template` | 获取协议摘要与未填写模板 |
| POST | `/api/researches/{research_id}/workflow-observations/validate` | `{observation}` 预检，不写入 |
| POST | `/api/researches/{research_id}/workflow-observations` | `{observation,idempotency_key}` 不可变导入 |
| GET | 同上 | `limit=1..100`、`offset=0..500` 历史分页 |
| GET | 同上加 `/summary` | 有限范围的来源、完成情况、覆盖与个案对照 |
| GET | 同上加 `/{observation_id}` | 查看记录与冻结计算结果 |
| GET | 同上加 `/{observation_id}/export` | 导出核对摘要后的 JSON |

前端应先耐久保存原请求及幂等键。提交结果未知时，使用原键和原正文重试。同键同正文返回原始冻结结果；同键异正文或同一协议/参与者/session/condition 改键重复提交返回 409。一次事务完成所有绑定检查、重叠检查与插入，防止并发产生两份观测。保存成功不应被后续列表刷新失败解释为提交失败。

## 协议与绑定

首版只接收 `evaluation_suites/v07/human_protocol.json` 中已冻结的 Alpha101 熟悉任务。模板通过服务器给出原协议文件的 SHA-256，不修改 v07 文件。预检核对协议引用的原始任务、PDF、CSV、metadata 文件摘要。记录要求 `record_status=observed`、明确来源、参与者、会话、两路径之一、顺序、代码 commit、机器环境、允许工具、熟悉程度、练习标志及停止条件；空白模板仍为 `pending_human_observation`，不可导入。

工作台记录：

1. 必须绑定本研究的不可变 revision。对证据、假设、候选、评估和预算使用相同规范化规则，与固定协议逐项核对。工作台重写输入文件路径或显示标题不应使相同科学条件失配。
2. 核对论文、注册数据版本及实际保存文件。数据、公式、证据或评估条件改变时，需要另建协议，不能继续原比较。
3. 已执行记录绑定 run 和其具体 terminal attempt。支持仍保留的历史 attempt，不能用最新 attempt 替代。成功完成的观测要求已完成且通过完整性校验的实验，以及本参与者/来源的结构化审核；绑定审核必须指向相同研究、revision、run、attempt、候选及结果摘要。
4. 未运行、失败、取消或中断的观测可以保留为 `incomplete` 或 `abandoned`，必须说明原因。已运行的非完成观测同样保留具体尝试身份；不要求失败结果产生成功指标。
5. 五维判断与绑定审核一致；`completed` 只表示观察任务完成，不要求五维全部通过。`not_assessed` 不能声明完整完成。

CLI 记录中的外部运行位置、核验文字和报告引用只保存为声明文本，服务器不会读取其指向的本地路径或 URL，也不将它们标成已核验。CLI 不能借用工作台的 revision/run/attempt/review 标识。代码 commit、机器环境与允许工具亦为声明；即使文本相同，也不能声称已独立核对外部执行环境。工作台的 `outputs_verified` 对应实际实验快照校验，用户填写的报告引用仍是声明。

## 计时与缺失

六阶段固定为 reading、hypothesis、implementation、run_setup、review、report。区间类型为 active、waiting、away。只有 `status=ended`、起止时间均明确且为正时长的区间参与计算。时间必须包含时区，不接受未来时间、会话外区间、跨阶段或类型重叠。单个会话的明确时间范围最多 30 天。

`status=interrupted` 必须 `ended_at=null`，时长保持缺失，不能根据记忆补齐。若会话结束时间明确，未结束区间保守占用其后的会话范围，不允许另一个区间与它重叠。同一参与者和来源的已有记录若有明确重叠区间，新导入会被拒绝；两者结束时间均缺失时无法确定完整重叠范围，这个边界不用于推算时间。

每阶段的 active/waiting/away 时间保持可空。`observed_active_seconds` 等是已结束区间的部分和，不代表完整流程。只有全部六阶段均有已结束主动区间、且没有中断时才产生 `full_active_seconds`。六阶段均有记录仍不证明会话内每一分钟均被观察；原始区间和覆盖信息持续保留。完全没有区间时所有时间为 null，不作零处理。

审核时间不会自动加到总计中。若复制已保存的审核计时，须在该 review 阶段的 active 区间填写 `source_reference=review_id`，其起止必须与已保存审核完全一致。复制后的区间只加一次；重复、重叠区间被拒绝。未声明为复制来源的手记审核区间按用户声明保存，不验证为审核计时器产生。

## 汇总与对照

汇总保留每条原始记录、ID、绑定限制与摘要。人工非练习记录作为明确分母；按 manual_cli/workbench 分别提供 completed/incomplete/abandoned、完成率、完整主动阶段覆盖条数、部分/未测量条数、中断条数和错误/返工文字条目数。返工条目数不进行语义分类，也不代表独立错误数量。自动化和练习记录独立显示，不进入人工效率比较。

仅当协议、原始任务身份、参与者、代码、环境、允许工具、熟悉程度和停止条件相同时组成一个声明条件组。每组恰好一条 manual_cli 和一条 workbench、两者完成且完整主动阶段覆盖、顺序不同，才给出 `workbench_minus_manual_cli` 主动耗时差。多个重复样本不会自动挑选较快的组合；缺少另一条路径、缺失时间或条件变化时给出不可比较原因和 null 差值。每个数字都可从同一响应中的 observation ID 和原区间复核。

此比较只是在声明条件相同下的单人熟悉合成任务个案，不验证 CLI 外部结果、不排除模板/熟悉/求助导致的学习效应，不证明普遍效率或市场表现。条件声明不完整、未执行和自动化样本不能补填成人工结果。

## 存储、导出与边界

数据库冻结原始 observation、计时、绑定、警示、ID、研究身份和创建时间，保存内容摘要及请求摘要。读取、列表、汇总、导出与幂等重放都核对它们；损坏记录返回 409。提交之后的原实验文件如果发生变化，不会改写历史记录；新预检仍校验当前文件，旧记录的核验声明是提交当时的快照。

一条请求最多 256 KiB，最多 300 个区间、20 个审核引用。一个研究最多 500 条记录；汇总最多 500 条/16 MiB，参与者冲突检查也限定 500 条。超过边界明确拒绝，不静默截断分母。列表分页有界。

验证命令：

```sh
.venv/bin/python -m unittest tests.test_workflow_observations tests.test_workflow_observations_migrations tests.test_migrations -q
```

所有测试使用临时隔离工作区，human 来源仅用于测试分支并标注 automated fixture；不向真实工作区写入人工观察数据。真实人工流程仍由本人按协议完成。
