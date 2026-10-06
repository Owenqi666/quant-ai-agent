# v0.15 作者公开月度数据的导入与诊断

这条路径承接已选定的 Goyal / Jegadeesh / Subrahmanyam momentum 论文和 Dataverse V2。完整来源审计见 [数据审计](research/momentum_author_data_audit.md)，字段和数值语义见 [契约](v015_author_panel_contract.md)。AI 接口仍留白，已有 Alpha101、研究规则和受控夹具月度实验继续独立运行。

## 已有本地文件

本次已下载两个 MAT 并核对完整官方 MD5 / SHA256；文件保存在本地忽略目录，不进入源码包：

- `artifacts/research-momentum-data-01/IntnlData.mat`
- `artifacts/research-momentum-data-01/USData.mat`
- 同目录 `dataverse-v2.json`、`download-manifest.json` 和下载/续传日志保存来源证据。
- 全源可读审计：`artifacts/author-source-v015-intnl/audit.json`、`artifacts/author-source-v015-us/audit.json`。

换机器时，通过 [官方固定 V2 页面](https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/R1UI1J&version=2.0) 下载以上两个文件，保持文件名。工具会核对固定摘要，文件版本改变时明确拒绝，不能直接更新 hash 绕过版本审核。首次安装按 README 的 `requirements-web-lock.txt`，包含固定 h5py 版本。只安装基础因子依赖不足以运行本路径。

## 操作

以下命令在项目根目录执行，输出目录须尚不存在。连续原行是调试/审阅切片，不按后续收益或数据完整程度挑选样本。

```sh
.venv/bin/python -m paper_alpha.author_workflow audit \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --out artifacts/my-intnl-audit

.venv/bin/python -m paper_alpha.author_workflow prepare \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --target-month 1993-03 --row-offset 0 --row-count 128 \
  --out artifacts/my-intnl-panel

.venv/bin/python -m paper_alpha.author_workflow verify \
  artifacts/my-intnl-panel \
  --source artifacts/research-momentum-data-01/IntnlData.mat
```

将文件名换成 `USData.mat` 可运行美国源。`row_offset` 从 0 开始，输出的 `source_row` 从 1 开始；每次最多 512 行、一个目标月。需要不同月份或行段时新建输出目录，不覆盖旧产物。

准备产物包含 input/result/reference/report/source_check/environment/manifest 及计算源码和依赖声明副本。`input.json` 是可导入的面板。`source_check.json` 记录创建时是否曾对原 MAT 再提取比较；每次带 `--source` 验证都会再次验证完整 MAT 摘要和原行值。无 `--source` 的 verify 只验证归一面板、参考、报告与快照完整性，返回 `raw_source_reverified=false`。这些哈希用于一致性检查，不是第三方签名认证。

打开工作台，进入 **作者数据**：

1. 选择上述 `input.json`，写标题和备注，点击导入并诊断。不要上传数百 MB 的 MAT。
2. 查看 MOM 窗口、跳过月、DGW/MV 所属月及标签月。展开原行，检查源值、缺失状态与原观测日期。
3. 分别看「形成可用」和「标签可用」；前者不受 H 月标签是否缺失影响。零可形成行也是有效诊断。
4. 下载 Markdown 报告。刷新或响应丢失后，使用保留的原请求恢复，避免重复新建。完整核验失败会隐藏旧结果。

服务器只接收小型归一面板，不读取本地 MAT，不会仅凭面板中声称的源 hash 认证整个原文件；页面和 API 均明确这一范围。记录不可变、精确幂等、读回独立复核，没有自动人工审批状态。

## 研究结论与下一门槛

MOM 使用 H−12..H−2 的 11 个月复合收益；DGW 和 MV 取 H−1，标签取 H。缺失不填零；有限原值不裁剪；不能将 DGW 误写为我们已用原始日数据重新计算的 ID。源文件不包含可直接认定的 ticker / PERMNO 映射。

论文明确公共包被扰动并随机删减，无法恢复为未扰动商业数据。全源固定检查点中，形成可用数仅 0–15 行（具体文件/月见审计），达不到作者 Table8 初始450行的要求。默认1993-03原行1–128的两个切片均没有形成可用行，这是记录数据限制的正确结果，不是有利润的 alpha。不能把窗口改短或填补后再声称复制原论文。

继续研究有两个独立前提：冻结 Table8 的国家中性化、分组、筛选与权重语义；取得足够完整且有使用权限的数据。当前公共源可以验证数据链路及对齐，尚不支持该论文组合收益复现。没有最终保留测试或真实市场表现结论。

## 软件验收

无网络、无 MAT 的工程基线：

```sh
.venv/bin/python scripts/demo_v015.py --out artifacts/my-author-api-demo
```

默认输入明确为合成合同夹具，只验证计算/未来标签隔离、真实 HTTP 导入、同键恢复、读回、报告、离线备份和恢复；不属于真实作者数据结果。使用已准备的实际面板时可加 `--input artifacts/my-intnl-panel/input.json`，服务仍只核验归一面板。

固定数值测试覆盖完整/缺失/非法窗口、非有限状态、原MV零值、大收益、零因子、溢出、日期、未来标签扰动、独立Fraction参考、报告与重哈希篡改；MAT测试使用显式tinyHDF工程夹具。浏览器用例覆盖恢复、防双击、本地持久化失败、错误文件、完整性失败和逐行检查。

整体验收：`scripts/verify_release.py --out <新目录> --browser-channel chrome`。验收结果保存为目录内 acceptance.json；源码独立安装阶段也执行新API示例及数值/MAT测试。原始MAT不进入CI或源码包，实际数据核验作为单独产物保留。

schema11 只新增 author_panels / author_panel_receipts，历史任务与研究数据保持原样。升级运行版本前停止旧服务、验证离线备份、先在恢复副本上迁移验证；验收成功才替换正在运行的源码包。旧包和备份均保留。
