# v0.19：跨路径受控研究执行

`DomainResearchJobs` 在独立控制层内复用现有月度队列及作者准入服务。v0.18 日频 `ResearchJobs` 的接口、原月度请求及旧记录不变。真实 AI 提供商未接入；科学停止不会获得收益实验权限。

## 两种闭合来源

- `monthly_fixture`：绑定现有 protocol ID、精确 digest 和月度配置，仅对明确的 `project` 规则运行既有 `controlled_fixture` 月度 MOM/ID 比较。论文仍未决的 DGW 内部日频窗口/缺失政策保留为限制；项目约定不被写成论文结论。
- `author_study_diagnostic`：绑定现有 study ID、精确 digest，只重读已核验的聚合结果。样本阈值不足停止为 `DATA_INSUFFICIENT`；即使筛查通过也停止为 `AUTHOR_METHOD_UNRESOLVED`。没有股票池、排序、权重和持有收益实验，也不读取真实 MAT。

作业冻结来源、配置、预算、创建时间、说明和有界上下文。创建与 `validate` 都不产生月度实验；只有显式 `submit` 使用原效果键调用 `MonthlyExperiments.create`，进入现有 worker 队列。

## 状态与动作

月度主链：`created → validate → validated → submit → submitted → observe → observed → complete → completed`。观察等待中的实验仍为 `submitted`，一次动作只读一次状态，HTTP 内不睡眠或无限轮询。实际失败/中断/取消终止该作业，不自动增加尝试。作者诊断在 `validate` 后稳定 `blocked`，原输入、证据、实际聚合结果和停止理由保留。

终态包括 `completed/blocked/failed/exhausted/cancelled`。`complete` 只接纳精确原 attempt 的核验结果；实际 summary/信号/收益代理来自原引擎，不接受调用者提供数字。任务完成时只冻结原实验、协议、结果、核验与审核目标，不拉取之后新加的审核/报告。用户可用现有 ResearchCases 控制另建精确结果上下文，作业不代写人工批准。

## 父预算和持久恢复

预算复用原 `JobBudget`：`max_steps=1..100`、`max_failures=1..20`、`max_seconds=1..600`。总墙钟从作业创建起算，包含用户等待、队列等待、准备、worker 子进程、校验及发布。月度原单次 600 秒上限仍保留，与父任务剩余预算取较小值。新作业端点不接受 deadline 覆盖或预算补充。

每个动作先保存原请求、序号与效果键，再 dispatch。网络回应不确定或进程在原月度事务提交后退出时，下一 owner 先使用原效果键核对原 receipt；回应丢失不能创建重复实验。已经存在的效果可在预算截止后追回身份，其收据仍保存并转为 exhausted；不能藉此授权新计算。

月度 create 在同一原队列事务内写：

1. 原 `monthly_receipts` 的 create 效果键与实验身份；
2. 独立 `domain_monthly_effects` 父子绑定；
3. 原 `monthly_events` 的 `domain_parent_bound` 标记。

原 `queued` 事件另保留提交 key 的 origin 标记。因此三处父锚同时被删时也不会退为普通月度；已确认的 submit 输出还用于有界精确反向查找。任意篡改全部数据库记录不属于数据库身份认证，本控制层只对仍有原锚/声明的所属实验做完整性拒绝。

`domain_execution_remaining_seconds` 交叉核验上述三处、父任务元数据/账本、原 protocol/config 和 durably charged submit。删 submit 步骤、删任一绑定、改预算不能令实验变成无预算实验，计算/发布会失败。没有父任务的历史/普通月度实验保持原契约；域 key 未获授权时原月度 create 拒绝。

claim 阶段检查排队截止，supervisor 在启动和计算期间检查剩余预算，finish 在核验后及实际发布事务中再核验。超时/损坏保留输入和已存在输出文件，结果不会登记为 completed。原月度 `_verified` 的 `ServiceError` 现在被保存为 failed，不留下假活跃 running。

预算关闭后最多允许一条安全 `cancel` 收据。这一收据只能停止所属已有实验，不能增加计算；未知响应复用该取消键。取消未发生的 submit 不会为了恢复而新建实验。月度取消带精确 `expected_attempt_id`，作业不能采用后续外部重试的 attempt；晚来的跨尝试动作被拒绝。

## 有界数据及接口

请求/冻结正文最大 512 KiB（请求自身 256 KiB），步骤最大 3 MiB，单步输出最大 2 MiB，累计账本最大 32 MiB并预留小型错误收据。上下文省略原始市场/MAT/任意文件，科学输出和模型解释仍为 `semantic_fidelity=unverified`。

```python
DomainResearchJobs(store).create(source, budget, idempotency_key, note='')
get(id)
list(limit=20, offset=0, source_kind=None)
advance(id, action, idempotency_key)
cancel(id, idempotency_key)
markdown(id)
```

`source` 精确格式：

```json
{"kind":"monthly_fixture","protocol_id":"protocol_<sha256>","protocol_digest":"<sha256>","config":{"schema_version":1,"start_month":"2025-01","end_month":"2025-02","cost_bps":10,"min_assets":18}}
```

```json
{"kind":"author_study_diagnostic","study_id":"author_study_<sha256>","study_digest":"<sha256>"}
```

动作仅 `validate/submit/observe/complete/cancel`。模型位于 `domain_research_jobs_schema.py`：`DomainJobCreate/DomainJobAdvance/DomainJobStep/DomainResearchJob/DomainJobSummary/DomainJobPage`。响应 `provider_connected` 固定 false。终态 Markdown 由冻结步骤时间生成，重复导出一致；活动态须完成或停止后导出。

## 示例与验证

```bash
.venv/bin/python scripts/demo_v019_domains.py --out artifacts/domain-v019-new
.venv/bin/python -m unittest tests.test_domain_research_jobs tests.test_monthly_runner tests.test_monthly_experiments -v
```

输出目录必须不存在。演示创建独立工作区，真实 worker 执行月度项目夹具，另运行同协议/配置固定基线，完整 engine result 相等；保留6项停止：原论文规则未决、作者样本阈值不足、作者筛查通过仍缺投资组合方法、步数耗尽、取消及实际墙钟排队到期。示例不写真实人工标签，不调用模型。

测试另外覆盖丢失回应、真实进程退出、并发提交、任一绑定/父预算篡改、真实长子进程墙钟停止、核验后发布截止、校验错误失败保存、安全取消和备份恢复。固定基线使用相同计算器，证明控制层集成一致，不能证明独立数值正确、真实市场收益、论文复现、AI优势或减少人工耗时；月度独立参考验证继续由原工具负责。
