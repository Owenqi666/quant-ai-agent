# 本地源码包与独立安装验证

本功能用于将当前工作台搬到个人的另一目录或机器，并保留可检查的安装与运行证据。当前支持 macOS / Linux、Python 3.11+；服务依赖 POSIX 文件锁，尚未验证 Windows。每次验收记录实际 Python 与操作系统版本，不能从一次本机通过推导出所有环境都已验证。

AI 接口保持关闭。发布包内的数据为合成样例，研究结果仅用于验证软件流程。

## 包含什么

`build_source_release.py` 使用明确的文件白名单与有限目录扩展名规则：Python 运行时、前端源代码和已构建资源、依赖锁、测试与开发脚本、已列明工程说明、现有示例、v0.4/v0.5 评测定义及第二组合成数据。每个文件的字节数与 SHA-256 均写入 `RELEASE_MANIFEST.json`，清单另有整体摘要。

同样的输入会产生字节完全相同的 `.tar.gz`：文件顺序、权限、所有者、时间戳和 gzip 头均固定。不包含用户工作区、上传数据、历史实验、Git、虚拟环境、`node_modules`、环境变量文件或任意符号链接。新建的论文/数据不会因为位于项目中就自动进入包。清单哈希用于完整性核对，不是发布者数字签名。

现有 `101 Formulaic Alphas` PDF、提取文本和证据由已保存的 arXiv 来源与原 PDF 摘要关联。上游再分发权限尚未核实，源码包明确标记为 `personal-local-source-bundle`，只用于本轮个人/本地搬迁；本命令不会上传或公开发布。公开分享前需要另行核实第三方材料许可，不能将本地搬迁验收当作再分发许可。

## 构建与检查

先在源码项目中构建前端，然后使用一个不存在的输出目录：

```bash
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python scripts/build_source_release.py --out artifacts/source-bundle-01
.venv/bin/python scripts/check_portable_release.py --out artifacts/portable-check-01
```

检查命令会自行构建源码包，也可用 `--archive /absolute/path/paper-to-alpha-VERSION.tar.gz` 检查已有包。所有输出目录必须是新的，失败的包、安装日志、HTTP 记录与结果同样保留。

执行顺序为：

1. 检查归档成员、路径、类型、清单、大小及每个文件的摘要；拒绝路径穿越、符号/硬链接、重复成员、漏文件和多余文件。
2. 在新的目录解包，新建独立 venv；从包内依赖锁安装，从解包源码执行 editable install。依赖下载需要可用的 Python 包索引；不复制开发 venv。
3. 清除继承的 `PYTHONPATH`、`PYTHONHOME` 等环境污染；核对关键模块全部来自解包目录，并运行 `pip check`。
4. 从无关工作目录启动实际 launcher、API 和独立 worker；调用健康检查，验证服务版本及 AI 关闭状态。
5. HTTP 请求首页及其 JS/CSS，逐字节核对打包资源；导入示例，执行 `normalized_fixed` 实验，检查两个受支持候选、已验证结果与报告。
6. 可选运行真实浏览器烟测；在解包源码执行并复算 v0.5 评测任务，验证其相对资料路径；最后核对源码没有变化，并在 `finally` 中关闭自有服务进程。

浏览器不是运行包的安装依赖；若测试主机已有 Playwright，可另外提供其绝对模块路径：

```bash
.venv/bin/python scripts/check_portable_release.py \
  --out artifacts/portable-browser-01 \
  --browser-module "$PWD/frontend/node_modules/playwright" \
  --browser-channel chrome
```

浏览器从测试主机启动，但被访问的前端资源与 API/worker 均来自解包包。未指定浏览器时 `result.json` 明确写 `browser.status: not_requested`，不算浏览器通过。

`check_clean_install.py` 已委托同一个入口，保留原有命令兼容性；统一验收只需运行一次，不需要同时执行两个脚本。

## 在另一环境手动启动

解包后保留整个源码目录结构。假设该目录是 `/path/to/paper-to-alpha-VERSION`：

```bash
python3 -m venv /path/to/alpha-venv
/path/to/alpha-venv/bin/python -m pip install -r /path/to/paper-to-alpha-VERSION/requirements-web-lock.txt
/path/to/alpha-venv/bin/python -m pip install --no-deps -e /path/to/paper-to-alpha-VERSION
/path/to/alpha-venv/bin/python -m paper_alpha.server.launcher --home /path/to/new-workspace --port 8765
```

这是保留相邻示例和前端资源的源码安装，不声称支持独立 wheel。运行已打包前端不需要 Node.js；仅重新构建前端时需要 Node.js/npm。启动只监听 `127.0.0.1`，不会自动迁移原工作区。如需现有研究数据，使用项目的备份/恢复流程单独迁移。

## 证据与范围

`result.json` 保存包摘要、解包来源、实际安装模块路径、安装版本、命令和日志摘要、健康状态、HTTP 资源、实验 ID、验证结果、浏览器是否执行和清理状态。`http-trace.json`、`run.json`、`report.md`、`server.log` 与可选截图供复查。检查不会接触原运行工作区；任何失败不会被覆盖成成功。

通过表示“这个源码包在记录的独立环境中完成了本地合成示例”。不代表已完成远端 CI、跨平台矩阵、真实市场研究或公开部署。


v0.6 独立安装验收还会从解包目录执行 v06 数值扩展开发集及复算。打包包含新审核协议、诊断说明和明确列入白名单的 v06 用例，不自动收集其他私人研究目录。

v0.7 在解包后的独立环境增加真实 HTTP 提交后丢响应、原请求重放、备份恢复后重放的验收。包内白名单还包括 v07 的待执行人工协议和空白观测模板；它们不属于已完成人工评测。新版本的实际文件数、摘要、模块来源和测试结果以 `portable-source/result.json` 为准。

v0.8 在解包环境运行 `demo_v08.py`：研究/实验/报告提交确认、结果来源对比、后续审核和恢复后双格式快照一致性。源码包包含 v08 操作说明、任务分工、后端和前端契约；仍是同一用户本地迁移验收，不代表公开分发或远端部署。
