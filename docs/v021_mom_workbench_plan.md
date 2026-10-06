# v0.21 行业 MOM 工作台接入计划与 Agent 分工

日期：2026-10-06（Europe/London）。沿用“盘点与评估 → 详细计划 → 独占模块并行实现 → 交叉审查 → 集成验收 → 发布交付”。AI、RAG 和全面 UI 设计继续后置。

## 已有实现与本轮缺口

基线提交为 `a88c1ea`，工作台代码为 v0.20 / schema16。行业 MOM 计算源码固定于 `424d40a`；已有真实来源例子在 `artifacts/mom-only-development-01/`。已完成 49 行业、36 个输入月、24 个开发月、固定十一月信号、同形成样本基线、独立 Fraction 数值参考、原始字节/配置/源码快照、严格核验与失败记录。67 项相关检查及禁止快照字节码写入的复跑已通过。这个例子没有接入当前网页。

现有 `MonthlyExperiments` 固定为 `controlled_fixture`，有 `mom_id` 和成本/净值代理字段；`DomainResearchJobs` 也只有月度夹具和作者诊断。`ResearchBindings` 是原 GJS 个股/MAT 准入，不能授予新的行业组合执行权限。`ResearchCases`、结果类型及准确审核来源还不接受行业 MOM。现有持久队列、worker 锁、子进程终止、事务幂等、案例快照、准确结论及人工审核可以复用机制，但必须保持数据和指标语义分离。

三项只读评估保存在 `artifacts/mom-workbench-assessment-01/`：后台、研究绑定/审核、前端。本计划根据已确认的接口缺口确定范围；开始实现前公布共同合同，后续变更记录在本文件。

## 本轮验收目标

用户可以在现有“研究执行”入口选择一份已注册的行业来源，提交一次后台 MOM 计算，查看状态与失败记录，看到实际 gross 指标及逐月输出，把准确结果保存为研究案例，再使用既有准确结论和人工审核入口。原始输入、方法、来源、尝试、数值输出和案例之间必须形成可核对的绑定。

首次接入只使用用户已选择的固定行业修改案例：2009-01..2011-12 输入、2010-01..2011-12 开发、2012-01..2013-12 保留。窗口、单位合同、49 行业轴、至少 30 个形成资产和权重固定。来源是当前版本的预计算行业组合，费用/借券/融资/成交仍未建模。候选净敞口 0，基线净敞口 1；差值不解释成风险调整后的 Alpha。原个股研究和其阻断记录保持独立。

## 数据流与操作顺序

1. **本地来源注册**：管理 CLI 接收已完成的 MOM artifact 目录、标题、说明及幂等键。调用现有严格 `verify()`，只接受完整且数值验证通过的固定合同。先在受控暂存目录复制全部已核验文件，再核验副本及原清单，建立不可变来源 ID/摘要和来源注册收据。工作区内自行保存源文件，后续不依赖外部 artifact 路径。保存原 invocation 声明，不能重写其中的历史绝对路径来冒充新的运行。
2. **浏览器选择**：HTTP 只提供来源列表/详情；创建实验只收来源 ID、准确摘要和幂等键。固定配置由后端读取，不接受任意文件路径、URL、代码、客户指标或真实性布尔值。无来源的新工作区明确展示本地注册说明，不静默使用夹具替代。
3. **创建研究执行**：新行业实验资源和独立表/目录保存来源快照、配置、创建请求与收据。排队、运行、取消、失败、中断和完成可见。提交后丢响应可重放相同请求；换摘要或负载必须冲突。
4. **后台计算**：现有 worker 增加独立行业队列，保留原日频/月度队列。以当前已交付引擎和注册来源输入启动 `python -B -m paper_alpha.mom_only_workflow run`，从原 inputs 读取证据，不执行保存的源码或 MATLAB。锁描述符、心跳、时间上限、取消和进程组终止保留。最多三次明确尝试；失败后只能显式重试，新尝试使用新目录，旧目录保留。
5. **发布结果**：运行输出先通过严格原始来源/方法/报告/独立数值核验，再在事务内检查 worker/attempt 所有权、原输入与源摘要、取消状态及输出清单，完成发布。验证通过仅代表计算正确，不能成为人工研究认可。中断和旧 worker 不能发布新结果。
6. **展示实际输出**：使用独立 typed DTO，显示两种策略的可用月份、月均毛收益、毛收益指数、波动率、回撤和逐月记录；显示来源、方法、代码、输入、结果和 manifest 摘要。提供已核验技术报告下载。未完成或验证失败的任务不能显示成功指标。
7. **绑定研究案例**：`ResearchCases` 新增 `industry_mom_experiment` 来源及 `industry_mom_portfolio` 结果种类。由服务器从准确已完成尝试派生 evidence、definitions、来源/方法/配置/代码/attempt/input/result/manifest 快照及限制。通过现有案例摘要和事务 fence 保存，拒绝过期、篡改、未完成或错域来源。
8. **准确审核与报告**：复用既有 Case → ResearchClaims → ClaimReviews，支持新来源种类。数值结论仍引用具体 result ID、摘要与 JSON pointer；导出由保存的工具输出派生。页面跳转时传递准确案例/结论身份。自动化示例只能以 automation 来源记录技术判断；真实工作区不自动创建人类质量标签、语义参考集或耗时成绩。

## Agent 分工及独占文件

| 执行者 | 独占范围 | 工作及验收 |
|---|---|---|
| Agent A：后台执行（`v019_assess_execution`） | 新 `server/industry_mom.py`、`industry_mom_schema.py`、`industry_mom_storage_schema.py`、`industry_mom_runner.py`，新后台/runner 测试 | 来源注册、严格读取、独立实验队列、准确请求收据、尝试 fence、取消/重试/恢复、子进程执行；尽早发布具体方法签名和 DTO，Root 接入 migration/API/worker |
| Agent B：研究绑定（`v019_assess_lineage`） | `research_cases.py`、`research_cases_schema.py`，必要时 `research_claims.py`/其 schema、`claim_reviews.py`/其 schema，新绑定测试 | 新行业来源的准确案例投影，固定证据/项目改动、完整来源与结果绑定；旧三类案例字节兼容；新领域的准确结论和审核可用，不新增虚假人类标签 |
| Agent C：基本前端（`v019_assess_evidence`） | 新 `components/IndustryMomExperiments.tsx`、`industryMomExperiments.ts`/测试；`App.tsx`、`components/ResearchCases.tsx`/`ResearchClaims.tsx`/`ClaimReviews.tsx` 的必要接线；新 E2E | 现有研究执行入口增加独立行业路径，准确来源选择/可恢复提交/轮询/实际输出/案例和审核跳转，空来源与错误可理解；沿用当前基础样式 |
| 主 Agent | 本计划、集成合同、`db.py`、`api.py`、`runner.py`、CLI/示例、能力文案、生成契约、版本/发布/迁移/备份/README、集成测试与实际验收 | 提前明确共享接口；schema17 加法迁移，typed API，第三队列调度、注册命令、真实示例与交付；控制原工作区升级和快照记录 |

不得同时编辑其他人的文件。新增跨边界方法或改变 DTO 先同步；Root 独占生成 `frontend/src/generated/`。不得修改原固定 MOM 方法合同/计算器、旧 Alpha101 包、作者扫描和原已发布结果。Agents 只操作各自隔离目录，不写现行工作区，不提交 Git；主 Agent 负责集成与提交。

## 共享接口约定

- 模块类以 `IndustryMomSources`、`IndustryMomExperiments` 为准；Root 在 `/api/industry-mom-sources`、`/api/industry-mom-experiments` 添加 typed 路由。来源注册仅管理 CLI，本机落盘，不提供 HTTP 任意路径入口。
- 来源：`register(artifact_dir, title, note, idempotency_key)`；`get(id)`；`list(limit, offset)`。响应携带准确来源/方法/输入/manifest 摘要、注册时间、范围及只由核验生成的验证结果。
- 实验：`create(source_id, source_digest, idempotency_key)`；`get(id)`；`status(id)`；`list(limit, offset)`；`cancel/retry(id, expected_attempt_id, idempotency_key)`；`claim(worker_id)`、`heartbeat`、`recover_stale`、`finish` 等具体内部签名由 Agent A 及时公布。固定运行上限 60 秒，最多三次尝试，不开放任意最终区间。
- 完成结果必须具有 `result_json`、`reference_json` 和可信 attempt/input/result/manifest 身份；详细类型由新 schema 提供，不强转 MonthlyResult。
- 研究案例以 `source_id=experiment_id` 注册新 `source_kind`。Agent B 从服务已验证 get/projection 提取当前唯一已完成目标，再用现有 preview/source_digest 创建案例；旧作者 binding 不参与该授权。
- root 发布表迁移只导入纯 SCHEMA 模块，避免 db/service 循环；所有 HTTP 输出接入现有生成契约和运行时验证。
- 当前全局能力的 `synthetic_only` 需要明确限于旧日频能力；新增行业域能力单独表述，不把任何作者域/旧夹具升级成真实市场研究。

## 验证流程与必要用例

**合同与完整性**：拒绝错误来源 SHA、缺文件、额外文件、symlink/hardlink/路径逃逸、篡改原结果/方法/代码/报告/收据；复制前后字节一致，后续所有读/排队/claim/发布均验证自己的来源和账本。不能仅重算摘要就放行。明确本地完整性并不提供外部作者身份认证。

**任务生命周期**：相同请求并发/丢响应重放一次；同键异请求冲突；queued/running 取消；失败和中断保留目录；三次上限；旧 attempt/worker/取消后发布拒绝；第三队列不饿死旧两队列；`--once` 最多消费一个任务（准备失败也计一次）。

**研究与报告**：只有已完成准确行业结果可建案例；原 GJS 阻断保持；industry 字段不能走 controlled_fixture 的 MOM-ID/成本代理；新旧案例读取、数值 pointer 和审核摘要绑定均有效；人工指标初始为空，报告数字对齐实际 JSON。

**前端**：空源、提交错误/响应丢失恢复、排队/失败/取消、完成真实指标、查看逐月与冻结报告、建立案例与进入既有审核、工作区切换不串结果。浏览器测试使用隔离合成夹具和 automation 身份；真实例子/原工作区不写测试人类标签。

**集成与兼容**：先专项测试和前端 typecheck/build，再全套 Python/前端测试及相关浏览器场景。发布前固定计算源码；校验 schema16→17 的旧表/文件摘要保留，备份/还原、契约再生成一致、便携包中方法/config JSON 齐全。第三方原 PDF/作者数据/市场字节继续本地保存，不打进源码包；新环境无真实来源明确留空，通过操作者本地注册恢复来源。

**实际闭环**：使用已冻结真实 artifact 注册一份源，在隔离工作区通过真实 HTTP 创建队列任务，worker 完成 24 开发月，结果与原独立例子在相同计算语义下对照；建立新准确案例/结论、下载报告、重复请求验证。保留区间不评价。原 8765 仅在新包验收通过及备份完成后升级；记录运行代码/静态资源/数据库身份。若实际启动条件不满足，交付准确启动命令及阻断，不称部署完成。

## 有限完成清单

- [x] 三项评估和共享接口确认；详细分工已保存。
- [x] 来源注册及自己的不可变副本/收据完整。
- [x] 独立 typed 行业实验资源、第三队列、生命周期和发布 fence。
- [x] 新准确案例来源/结果、证据归因、数值结论与审核接通。
- [x] 基础前端可见真实输出并能进入案例/审核；不新增复杂配置。
- [ ] 专项、兼容、前端及相关浏览器检查通过。
- [x] 真实来源的隔离 HTTP/worker/案例/报告闭环可复现。
- [ ] 升级/备份/便携包/运行身份核验及启动说明完成。

完成后填写实际证据路径和验证结果，不提前填写成果数字。未经用户本人填写，质量审核和人工效率对照继续待填。

## 实施证据与验收状态

- A：后台注册、实验账本及运行监督；证据在 `artifacts/industry-mom-backend-01/`。读取报告期间来源发生变化的最终 fence 缺口已由 B 复现、A 修正，再由 B 复验立即拒绝。每实验新取消/重试收据设独立总预算；原键请求恢复仍可重放。
- B：新绑定专项 10 项、旧案例/结论/准确审核兼容 41 项通过；见 `artifacts/industry-mom-binding-01/findings.json`。准备失败的部分目录、显式新尝试、取消后发布拒绝、缺收据、备份恢复及原 invocation 字节保存已交叉核验。
- C：9 项单元检查、typecheck/build、11 项隔离 Chrome 场景通过；见 `artifacts/v021-frontend-c/result.json`。浏览器使用受控合同夹具；另只读解析真实 24 月结果，不作为真实浏览器任务执行证据。
- 主 Agent：`artifacts/v021-real-flow-01/result.json` 实际通过来源注册、真实 HTTP/worker 计算、准确案例/数值结论、自动化空判断、报告目标检查和完整备份恢复。24 开发月结果摘要与原 CLI 都是 `655bd72f1d7589ef5423163b711ae2614e30da6f51eea37cac1f78316b91d329`，保留区间未评价。
- schema16 冻结受控样例的副本升级通过，见 `artifacts/v021-schema16-clone-01/result.json`；旧业务表和历史文件不变，新五表为空。现行工作区仍须先备份、再对其具体副本核验后升级。
- 集成专项最初下载路由遗漏非 JSON 测试目录，已保留失败日志并补明确 Markdown schema/准确目标参数断言，相关 3 项重跑通过。完整发布门禁、便携新环境安装和现行工作区升级仍待最终验收，不能把本段视为已部署证明。
- 首轮完整发布在 Python 阶段发现旧人工审核夹具使用当前 runtime，却硬编码 v0.20/schema16。独立复现版本一致性拒绝后，提前中断该轮隔离测试子进程；未完成的阶段不算通过。`artifacts/release-v021-01/acceptance.json` 与隔离日志保留。夹具改为读取自己的真实 runtime/数据库声明；原人工审核准备 CLI 同时支持准确的 v0.20/schema16、v0.21/schema17 两个组合，仍拒绝版本/数据库错配，并保留负例验证。后续完整门禁使用新目录。
