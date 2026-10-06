# v0.20：精确研究来源绑定

ResearchBinding 把已经存在的论文、研究协议、作者来源、准入研究和相关 panel 连接成不可变的准备记录。它复用原协议、panel、study 与准入计算，不执行组合收益，也不修改旧记录。

## 基础操作

在已有论文、协议与作者研究任务中选择一项，填写标题。`POST /api/research-bindings/prepare` 由服务端从选定资源派生全部摘要及 panel 引用，返回预览；不需要用户手填 hash，也不保存或升级来源权限。随后创建请求携带准确预览摘要，服务端再次核验并冻结原引用。基础 UI 使用 `author_paper` 范围。

- `prepare(paper_id, protocol_id, study_id, source_scope, title, note='') -> BindingPreview`
- `preview(**BindingPreviewRequest) -> BindingPreview`
- `create(**BindingCreate) -> ResearchBinding`
- `get(id)`、`list(limit=20, offset=0, study_id=None)`、`export(id)`、`markdown(id)`

`author_paper` 必须对应固定 GJS 论文 PDF。每次预览、读取及幂等恢复都校验实际 PDF 字节和重新提取的文本、固定来源注册表，以及协议/study/panel 的原始合同。论文原句匹配只证明出处可检查，不能替代人工语义判断。

冻结内容包括论文摘要与原句定位，协议及原未决事项，固定 MAT 文件版本/摘要，预声明开发与保留边界，plan/input scan/aggregate result 摘要，study 与全部已有 panel 的准确引用，以及窗口和缺失政策的兼容决定。所有原 study panel 必须按原顺序绑定；不能悄悄省略某个 panel。MOM 的固定作者扫描使用 H-12…H-2 十一个月、跳过 H-1；DGW/MV 取 H-1。项目日频 ID 参数保留为独立约定，不视为作者月度 DGW 的等价实现。

记录与请求收据采用内容摘要、元数据摘要、准确请求幂等及同一事务。全部有界记录/收据必须互相覆盖，缺失收据不靠重建放行；插入双方后、提交前重新检查原资源。改变原论文、协议、scan/panel 或账本会安全拒绝读取和恢复。此完整性合同不认证外部人工身份，也不保护被整体替换的本地数据库作为可信第三方历史。

## 两个来源范围

| 范围 | 允许的论文 | 用途与结果 |
|---|---|---|
| `author_paper` | 固定 GJS 论文 PDF | 准确作者来源准备，仍为 blocked |
| `controlled_contract_fixture` | 仓库随附的 Alpha101 PDF | 工程合同演示；明确是与 GJS 无关的文档，不是研究复现 |

便携示例使用第二种范围，并使用合成扫描计数。不能将其改为作者论文范围，也不能据此取得执行权限。一般 UI 不提供夹具范围入口。

HTTP 不接受原始文件路径、URL、可执行对象、`raw_source_reverified` 或 `execution_ready` 布尔。导入的来源 SHA 与 scan 计数属于声明；所有 ResearchBinding 返回 `raw_source_reverified=false`、`execution_ready=false`、`semantic_fidelity=unverified`。即使样本计数通过，作者组合方法未执行这一阻断仍存在。真实收益图、原论文复现、人工审核通过、AI 质量与人工节省时间均不由本功能产生。

## 可运行的便携示例

从仓库根目录运行，目标目录必须不存在：

```sh
.venv/bin/python scripts/demo_v020_bindings.py --out /private/tmp/v020-binding-example
```

保存合成 scan、实际随附 PDF 和实现代码摘要、绑定导出、独立诊断作业、独立 research case、manifest 和 `result.json`。验证的是可运行合同、重放、明确阻断和夹具不可升级。`human_reviews_written=0`，`llm_api_called=false`，没有读取作者 MAT。

## 固定真实作者文件的本地准备

已存在作者 MAT、原论文、v16 扫描和 v15 panel 时可以复用它们：

```sh
.venv/bin/python -m paper_alpha.author_research_workflow prepare \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --study artifacts/author-studies-v016-01/intnl-mom \
  --paper artifacts/research-momentum-01/paper.pdf \
  --panel artifacts/author-panel-v015-intnl-01 \
  --out /private/tmp/v020-author-preparation

.venv/bin/python -m paper_alpha.author_research_workflow verify \
  /private/tmp/v020-author-preparation \
  --source artifacts/research-momentum-data-01/IntnlData.mat
```

准备过程建立独立工作区，核验固定原 MAT 当前字节及字段/轴，核验已有 scan 的 manifest、原输入和重算准入结果，重新抽取指定的有界 panel。它不再次扫描所有开发月份/原始资产。receipt 明确区分：当前原文件核验、历史扫描 artifact 核验、panel 原始重抽，以及 `eligibility_scan_recomputed_now=false`。未传 `verify --source` 时，只报告历史原文件核验声明和当次 artifact 完整性；传参才报告当前 raw archive 核验。

本地 receipt 是离线本地过程声明，不能经 HTTP 赋予原文件权限，不能认证人工、历史计数创建时间或收益执行许可。作者公开文件经扰动和随机删除，不是完整原市场数据。

原诊断作业和 Case 没有 binding 字段，本轮保留旧合同。CLI 在独立工作区创建 `author_study_diagnostic`，执行 `validate` 到 blocked，并创建原 `author_study` Case。它们的准确 ID/digest 写入本地 receipt 的 `external_associations`，另存 `diagnostic-job.json` 和 `research-case.json`。核验同时检查这两个关联。它们是绑定外的精确关联，不声称被 ResearchBinding 主体冻结，也不获得组合执行许可。

本轮实际本地成功记录位于 `artifacts/v020-author-source-01/attempt-02`。首次集成误读旧 `advance` 返回 DTO 的失败及错误记录保留在父目录，没有覆盖失败记录。成功绑定固定 1993-03…2006-12 的 166 月开发 scan、2007-01 起保留边界及 128 原始行 panel；原扫描 0/166 月达到预声明门槛，稳定 DATA_INSUFFICIENT。这些真实来源文件与 PDF 不随便携包重新分发。

## 后续研究边界

接通实际组合之前仍需足够且明确授权的数据、明确的作者 universe/sort/中性化/持有权重与 label 方法、DGW 日频内部窗口/缺失政策决议、独立数值核对和人工语义审核。当前数据不足不通过降低原门槛、填补删减值或自动切换方法解决。新增 ResearchBinding 仅使这些缺口及来源更清楚，不制造收益。
