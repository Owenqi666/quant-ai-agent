# v0.12 执行阶段、输入准备与结果发布

## 目标及契约

执行模块 `paper_alpha.server.execution_lifecycle` 提供 `claim(store, worker_id)`、`finish(store, run_id, worker_id, attempt_id, status, error=None, verification=None, timings=None)` 和 `record_phase(store, run_id, worker_id, attempt_id, phase)`。Store 以薄包装调用，公开状态与原响应保持兼容。

阶段是 `execution_phase` 事件的 `payload.phase`，取值 `preparing / executing / verifying / publishing`。阶段只解释进度，不意味着结果已通过校验；运行最终状态和 verification 仍是原有发布事务的结果。同一阶段连续记录不会重复插入事件。

## 准备输入

claim 的短写事务只选择 queued 任务、保存精确 attempt、设置 running 和记录 preparing。事务提交后才创建 attempt 目录、校验和复制 PDF/行情/任务，文件 I/O 不持有 SQLite 写锁。复制后再次核对摘要，防止来源在复制期间改变。

准备失败会保留该 attempt 的文件和错误，并消耗一次 attempt。准备时取消会保存 cancelled，不返回可执行任务。进程被强制结束时，目录创建之前已存在数据库归属；重启恢复将未完成 attempt 标为 interrupted，仍需使用已有显式重试操作，不自动猜测输入或输出是否完整。

## 心跳和取消

`heartbeat_scope` 在准备输入、运行、核验和发布期间持续更新 heartbeat。它只写 heartbeat 字段，不反复解析大结果；同一线程嵌套调用复用现有 scope。计算阶段已有引擎事件采集仍保留。

每次阶段变更、心跳、准备边界和最终发布都检查 run / worker / attempt 所有权。旧 attempt 不能更新新的心跳、发出阶段、注册文件或提交成功。心跳属于观测，执行排他仍由已有继承进程锁保证，不能因数据库暂时繁忙就启动第二个 worker。

取消会在文件工作边界检查，并在最终数据库事务重新检查。取消期间已经完成的导出文件仍可留在原 attempt 目录，但不会注册为可信下载结果。

## 发布及中断

原始输出仍经过 verify_run、输入及修订绑定核验。之后进入 publishing，使用临时文件、flush/fsync、原子替换与目录同步创建逐份可下载 export。只有全部准备完成后，短事务才同时写入终态、state 摘要和完整 artifact 清单。

核验前捕获 output 源目录的文件与目录 stat/inventory；生成 export 时核对其 SHA256 等于原始内容经过既有路径隐去后的摘要，并保存每份 export 的 stat。导出完成及最终发布事务内分别重新比较源目录与 export 清单，覆盖 inode、ctime、mtime、mode、nlink、大小等信息；写锁内不重复全量哈希。新增、替换或修改文件会保存 failed、清空 verification、不注册产物，并保留失败文件。两个目录分别核对，避免正常生成 export 导致源目录误报。清单限制 4096 项 / 512 MiB，拒绝符号链接与非常规文件。

被强制结束后可能保留完整 export 或 `.pending` 临时文件。这些文件有 attempt 归属但没有成功注册，不会被当作成功实验。当前恢复策略是保留旧尝试、明确 interrupted、人工显式创建新 attempt；没有新增“自动认领已算完结果”功能。

这些文件保护不等同于对恶意外部文件系统的认证；后续读取、审核和下载继续执行原有完整性检查。

## 可复现的 claim 并发测量

脚本：`scripts/measure_execution_claim.py`。独立工作区通过现有导入验证链路构造 400 会话、240 个复制合成资产的合法数据，CSV 为 7,146,918 字节。复制资产只放大文件工作负载，不增加独立市场样本。不在性能测试中插入 sleep，每条原始测量均保留。

- 改动前：`artifacts/v012-claim-before-01/measurement.json`。
- 改动后：`artifacts/v012-claim-after-01/measurement.json`。
- 每边 5 次 claim，同时运行 heartbeat 和 submit 两类 writer。两边工作负载、CSV 摘要和 task 摘要相同。
- 改动前 10 个 writer 均在 claim 后完成；改动后 10 个均在 claim 前完成。
- 此次 warm-cache 样例中，writer 调用约从 10–24 ms 变为 0.7–3.3 ms；包含调度、锁等待和提交，不是纯锁等待。
- claim 自身约从 6.5–7.1 ms 增为 12.6–13.8 ms，因为增加了提前持久化 attempt、独立心跳及复制后摘要校验。该改动改善其他写入的并发进展，并不声称 claim 更快。

复测当前实现：

```bash
.venv/bin/python scripts/measure_execution_claim.py \
  --out artifacts/v012-claim-new-measurement \
  --implementation current --copies 20 --repeats 5
```

baseline 参数只应在含旧 Store.claim 的源码版本上运行；新版本包装完成后不得把新实现命名为旧基线。正式报告记录运行时 claim 源码和脚本摘要；测量不代表 HTTP 延迟、吞吐、冷缓存或真实行情表现。

回归发布测量使用 `scripts/measure_regression_concurrency.py`，冻结完整 service 模块后加载，其余依赖记录摘要。writer 在回归发布已经取得 SQLite 写锁时启动，故其完成必须等到发布释放锁，比较的是占锁范围及随后写入延迟，不是“在写事务内并发提交”。旧方法冻结源码与原始基线位于 `artifacts/v012-regression-before-01`，探索性后测 `after-01/02/03` 全部保留，不挑选最短样本。开发阶段同机可能有其他 Agent 的测试，不能把这些测量称为机器空闲下的对照；正式交付以统一验收在其他测试之后顺序执行的测量结果为准。

## 验证范围

`python -m unittest tests.test_execution_lifecycle tests.test_runner -v`

专属测试在隔离工作区覆盖：首次复制前 attempt 已落库、准备中另一 writer 可提交、复制失败和来源漂移、准备/核验/发布期间心跳与取消、旧尝试拒绝阶段/心跳/发布，以及准备和发布边界的真实 SIGKILL。强制结束测试核对原目录都有数据库归属、部分导出未注册、恢复不会自动运行，显式 retry 使用新 attempt 并保留旧记录。

另外覆盖核验成功后原始结果变更、export 在登记摘要前变更、最终写事务开始后原始文件或 export 变更（恢复原 mtime 仍必须拒绝），并确认写锁内没有重复文件哈希。这修复了旧 finish 中“读取时已发现篡改，但数据库仍保存 completed / verified=true”的发布窗口；并不取消后续读取校验。

进程组取消及继承锁继续由原有 runner 测试覆盖。小 FakeStore 测试仅屏蔽新的 SQLite 心跳/阶段调用，真实 SQLite 路径由上述生命周期测试及既有 worker integration 覆盖。发布验收还需运行整体 API、备份恢复和独立源码包检查；本模块测试不替代完整发布验收。
