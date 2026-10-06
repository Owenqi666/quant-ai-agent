# v0.22：CI 准备与交付边界

本轮交付整理在进行中，正式运行基线为已验收的 v0.21 / schema17。新 v0.22 是否可替换它，以主 Agent 冻结提交后的完整验收、独立安装及升级记录为准。准备检查不能代替这些验收，也不能代替 GitHub 上实际 Linux 运行。

## 三种状态分开记录

| 状态 | 准确含义 |
|---|---|
| 本地准备检查 passed | 当前已审查 workflow、锁文件、版本和所需源文件可用，已检查的文件边界没有发现违规；没有安装或运行服务 |
| target_access_ready | 显式提供的最近元数据观察记录曾显示目标仓库 push 权限；离线工具没有重新联网或认证这份记录 |
| remote_ci=not_run | 本工具从不启动 GitHub workflow。真正运行后仍须核对准确仓库、提交、workflow、运行结论和下载的验收结果 |

2026-10-06 的只读 GitHub API 观察：目标为 [Owenqi666/quant-ai-agent](https://github.com/Owenqi666/quant-ai-agent)，public，默认 main；已有 Initial commit 与单个 README，main 为 `19f1d0b65b3741609835b850daf3579f328c34ef`。当前访问方式显示 push 权限，Actions 记录为 0。这个历史观察不是之后的发布或 CI 成功证明。本地原工程没有配置 remote。

## 可复用离线预检

在项目根目录、使用一个新输出目录运行：

```bash
python3 scripts/check_ci_readiness.py --out artifacts/my-ci-readiness
```

读取本地 Git HEAD、工作树是否干净、remote 数量和 tracked 文件名；不显示 remote URL、凭据、token 或环境变量，不执行 Git hooks，不联网、push、修改 remote 或构建项目。锁版本和前端/后端版本必须一致。源码打包规则从 AST 静态读取，不执行打包脚本。没有 `.git` 的个人安装包明确不检查 tracked 边界；公开 checkout 则必须有可验证的 Git 清单。

公开导出 checkout 使用 `--public-checkout`。它额外拒绝 tracked 原 PDF、完整提取文本 `paper.json` 和示例页图，并要求 `.gitignore` 显式忽略下载重建的两个文件。检查在 bootstrap 前进行，因此允许这两份固定材料暂不存在，其他源码必需文件仍须可用。

可选 `--release-manifest /absolute/path/RELEASE_MANIFEST.json` 检查当前打包策略完整清单、逐文件 size/SHA256、清单摘要及版本；不是签名认证或公共许可审批。可选 `--remote-observation /absolute/path/receipt.json` 仅读取目标固定、24 小时内的显式 `gh-api-read-only` 元数据收据，不读取 gh 的登录文件或令牌，也不将自述收据当作远端身份认证。失效/目标错误的收据拒绝。完整发布结论仍由实际验收产物给出。

workflow 摘要匹配是针对本项目已审查的确切字节，不是通用 YAML 语义验证。修改 workflow 时必须同时审查行为并更新预检中固定摘要；不能仅更新摘要来跳过行为审查。

## 原出处论文 bootstrap

公开 checkout 排除论文原文件与完整提取文本。在安装锁定 Python 依赖之后显式运行：

```bash
.venv/bin/python scripts/fetch_demo_paper.py --out artifacts/my-paper-bootstrap
```

工具只请求 [101 Formulaic Alphas v3 原 PDF](https://arxiv.org/pdf/1601.00991v3)，固定保存为 `examples/alpha101/paper.pdf`，预期 SHA256 为 `1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9`。不接受任意 URL，使用 stdlib urllib/ssl 与现有锁定 certifi 的公共 CA 根；未安装 certifi 时用系统根。保持 TLS 和主机名核验、不跟随重定向，8 MiB 上限及有界超时；请求失败、内容改变或摘要不符均停止，不更改预期摘要。

用锁定 pypdf 从物理页重建 `examples/alpha101/paper.json`，包括原 metadata 和逐页文字；预期 JSON SHA256 为 `2e958978f23f7359f8f1e18e90f41db4f836216e69c717c3dcc1d2b4b761d7cd`。依赖差异导致提取字节不同也会停止。正确已有文件只复验；错误已有文件拒绝，不覆盖。新文件以临时写入、fsync、排他原子发布产生；PDF 已保存但提取失败时可在解决环境问题后再次运行，它会复用正确 PDF。每次用新的 receipt 输出目录。

收据区分真实 HTTPS 下载、已有文件复验和测试注入 transport。下载成功只是取得固定原出处字节；**不是作者身份认证、论文语义通过，也不是获得再分发许可**。PDF、提取文本、页图和个人源码包不通过 Actions artifacts 上传。

## CI 的实际链路

现有 GitHub Actions 执行：checkout → Python3.14 / Node24 → 公开 checkout 预检 → 锁定依赖安装 → 固定论文 bootstrap → npm ci / Chromium → 统一 `verify_release.py`。使用 `contents: read`、checkout 不保留凭据、同 ref 并发取消，单 job 60 分钟、完整验收步骤 50 分钟。该链路仍需要包源、浏览器包和原论文源网络；下载失败会保留失败状态，不能声称离线完整 CI 或伪造论文 fixture。权限和超时语法见 [GitHub workflow 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。

`if: always()` 仅上传以下文件类型/位置：`artifacts/ci-readiness/result.json`、`artifacts/ci-paper-bootstrap/result.json`、`artifacts/ci-acceptance/acceptance.json`、验收树内 `result.json` 与 `.log`。不上传整个目录，不上传源码 ZIP、PDF、页图、市场 CSV、工作台 DB、trace ZIP 或截图。保留期 14 天；准确匹配与多路径规则见 [upload-artifact 文档](https://github.com/actions/upload-artifact#upload-using-multiple-paths-and-exclusions)。日志仍应在评审中核对，不以文件扩展名保证任意内容安全。

CI 仅使用源码中的合成和受控 fixture。真实行业归档与作者 MAT 不在 public checkout，也不是 CI 的前置条件；相关固定转换、预算、故障和合同测试使用独立夹具。软件通过不能称为真实 Alpha 效果、人工研究认可或模型质量。

## 公开导出排除表

主 Agent 从独立远端 checkout 保留原 main 历史，建立新的 `codex/` 分支和 PR。禁止将本地完整历史推向公共仓库。源码白名单审查与公开提交由主 Agent 执行，此工具不发布。

| 不进入公开 Git / Actions 附件 | 处理方式 |
|---|---|
| 原 Alpha101 PDF、paper-page 图片、完整 paper.json 提取文本 | 原出处 bootstrap，固定摘要；本机忽略，不提交 |
| 第三方真实行业 ZIP/CSV、作者 MAT、下载 receipts 与归档论文/代码原文件 | 操作者独立保存和登记，CI 使用夹具 |
| var、artifacts、runs、backups、用户上传、SQLite/WAL、真实人工判断与观测 | 留在原本地工作区；不进入源码导出 |
| 个人源码包 ZIP/TAR、验收安装目录、node_modules、.venv、缓存 | 由新环境生成；不作为公开附件 |
| .env、token/密钥、个人配置目录 | 不读取或输出内容；提交前另审白名单及历史 |

短引用和公式证据保留在受限固定开发材料中，不把“存在于源码”理解为所有版权问题已解决。个人本地源码包原范围继续是 `personal-local-source-bundle`，其中第三方材料的 redistribution_permission=not_verified；不能把它作为公开下载包。

## 研究范围与后续

当前分别支持日度合成因子、受控月度 fixture、49 行业真实组合 MOM-only 修改案例、作者公开扰动数据准入诊断。行业结果为 current-vintage 回顾性毛收益演示，不是原个股论文复现、可交易策略或 point-in-time 历史；2012–2013 保留区间没有评价。作者路径的不足条件继续停止。

AI/provider/RAG 与 BRAIN 未接入。人工五维审核和流程耗时仍由本人实际填写；自动准备材料或结构检查不能补填。390px 长 UUID 目录溢出已有诊断，全面 UI 和该基础响应性修复继续按后置安排，不能把桌面通过写成窄屏通过。
