# v0.11 冻结候选时序接口

审核页的三组基础图读取引擎保存的逐日结果，原始因子、收益、IC 和冻结指标不会在前端重算。后端只增加有效日毛收益的算术累计，以及有限信号资产数 / 冻结股票池数量的覆盖比例。

## HTTP 契约

`GET /api/runs/{run_id}/candidates/{candidate_id}/series?attempt_id=...&result_digest=...`

两个查询参数必须来自所选运行的 `review_targets`。这里的 `result_digest` 是整个 candidate 的摘要，包含实际表达式、尝试和结果；它不是内部 result 的摘要。后者另存于响应的 `source.result_sha256`。

`CandidateSeriesResponse` 是禁止额外字段及非有限数字的 Pydantic 响应模型，包含：

- run / research / revision / attempt / candidate 的精确身份与修订、候选摘要。
- 原始 result 及可下载 export 的摘要、artifact 身份和 `/daily` JSON 指针。
- 冻结数据版本、种类、数据集摘要、股票池数量，以及 validation 起止日和 min_assets。
- 已保存的收益、Rank IC、覆盖和有效日数量汇总；不提供新计算的 Sharpe、回撤或交易成本指标。
- 每个信号日的 signal / entry / exit 日期，evaluated / skipped / purged 状态、原因、毛收益、算术累计、IC 及其状态、可用资产数量和覆盖比例。

失败候选没有 result 时返回明确错误，不替换为其他候选。`not_evaluable` 候选若存在有效保存的逐日结果，可以查看全部跳过原因和覆盖；所有收益保持 null。普通 run 接口仍不返回逐日数组。

## 口径和空值

收益的时间位置是 exit_date，即完成持有期的开盘日。IC 和覆盖使用 signal_date，保留对应的入场和出场日期，避免把信号形成与收益实现混成同一天。

收益与累计均为比例（1 = 100%），不是复利资金净值或 BRAIN PnL。累计使用 `math.fsum` 对截至该日的有效毛收益求和：跳过和边界剔除日的图点是 null，后续有效日继续累计。真实零收益或零 IC 不会被转成缺失。

常量因子产生 skipped / constant_factor；资产不足产生 skipped / insufficient_finite_assets。常量未来收益仍可有 evaluated 毛收益，但 IC 是 null / constant_forward_returns。purged 日期在旧产物中使用 available_assets=0 作为初始化占位，此接口投影为 available_assets=null、coverage=null，不能解读成真实零覆盖。

## 核验和资源约束

服务在 SQLite `query_only` 的 read transaction 内固定数据库视图，不申请写锁。复用既有有界文件核验和 `Store._check_integrity`，检查真实快照、原始 result、下载 export、精确候选和 review target 摘要。新修订不会改变旧运行的曲线；新 attempt 不会悄悄替代所选 attempt。

历史执行口径从已核验源文件的静态 `EXECUTION` 字面量读取，绝不执行历史 Python。源代码清单摘要、结果内口径与本投影支持的 next-open validation 口径必须一致；未知历史口径拒绝显示，不使用当前默认值填补。

还会核对逐日顺序、冻结区间、入场和出场日、purge 边界、空值语义、有效日数量以及 summary 与逐日结果的一致性。计数精确比较，浮点聚合允许 `rel_tol=1e-10, abs_tol=1e-12` 的数值求和误差；summary 仍原样复制产物。

限制为 4000 个信号日、2 MiB 响应、16 MiB JSON / run state，快照继续使用既有单文件 64 MiB、总量 256 MiB、3000 文件上限。更长但引擎合法的结果仍保存在完整下载中；接口返回 413，不做隐藏截断或采样。错误 422 表示缺少或无效请求身份，404 表示运行或候选不存在，409 表示目标过期、无计算产物、未知口径或完整性校验失败。

## 验证

`python -m unittest tests.test_candidate_series tests.test_candidate_series_api -v`

测试使用独立临时工作区，包含实际 worker → SQLite → HTTP 路径；手算用例独立指定累计、覆盖和聚合；覆盖真实零值、缺口、常量未来收益、全无效候选、旧修订绑定、错误身份、篡改、未知历史口径、大小限制，以及核验期间另一 SQLite writer 可以提交。HTTP 读取不创建审核或人工观测。

这些测试验证软件读取和呈现契约，不代表真实市场表现或人工评审效率提升。正式研究数据接入及最终测试协议仍是独立后续任务。
