# v0.14 共享月度实验契约

所有字段必填，除明确标注可空；封闭对象拒绝多余键，数值必须有限且 bool 不充当数值。新月度资源独立于旧 daily Task。

## 核心 M1

模块 `paper_alpha.monthly_evaluation` 导出 `SEMANTICS_VERSION='monthly-portfolio-v1'`、`MonthlyError(ValueError)`（code 属性）、`validate_config(config)`、`demo_bundle(config,rules)`、`evaluate(rules,config,bundle)`。

MonthlyConfig：`schema_version=1,start_month,end_month,cost_bps,min_assets`。月份1905–2099，含首尾1–24个月；cost_bps有限0–100；min_assets整数9–128；日期/规则使用 v0.13 已有严格校验。接口示例默认2025-01至2025-06、0bps、18资产下限。bundle 为原研究规则 controlled_fixture 日总收益契约；demo为版本化虚构日历和至少36资产，覆盖实际规则所需预热和完整目标月份，适度覆盖缺失负例；不得引入真实行情身份。

共同形成样本只由形成时点 MOM/ID 有效性决定，基线和对照同样本。MOM、ID分别升序三分组，exact-value ties不拆，平均秩 r（1起）、总数n，以 `min(3, floor(3*(r-0.5)/n)+1)` 定组；同值只用资产ID排序展示。各必需极端格至少一只，空格/全并列不降组数。原文规则仍blocked，不能补齐未知ID。

两条正式策略均gross exposure=1、net=0：`momentum` 基线对MOM最高组+0.5、最低组−0.5，组内等权；`mom_id` 为归一化低ID与高ID条件MOM差：低ID高MOM +0.25，低ID低MOM −0.25，高ID高MOM −0.25，高ID低MOM +0.25。属于项目比较定义，不是声称论文原式复现。九格为long-only等权月收益辅助诊断。

权重先形成，再取目标月完整日收益复合标签；持仓任何缺行/null/不完整日历/数值不可得，则该策略当月收益null，原权重保留，绝不按未来标签重归一化。非持仓缺标签不影响该策略收益。真实零收益保留。持有期标签规则固定完整，与信号available配置分开。

成本明确为target-weight proxy：Q=sum(abs(w_t-w_prev_target))，初月prev=0，turnover_proxy=Q/2，estimated_cost=cost_bps/10000*Q，net_return_proxy=gross−cost。无可形成权重的月份令prev未知；后续第一次形成权重仍可输出gross但成本/net未知，并将该期目标作为下一月基准，不能假装从零仓恢复。缺收益标签但已形成权重时仍更新目标基准。代理忽略持仓漂移、融资、借券、滑点；不能称真实执行表现。累计nav从1开始连乘(1+net)，遇任一null或net<=−1后不再恢复完整路径；保留后续单月结果。Sharpe按有效net月份、rf=0、样本std、sqrt(12)，不足2个月/零波动null；有效分母明确。完整累计/最大回撤仅全期路径有效才提供。

Result 精确结构：
```
{schema_version:1,semantics_version,data_kind:'controlled_fixture',source_id,
 rules:ProtocolConfig,config:MonthlyConfig,config_digest,input_digest,
 status:'evaluated'|'partial'|'not_evaluable'|'blocked',warnings:string[],
 months:MonthlyPeriod[],summary:MonthlyMetric[]}
MonthlyPeriod={month,as_of,windows:ProtocolWindows,status:'evaluated'|'partial'|'unavailable'|'blocked',
 eligible_assets:string[],exclusions:[{asset,reasons:string[]}],
 signals:[{asset,momentum:number,pret:number,id:number,mom_group:int,id_group:int}],
 groups:[{mom_group:int,id_group:int,assets:string[],gross_return:number|null,reasons:string[]}],
 labels:[{asset,return_value:number|null,coverage:ProtocolCoverage,reasons:string[]}],
 strategies:MonthlyStrategy[],difference_net_proxy:number|null,warnings:string[]}
MonthlyStrategy={id:'momentum'|'mom_id',status:'evaluated'|'unavailable',weights:[{asset,weight:number}],
 gross_return:number|null,traded_weight:number|null,turnover_proxy:number|null,
 estimated_cost:number|null,net_return_proxy:number|null,nav_proxy:number|null,drawdown_proxy:number|null,reasons:string[]}
MonthlyMetric={strategy_id:'momentum'|'mom_id',months_total:int,months_evaluated:int,gross_months:int,turnover_months:int,
 mean_gross_return:number|null,mean_net_return_proxy:number|null,
 volatility_annualized_proxy:number|null,sharpe_annualized_proxy:number|null,
 max_drawdown_proxy:number|null,terminal_nav_proxy:number|null,mean_turnover_proxy:number|null,cumulative_complete:bool}
```

config_digest 对规范 `{rules,config}` 用 storage.digest；input_digest 对原bundle用storage.digest。预览的旧config_digest语义不变，只在新Result明确不同对象。`monthly_reference.check(rules,config,bundle,result)`返回 `{supported:bool,passed:bool|null,checks:int,issues:string[]}`，独立复合/时间/分组/权重/收益，不调用生产计算/分组helper；blocked时supported=false,passed=null。非blocked结果必须得到reference passed才能发表。

months_evaluated为net有效月数，gross_months和turnover_months分别说明另两类均值分母。策略status按net是否可算；累计路径已断不阻止后续单月结果。持有期日历范围不完整时标签null并列出holding_calendar_incomplete，保留形成权重；现有coverage仅描述已声明sessions，不能用来冒充整个持有月覆盖。

## 冻结工作流 M4

`paper_alpha.monthly_workflow.run(input_path,output_dir)`：输入 `{schema_version:1,protocol:ProtocolRecord,config:MonthlyConfig,bundle}`。创建新目录，保存input.json/result.json/reference.json/report.md/manifest.json和版本化源代码快照，摘要绑定。失败目录保留。模块CLI `python -m paper_alpha.monthly_workflow run INPUT --out OUTPUT` 成功完成算术（包括blocked）exit0，异常exit2。`verify(output_dir,expected_input_digest=None)`完整核验并返回 `{verified:true,result_digest,input_digest,reference_passed:bool|null}`；report仅引用产物，不能手填指标。`render_report(result)`和`load_result`按需要导出，M2可用read_json读取result。

## 存储/监督 M2 与 API M4

M2独占 `monthly_storage_schema.py` 导出纯SQL `SCHEMA`，M4的db._migrate_v9逐statement执行。schema10新增monthly_experiments/attempts/events/reviews/reports/receipts；路径均由服务安全ID派生，不存需重定位的绝对路径。M2负责runner.py最小公平调度：相同worker_lock串行执行daily/月度、两队列轮转、共享workers健康心跳、持锁后恢复两类未完成任务。新monthly_runner调用既有stop_group和pass_fds，不改旧daily语义。

`MonthlyExperiments(store)`固定接口：
- create(protocol_id,protocol_digest,config,idempotency_key) → `{experiment_id}`。冻结服务器规则快照与生成demo bundle，不接受用户伪造source；正文同键重放原ack，异正文409。首次请求总量有界。
- list(limit=20,offset=0) → `{items:MonthlySummary[],total,limit,offset}`。
- get(id) → MonthlyDetail；status(id) → MonthlyStatus；get坏文件结果为409，不回传旧缓存指标。
- cancel(id,expected_attempt_id:string|null) → `{experiment_id}`。准确尝试不一致412。终态不改变结果。
- retry(id,expected_attempt_id:string,idempotency_key) → `{experiment_id}`。仅failed/interrupted/cancelled，最多3次；收据在状态变化后仍回放原ack。
- create_review(id,attempt_id,result_digest,verdict:'accepted'|'needs_revision'|'rejected',note,source:'human'|'automation',idempotency_key) → MonthlyReview；只能对完成且核验通过的确切结果，note<=4000。
- create_report(id,attempt_id,result_digest,idempotency_key) → MonthlyReport；冻结核验结果及当时reviews。get_report(id,report_id) →同对象；报告markdown由root/M2协商生成，API下载JSON和Markdown。
- claim(worker_id),recover_stale(stale_seconds=0),cancel_requested(id),heartbeat(worker_id,id,attempt_id),record_phase(...),finish(...) 由M2实现与monthly_runner配套；严格ownership与发布前后校验。

```
MonthlySummary={id,protocol_id,created_at,updated_at,status:'queued'|'running'|'cancelling'|'completed'|'failed'|'cancelled'|'interrupted',
 config:MonthlyConfig,attempt_count:int,attempt_id:string|null,error:string|null,phase:string|null}
MonthlyStatus={id,status,attempt_id:string|null,attempt_count:int,phase:string|null,change_token:string,integrity_checked:false}
MonthlyAttempt={id,number:int,status,started_at,finished_at:string|null,error:string|null}
MonthlyReview={id,experiment_id,attempt_id,result_digest,verdict,note,source,created_at,digest}
MonthlyReport={id,experiment_id,attempt_id,result_digest,created_at,digest,result:MonthlyResult,reviews:MonthlyReview[],protocol:ProtocolRecord,markdown:string}
MonthlyDetail={experiment:MonthlySummary,protocol:ProtocolRecord,attempts:MonthlyAttempt[],
 result:MonthlyResult|null,verification:{verified:bool,result_digest:string|null,input_digest:string|null,reference_passed:bool|null},
 review_target:{attempt_id,result_digest}|null,reviews:MonthlyReview[],reports:[{id,created_at,digest,attempt_id,result_digest}]}
```

所有接口前缀 `/api/monthly-experiments`。POST根=创建，GET根=list，GET/{id}=detail，GET/{id}/status，POST/{id}/cancel、/retry、/reviews、/reports，GET/{id}/reports/{report_id}及`/json`、`/markdown`下载。GET `/defaults` 返回 `{config:MonthlyConfig,data_kind:'controlled_fixture',source_id:string,warnings:string[]}`。创建body为create上述4参数；取消body为expected_attempt_id；retry/review/report body为签名去id。所有写请求复用现有输入边界，POST返回201（取消/重试200）。

界面只展示服务指标，不重算统计。提交/重试/审核/冻结报告使用原请求恢复；视图绑定工作区、选择与attempt。终态轮询用status，详情显式完整验证。不可将流水线completed显示成原文已复现或投资有效。
