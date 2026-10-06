# v0.13 研究规则接口约定

本文件是本轮 Agent 的共享实现接口。核心计算只接受 controlled_fixture 日总收益，属于信号与覆盖诊断，不产生未来收益、组合净值或交易结论。

## 配置

`schema_version=1, mode=paper|project, mom_window_months, mom_skip_months, id_window_months, id_skip_months, missing_policy=unresolved|complete|available, min_coverage, max_missing_run, zero_policy=include, fill_policy=none`。

月份窗口范围 1–36，跳过期 0–12；目标月限定 1905–2099 的 YYYY-MM。原文模式固定 MOM 11/1，ID 窗口与缺失参数为 null，missing_policy=unresolved，禁止在原文身份下填写推测规则。项目默认 MOM/ID 均 11/1，complete、min_coverage=1、max_missing_run=0。available 是显式项目变体：min_coverage 在 (0,1]，max_missing_run 在 0–366；复合收益只来自观察到的有效日收益，不代表完整窗口收益，输出必须带该说明。缺失不补零，零收益进入分母。

## 核心模块 paper_alpha.research_protocol

- `presets() -> list[dict]`：每项 id/title/description/config/sources/unresolved。
- `validate_config(config) -> dict`：拒绝未知字段、非有限值、bool 充当数值和伪造原文配置，返回规范配置。
- `resolve_windows(config, target_month) -> dict`：target_month、as_of（上月最后一天）、momentum/id 两个窗口（start/end 或 null）。
- `demo_bundle(target_month) -> dict`：虚构工作日日历与可手算的完整、缺行、空值、历史不足资产，足够覆盖允许的最大回看；不包含市场数据。
- `preview(config, target_month, bundle=None) -> dict`：无 bundle 则使用 demo。结构见下。

bundle 字段固定为 schema_version、data_kind=controlled_fixture、source_id、return_semantics=daily_total_return_decimal、calendar（id/version/start/end/sessions）、assets（id/history_start）、returns（date/asset/value，value 可 null）。sessions 声明 start/end 内完整开市日集合；日期严格唯一、按日历判断缺失，收益行不得位于非 session。范围不能覆盖窗口时拒绝，不能假装低覆盖。-100% 允许，小于 -100% 或非有限值拒绝。输入总量有界。

preview 返回 schema_version、semantics_version、config、config_digest、input_digest、data_kind、source_id、windows、status（blocked|ready|partial）、warnings、assets。

assets 每项固定 asset、status（ready|unavailable|blocked）、momentum（number|null）、pret（number|null）、id（number|null）、momentum_coverage、id_coverage、reasons。coverage 为 null 或 expected/valid/positive/negative/zero/missing_rows/null_values/coverage/max_missing_run/missing_dates（date/reason）。按 session 列表计算连续缺失，不将周末打断或加入连段。最少一个有效日才可能输出；任何 required window 早于 history_start 时，标 history_insufficient，不因允许 available 自动缩短历史。paper 模式返回 blocked，不输出信号，日期可解析并列出未决问题。每个字段不足资格时空值和原因必须一致。有限输入产生溢出也应为空且有原因；禁止将 NaN/Infinity 序列化。

## 保存与 HTTP

新增独立资源，不伪装成旧 Task/实验，也不修改 Alpha101 的日度契约。

- GET /api/research-protocols/presets → `{presets: [...]}`。
- POST /api/research-protocols/preview → `{config,target_month,bundle?}`，返回 core preview。
- POST /api/research-protocols → `{title,note,parent_id?,config}`，返回保存记录。
- GET /api/research-protocols?limit=20&offset=0 → `{items,total,limit,offset}`。
- GET /api/research-protocols/{id} → 保存记录。

保存记录字段：id/title/note/parent_id/config/config_digest/digest/created_at/changes（field,before,after）。源依据由 core presets 绑定，不接受用户提供的原文依据。服务模块 `ResearchProtocols(store)` 提供 create(title,note,config,parent_id=None)、list(limit=20,offset=0)、get(id)。记录为不可变内容寻址：id 为 `protocol_` 加 64 位内容摘要；相同正文与父记录重复保存返回原记录。正文摘要包括 title/note/parent_id/config/changes 和语义版本；created_at 不参与 id。读取核验摘要，父记录核验且不能删除。原文到项目、项目参数变动必须产生新记录及差异。新根记录的 changes 对比对应 mode 的服务器预设。

schema 9 新增 research_protocols 表：`id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,parent_id TEXT REFERENCES research_protocols(id)`。主 Agent 负责迁移/API/Pydantic与前端生成契约；服务 Agent 不改 db.py。

## 界面

新增轻量“研究规则”入口，不依赖已有 research/dataset。默认项目约定，论文预设可查看未决项。基本操作为选择预设、预览日期与覆盖、保存版本；修改窗口或高级缺失规则时清楚显示项目变体，原文预设不允许被隐式改写。说明 available 仅复合已观察到的收益。预览使用演示数据且明确标示，不能出现收益回测成功文案。异步结果绑定配置/目标月/工作区，编辑或切换后旧响应不得覆盖当前结果。保存失败后保留原请求重试，内容寻址保证无重复；刷新可通过记录列表读回。
