# v0.20 操作与验收

AI 尚未接入，完整 UI 设计仍后置。此次增加准确结论审核、冻结材料参考和来源研究准备三处连接；原实验与记录继续使用。当前部署目录和启动命令以本轮 `completion.md` 为准，README 的默认 `var/workbench` 是另一个工作区。

## 审核准确文字

1. 工作台「研究任务」选择已有任务，展开「带实际数值引用的结论草稿」。保存或查看一条已有草稿。
2. 在「精确结论审核」选择具体结论，阅读准确文字、服务实际数值、出处、数据范围及方法范围；必要时展开来源与执行身份。
3. 展开五维判断，明确选择声明来源、审核者、实际时间，逐维填判断和理由；没有默认通过。
4. 预检后保存。丢失响应时使用原请求安全重试；修改已有判断选择准确旧记录，新版本保留旧历史。文字改动创建新草稿，并重新审核。

这是对准确文字版本的本地人工声明；不会把原 automation draft 改成软件证明，不会批准原实验。自动化测试不计为人工审核。多人矛盾显示冲突，需要实际判断者解决。

## 冻结材料参考与核对

1. 「语义评测」按已有流程阅读、标注材料；Agent 不代填实际人工意见。
2. 「冻结本次材料参考版本」选择材料，预览全部活动人工声明和逐维 pending/unknown/conflicting/eligible。没有人工判断也可保存 pending 快照。
3. 冻结并下载参考集与核对记录。后续标注不会改写旧版；需要新参考时再次显式冻结。
4. 本地验证下载 JSON：`.venv/bin/python scripts/verify_semantic_set.py /absolute/path/bundle.json`。默认复验安装包中的固定来源；省略的 PDF 二进制仍须本地存在。`--snapshot-only` 仅核验冻结 JSON 一致性，不能证明省略来源或真实身份。

高级结构化核对入口接受当前 set_id/digest、source、reviewer、declared_at（含时区）、execution_reference 及每个选中 case 的全部五维 `outcome/reason`，不预填 idempotency_key；页面会持久化提交键。完整格式见 `/docs` 的 `SemanticEvaluationComparisonCreate`。核对输出给出匹配数、可比较数、总维度数及排除原因；无人工参考时一致性率为空。执行引用与审核者是声明，尚未认证；一致性不是模型质量成绩。

所有七项仍是固定开发材料，不能作为独立最终测试集或新论文总体质量证明。

## 绑定作者来源研究

「研究准入」查看已有作者 scan study，在「论文协议来源绑定」选择实际论文与冻结协议并填写标题。服务派生准确摘要；用户不手填 hash。预检检查固定论文、作者 V2 来源、MOM 窗口、计划、扫描、结果及面板；不相容即拒绝。保存后展示阻断项和精确版本导出。

HTTP 只核验已导入关联资源，不读取 MAT，也不重新扫描原资产。真实本地准备／源复核复用源专属 CLI：

```sh
.venv/bin/python -m paper_alpha.author_research_workflow prepare \
  --source /absolute/path/IntnlData.mat \
  --study /absolute/path/existing-eligibility-bundle \
  --paper /absolute/path/selected-momentum-paper.pdf \
  --out artifacts/new-source-linked-preparation
.venv/bin/python -m paper_alpha.author_research_workflow verify \
  artifacts/new-source-linked-preparation --source /absolute/path/IntnlData.mat
```

具体 study 参数及 panel 输入见 [来源专属说明](v020_research_binding.md)。当次固定原 MAT 字节核验、面板重抽与旧 scan 产物核验分别记录；未重新全源扫描时不得声称计数已当场复算。准入诊断作业和 Case 的准确链接在本地准备产物中另存，不回写旧 Case 或内置执行许可。

当前来源仍扰动、删减且方法未决，绑定结果保持 blocked/execution_ready=false；不生成组合收益。便携例子使用单独 `controlled_contract_fixture`，与真实作者论文严格区分。真实大 MAT 和未授权重分发论文不进入便携包。

## 一条命令运行工程示例

```sh
.venv/bin/python scripts/demo_v020.py --out artifacts/my-v020-example
```

输出包含真实工具生成的 Alpha101 合成验证结果及精确结论、自动化审核、pending 参考快照／核对和被阻断的来源合同例子。它验证软件链路；不产生实际人工标签、AI 调用、投资收益或论文复现。保留失败尝试，不复用输出目录。

完整验收使用 `scripts/verify_release.py --out 新目录 --browser-channel chrome`，包括兼容测试、源移位安装、浏览器和原基线。历史完整性是相对于保存的本地账本、来源、发布包和备份；一致性重写全部记录及摘要需要外部可信 checkpoint 才能检测，hash 不等于身份认证。
