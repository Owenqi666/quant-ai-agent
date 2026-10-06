# 作者数据研究准入与审核

本流程回答：**在已经声明的开发月份内，这份数据是否有足够原行支持我的研究任务？** 它不计算组合收益，也不把数量门槛当成论文方法验收。

## 日常操作

1. 在工作台选择「研究准入」。选择已导入研究，查看论文依据、任务所需字段、开发/保留区间、四种逐月可用资产数和筛查结论。原作者面板可作为关联的逐行证据。
2. 在输出可见后记录审核：数据不足、规则待确认、实现错误或接受当前限制；写清理由。页面默认声明为human，也可显式选择automation；系统保存所选来源，不会自动生成人工通过。
3. 确需改变任务或规则时，先另存计划并重新运行CLI；在旧审核中点击“以此审核创建修订”，再选择新input.json。选择父审核会清空此前选中的文件，避免错配。新旧计划差异和父审核被保存；新结果需要重新审核。

导入输入是CLI生成的 `input.json`，不是MAT、整份manifest或旧单月面板。服务器验证聚合结构和决策一致性；要重新核验真实来源，使用下方带 `--source` 的命令。浏览器提供原请求恢复，响应丢失时重放同一个请求键，不重新构造另一请求。

## 四个预先声明的研究计划

`examples/author_studies/` 包含 IntnlData.mat、USData.mat 各两份计划：

- `*-mom.json`：只要求MOM，最低30行，是项目筛查约定。
- `*-mom-dgw-mv.json`：要求MOM、DGW和MV，检查Table8初始450行数量必要条件。未过滤的全原行计数只是作者样本数上界，不是完整Table8资格。

四份计划均使用1993-03至2006-12全部166个月，2007-01为本轮声明的保留起点。此前已做整体数据质量检查和部分月份诊断，不能声称后段从未被观察。系统保留未达标月份，不选择性删除。

本轮实际扫描四份计划均为数据不足，逐项范围与边界见 [实际源准入结果](research/momentum_eligibility_results.md)。这不等于工具错误或对因子收益的否定。

示例（已有固定来源下载，不需重新下载）：

```bash
.venv/bin/python -m paper_alpha.eligibility_workflow run \
  --plan examples/author_studies/intnl-mom.json \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --out artifacts/my-intnl-mom-study

.venv/bin/python -m paper_alpha.eligibility_workflow verify \
  artifacts/my-intnl-mom-study \
  --source artifacts/research-momentum-data-01/IntnlData.mat
```

输出目录必须不存在。计划先落盘，再扫描原文件。失败保存计划和错误；不会覆盖旧实验。成功产物包含计划、逐月scan、计算结果、来源声明、代码/依赖/环境快照、报告和manifest。带原文件的verify重新校验固定摘要并全行复算；不带原文件只检验保存产物的一致性。

## 如何解释结果

MOM使用H−12..H−2的11个自然月，跳过H−1。DGW和MV取H−1。普通MOM无需DGW/MV；DGW条件对照才要求DGW；市值加权或筛选任务才要求MV。非有限/缺失历史优先计为missing，其次非法收益，再次复合收益数值不可表示。H月收益标签不进入本轮输入或资格判断。

`screen_passed`只表示所有声明月份达到该计划的数量门槛；`screen_blocked`表示至少一个月未达到。两者都属于“筛查成功完成”。`execution_ready`恒为false：公开数据被扰动且有随机删除，原文过滤、组合分组、标签、收益和权重尚未由作者源路径运行。

审核与修订是追加记录，不更新旧结果。`accepted_with_limits`只是接受筛查及其限制，不是认可Alpha收益或论文完整复现。人工/automation是本地声明身份，不是多用户认证。

## 可重复软件验收与基线

```bash
.venv/bin/python scripts/demo_v016.py --out artifacts/my-study-flow
.venv/bin/python scripts/demo_v016.py \
  --input artifacts/my-intnl-mom-study/input.json \
  --out artifacts/my-source-study-flow
```

脚本在独立工作区比较固定纯函数流程与HTTP工作台的同输入、同结果、同报告，再验证自动化审核、修订不继承审核、错误输入拒绝及备份恢复重放。默认聚合输入是构造的契约夹具，不能当作真实源统计。两条流程共享决策引擎，所以这是集成一致性基线；扫描数值正确性另由tinyHDF独立标量/Fraction测试及真实源复算检验。没有测量人工耗时或投资收益。

技术接口与边界见 [冻结契约](v016_author_study_contract.md)，实施及分工见 [任务文档](v0.16任务清单与Agent分工.md)。
