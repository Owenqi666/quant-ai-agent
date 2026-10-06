# 跨研究的保留区间保护

## 保护规则

v0.18 在现有 Store 写事务中增加时间保护。保护身份是已核对的 `market.csv` 内容 SHA-256，不是研究 ID、论文名、数据登记 ID 或 metadata 的版本名称。同一个 CSV 重新登记或复制为新研究，继承相同的保留区间。

每个不可变研究修订同时保存两种声明：

- `test`：当前修订声明的保留区间；所有历史保留声明共同有效，修订不能释放旧区间。
- `development`：数据日历第一天至当前 `validation.end`。现有 worker 在这一完整前缀计算因子，以供滚动窗口预热；仅检查命名的 train/validation 起止日期会漏掉中间的保留区间。

新声明必须同时满足：开发前缀不接触同内容数据的任何既有 test；新 test 不接触任何既有开发前缀。后一条避免把已用于开发的区间再次描述为新的未开发保留区间。规则是保守的：创建修订即声明开发范围，未实际运行也不能使这个范围重新成为未触达 test。

例如 Alpha101 原本保留 `2023-04-03..2023-07-14`。将 validation 改成 `2023-04-03..2023-06-30`、将新 test 改成七月仍会拒绝。将 train 与 validation 一起移到旧 test 后面也会拒绝，因为计算预热前缀仍包含旧 test。

## 服务与原子性

`paper_alpha.server.research_guard` 导出 `SCHEMA` 与 `seed_history(connection)`，由 schema14 迁移注册。新增表：

- `research_guard_data`：内容身份。
- `research_guard_boundaries`：修订、数据、任务摘要及区间的不可变绑定。
- `research_guard_unresolved`：无法解析的历史范围及可用内容身份。
- `research_guard_events`：提交、领取与重试记录。

`Store.create_research`、`create_revision`、`submit_run`、`retry_run` 均在原 SQLite 写事务内检查保护；拒绝同时回滚业务写入、边界与收据，不产生半份修订。原幂等收据先于保护检查重放，同键改请求仍冲突，不会因新规则使已确认的返回消失。

领取阶段也验证当前修订：schema13 中已排队的危险修订不能在迁移后继续计算。领取失败保留 run ID，产生 `temporal_guard_blocked` 事件及失败原因，不创建 attempt、计算文件或成功结果。正常取消、并发领取、尝试所有权与队列接口保持原行为。

每次执行前重新核对原 CSV、metadata 文件、登记摘要、任务摘要和保护绑定。检查以原 `revisions` 与 `datasets` 为登记覆盖依据，按行核对同内容历史的全部声明；删除旧保护行、改旧保留日期或篡改旧修订都使内容范围成为 `unresolved`，新复制研究不能据此获得额外开发权限。核对通过数据库游标逐条读取受限任务，不全量载入历史任务列表。外部编辑输入文件或修改保护行均不能获得执行授权。

`ResearchGuard(store)` 提供：

```python
guard.get(research_id)                 # GuardStatus: 同内容保护与历史冲突
guard.list(limit=20, offset=0)          # GuardPage
guard.check(dataset_id, task)          # GuardCheck: allowed / reason / 区间
```

`check` 是只读预检；最终 mutation 事务才是授权依据。模型均使用严格的闭合字段。状态输出最多 1000 条边界及 1000 条冲突，并报告总数和省略数量；分页最多 100 个研究。

## 历史迁移与恢复

迁移只向新表写入既有修订的声明，不改变研究、修订、run、审核、收据或原文件。历史危险范围保留为 `historical_conflict`，不会制造“过去从未看过”的结论；无法解析的旧输入保持 `unresolved`，提交与领取拒绝，而不是为了迁移改写旧任务。同内容数据也不能通过复制研究绕过未解的历史范围。若旧 metadata 和原市场文件都不足以确认内容身份，则保守阻断新声明，等待明确的数据修复与来源审核。

状态显示提交 run 数与实际存在 attempt 的 run 数。声明范围和已开始的运行是不同信息；它们都不能证明使用者在其他软件中没有读过数据。备份与新目录恢复保存保护表，恢复后继续拒绝相同越界请求。

## 验证与边界

`tests/test_research_guard.py` 使用实际 Store、schema13 迁移与备份恢复，验证跨修订、复制研究、同 CSV 改登记 metadata、完整预热范围、新 test 反向污染、并发互斥、幂等收据、输入/绑定篡改、旧保护记录删除、旧修订篡改及历史排队/重试。

```bash
.venv/bin/python -m unittest tests.test_research_guard -v
```

当前身份按完全相同的 CSV 字节归并。重新编码、改变数值精度、裁剪或追加历史数据后的不同 CSV 尚不自动推断观测重叠，需要独立的数据来源审查。当前保护覆盖本地日频 Store 路径；月度受控示例及作者准入研究的语义和最终市场测试仍分别记录。本轮不提供最终测试执行或解锁接口。
