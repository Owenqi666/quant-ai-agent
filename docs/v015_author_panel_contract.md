# v0.15 作者月度面板契约

状态：实施契约。范围固定为 Harvard Dataverse DOI 10.7910/DVN/R1UI1J，version 2.0 的 IntnlData.mat / USData.mat。

## 来源与边界

论文 p30 明示公开的是 scrambled / randomly deleted 数据；CC0 公共包不代表商业原始行情授权。MAT 无日收益，DGW 为作者预计算，不能据此还原日频 ID。原行号标识匿名资产，不能编造 ticker。Intnl 主文件有 ATGrowth/BM/COGStoAT/GPtoAT，但尚未证明等价于脚本缺失外部路径 Intnl_WCMonthly.mat。

本轮不执行 Table8 组合：其 winsorization、国家中性化、分位分组、未来标签筛除和权重与 v0.14 不同；全源 11 月完整窗口也十分稀疏。零可形成行是有效诊断，不能填补或挑选未来可用行凑收益。

## 输入

唯一 kind `author_perturbed_monthly_panel`，schema_version=1，adapter_version=`gjs-v2-monthly-panel-v1`。准确字段以 `paper_alpha/author_panel_schema.py` 为准。来源为固定元数据；source_row 为原 1-based 行号，连续切片最多 512 行。资产名 `<filename>:row:<source_row>`；Intnl country 按 NumAll/CountryCodes，US 为 null。输入上限 768 KiB。

目标月 H 对应连续 H-12..H 的 13 个自然月。Return 保存 13 个月；DGW/MV 保存 H-1 原值。US 保留 ymd 实际日期（不要求日历月末），Intnl 无日日期，以 null 保存。NaN/inf 映射为 null 并保留 value/nan/posinf/neginf 状态；有限原值（包括 MV=0、Return>1）不得截断或填补。

## 诊断及校验

MOM=prod(1+Return[H-12..H-2])-1，完整 11 月；跳过 H-1；DGW/MV 用 H-1；标签只用 H。formation_ready 仅表示项目诊断可用性：历史 Return 均有限且 >=-1、MOM 可表示、DGW 在 [-1,1]、MV>0。不代表原论文全部资格筛选或可投资组合。label_ready 独立判断 H Return >=-1，不能反向影响形成资格。

诊断包含原行逐月缺失/非法原因、MOM、DGW、MV、标签、可用性计数和边界。独立 Fraction 参考从冻结输入重算，校验结果和报告；报告只格式化工具数值。没有收益图、Sharpe 或市场复现声明。

## 工具 / 服务边界

`author_archive` 工具只读取指定 MAT，先核对固定源大小、官方 MD5 和 SHA256，检查 HDF 硬链接、shape、dtype、月份，按块扫描，按原行有界提取。大文件不上传 HTTP、不进入发布包。`author_workflow` 保存 input/result/reference/report/source/environment/manifest，独立验证，可带 --source 再次对照 MAT 原行。

工作台 POST /api/author-panels 输入 {title,note,panel,idempotency_key}；GET 列表、详情和 markdown。所有字段闭合；详情返回 {id,title,note,created_at,digest,panel,result,reference,verification_scope,raw_source_reverified}。verification_scope 固定 normalized_panel_and_diagnostics，raw_source_reverified=false：导入面板不等于工作台重新核验整个 MAT，不能仅凭声明的 source hash 认证原文件。CLI 完整验证另有清晰证据。

SQLite schema11 增加 author_panels/author_panel_receipts；导入前有界计算，事务内仅存储不可变 payload/摘要及幂等回执；同 key 不同请求 409，重试恢复原结果。读回须验完整性和独立参考，不静默修复。列表返回轻量 summary（不嵌全 panel）；offset/limit 有界。

工作台独立“作者数据”入口，文件选择→导入并诊断→查看原值与缺失原因→下载报告。保留现有基础设计，使用现有可恢复提交，页面注明公开扰动数据和原 MAT 未在服务器复验。无人工通过状态、无 AI。
