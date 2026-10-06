# v0.11 审核结果图表

## 范围

在“审核结果”的本次候选输出中增加三组基础图。保持原有导航与审核表单，不进行整体 UI 重设计，也不自动提交或判断人工审核。没有新增图表依赖。

- 毛收益：每日毛收益、有效日毛收益的算术累计，横轴为退出日。两者分别绘制，以避免累计量级遮盖日收益。
- Rank IC：按信号日显示其与后续入场至退出期间收益的排名相关性，固定纵轴 -1 至 1。
- 覆盖：按信号日显示有限因子资产占冻结股票池的比例，固定纵轴 0% 至 100%；状态条标出已评估、跳过和边界剔除。

图表随审核中选定的已核验候选读取轻量 API；普通 run 响应继续不携带逐日大表。窄审核列使用单列图，宽容器才并排，避免刻度被挤小。精确逐日表默认折叠，每页 15 条，支持键盘翻页；图表来源和限制另行折叠。

## 正确性边界

1. 请求明确绑定 run、candidate、attempt、result_digest；响应还核对冻结 revision。切换目标时旧图立即隐藏，终止旧请求，并用 effect 生命周期防止迟到响应写回。
2. 图点直接来自 `CandidateSeriesResponse` 的已核验计算投影。前端不重算累计，不复利，不补齐缺失收益。真实零值保留，未定义值断线；单一有效点仍可见。
3. 图使用真实日期距离，而不是把周末和交易日间隔强制等距。毛收益按退出日，IC 和覆盖按信号日；明细同时列出 signal、entry、exit。
4. 跳过日保留实际覆盖与原因。purged 日覆盖与有效资产显示“不适用”，不能把原产物初始化的 0 当真实零覆盖。
5. 全无效结果显示没有可绘制值及原因；常量因子不会被画成零收益。常量后续收益的 IC 未定义与真实零收益分开处理。
6. 网络、契约或完整性失败显示明确错误与重试按钮，不回退到其他候选、最近实验或此前图表。
7. 明确标注合成数据、未扣成本、算术累计不是复利净值或 BRAIN PnL。没有新增 Sharpe、回撤、换手、自动优劣标签或真实投资表现声明。

## 实现入口

- `frontend/src/candidateSeries.ts`：审核身份、请求路径、只读图点投影、分段与坐标、显示格式。
- `frontend/src/components/CandidateCharts.tsx`：请求生命周期、基础 SVG、状态条、分页可访问明细。
- `frontend/src/components/ReviewEvidence.tsx`：仅在已核验目标具有计算结果时嵌入。
- `frontend/src/components/candidate-charts.css`：局部样式与容器响应布局。

## 验证

纯函数/静态渲染测试覆盖真实零与缺值、跳过断线、算术累计不重算、退出日与信号日、日期间距、全无效/单点、五项目标身份及精确明细。

`frontend/e2e/candidate-charts.spec.ts` 使用独立临时工作区和 worker/API，不接触正在使用的 8765 工作区：

1. 实际 Alpha101 计算产物、时序 API、SVG 图点和逐日表逐项一致；purged 覆盖不适用。
2. 候选切换期间延迟旧响应不会覆盖新候选；暂时失败可重试且不残留旧图。
3. 常量因子完整运行产生 `not_evaluable`，收益与 IC 留空，覆盖及跳过原因仍可检查。
4. 篡改独立测试工作区的结果字节后，真实后端拒绝读取，前端隐藏旧图；恢复原字节后可以重试。

运行：

```sh
cd frontend
npm test -- src/candidateSeries.test.ts src/reviewEvidence.test.ts
npm run build
PLAYWRIGHT_CHANNEL=chrome PLAYWRIGHT_PORT=8811 npm run test:e2e -- e2e/candidate-charts.spec.ts
```

阶段结果交由本轮统一验收记录归档。上述测试属于软件验证，不代表人工效率、真实数据研究效果或 BRAIN 结果等价性。
