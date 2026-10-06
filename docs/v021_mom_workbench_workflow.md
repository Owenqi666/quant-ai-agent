# 行业 MOM：从固定来源到工作台审核

本轮入口位于既有“研究执行”页面。来源是 49 个预计算行业组合，窗口、单位、权重、开发日期均采用已保存的行业修改合同；原 GJS 个股研究保持独立。AI 与整体 UI 设计仍后置。

## 本机登记一次来源

已有固定例子 `artifacts/mom-only-development-01/`。在项目根目录登记至实际启动工作台所用的 `--home`：

```sh
.venv/bin/python -B scripts/register_industry_mom.py \
  --home var/v09-workbench \
  --artifact artifacts/mom-only-development-01 \
  --idempotency-key industry-mom-source-v1
```

工具核对原始 ZIP、原论文/作者代码、方法、配置、数值参考、报告和全部文件清单，复制到该工作台自己的受控目录，再登记准确 ID 与收据。成功返回 `source_id` 和 `source_digest`；相同键与请求可重放，改变请求须使用新键。登记不执行保存的源码或 MATLAB，也不添加人工标签。源码包不包含本轮第三方原材料，干净环境的列表会留空；操作者需要提供自己的、与固定合同一致的 artifact 再执行本地登记，不能静默换成最新下载或模拟数据。

首次源计算与快照复跑说明见 [独立 CLI](v020_mom_only_workflow.md)。注册后不依赖原 artifact 的路径继续运行；历史 invocation 中的原路径是原调用声明，不是当前文件定位授权。

## 在浏览器操作

1. 打开工作台“研究执行”，在“行业组合 MOM”区域选择登记来源。确认范围是 2010–2011 开发月、行业组合、gross-only，并查看来源及方法摘要。
2. 点击开始计算。服务固定配置后排队；worker 执行与验证。页面显示排队、准备、执行、验证和最终状态，最多 60 秒计算监督预算，最多三次显式尝试。取消和重试保留原尝试；丢响应时使用原请求恢复，避免盲目重复创建。
3. 完成且完整核验后查看真实结果：两种组合的可用月份、月均毛收益、毛收益指数、波动率、回撤和逐月记录。状态轮询只用于刷新提示，不能单独证明结果已验证。
4. 下载准确报告，或创建该结果的研究案例。报告链接携带 `expected_attempt_id` 和 `expected_result_digest`，目标变化会拒绝；案例由服务器冻结证据、方法归因、数据、配置、代码、尝试和结果。
5. 进入已有研究案例/准确结论/五维审核入口。数值结论引用明确 JSON pointer 和工具结果；人工质量判断由使用者填写。计算通过不会自动选“通过”，也不会生成语义参考或人工耗时成绩。

如果来源为空，先执行上面的登记命令。如果数据或清单不匹配，保留失败信息并检查原固定资料；不要重算清单来跳过错误。若任务失败或中断，确认原失败原因后显式重试，页面仍可查看旧尝试。

## 隔离复现和发布验收

完整真实例子使用新目录，自动选择空闲端口，不操作正在运行的工作台：

```sh
.venv/bin/python -B scripts/demo_v021_mom_workbench.py \
  --artifact artifacts/mom-only-development-01 \
  --out artifacts/my-v021-example
```

输出 `result.json`、来源/实验/案例/结论/准确目标、`report.md`、HTTP 请求摘要、工作区及完整备份恢复。这个工程例子的审核来源为 `automation`，五维均为 `not_assessed`；不会伪造人类审核或效率。实际数值须与原独立 CLI 结果完全一致。

全套验收：

```sh
.venv/bin/python -B scripts/verify_release.py \
  --out artifacts/my-v021-release --browser-channel chrome \
  --industry-mom-artifact artifacts/mom-only-development-01
```

参数指定的真实来源检查作为独立阶段记录；其他软件/浏览器基线仍使用各自的受控样例。验收期间不得修改源码，实际通过情况只以新目录的 `acceptance.json` 为准。schema16 备份的升级核验可用 `scripts/check_v021_upgrade.py --backup <备份目录> --out <新目录>`；升级先在副本确认旧业务表、历史文件和审核摘要保存，再覆盖本机服务。远端 CI、模型接口和整体 UI 设计仍未交付。

## 开发启动和接口

安装说明沿用项目 README。构建前端后，可启动一个独立工作区：

```sh
npm --prefix frontend run build
.venv/bin/python -B -m paper_alpha.server.launcher --home var/v09-workbench --port 8765
```

若端口已有正在运行的旧版本，要完成发布验收和工作区备份后升级。不要同时启动两个 worker。`--home` 应与来源登记命令一致；隔离演示使用新的独立目录及空闲端口。

新域使用 `/api/industry-mom-sources` 和 `/api/industry-mom-experiments`。HTTP 创建只接收准确来源 ID/摘要与幂等键，不接受本地路径、下载地址、代码、配置改写或客户收益数字。研究案例使用 `source_kind=industry_mom_experiment`、`result.kind=industry_mom_portfolio`；旧月度夹具仍是 `controlled_fixture`，旧作者绑定仍保留其阻断。

## 结果的解释范围

这是当前来源历史版本上的行业方法演示，2012–2013 保留区间没有解析数值或评价。候选净敞口 0，等权多头基线净敞口 1；收益差不直接证明风险调整后的 Alpha。费用、借券、融资、交易执行和可交易资产篮子仍未建模。来源验证意味着本地字节及独立数值参考一致，不证明提供者身份、点时可得数据或原个股论文复现。人工认可、模型优势和流程效率仍须分别收集证据。

[详细计划与分工](v021_mom_workbench_plan.md) · [固定方法](research/mom_only_method.md) · [原 CLI 交付](v020_mom_only_delivery.md)
