import { expect, test } from "@playwright/test";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";

test("real evidence → experiment → review → case → revision → regression chain", async ({
  page,
  request,
}, testInfo) => {
  // Other acceptance files may already have completed work in this server.
  // Count exactly the records created by this chain, not the entire workspace.
  const existingRuns = await (await request.get("/api/runs")).json();
  const existingRunIds = new Set(existingRuns.map((row: { id: string }) => row.id));
  const existingChecks = await (await request.get("/api/regression-checks")).json();
  const existingCheckIds = new Set(existingChecks.map((row: { id: string }) => row.id));
  await page.goto("/");
  await expect(page.getByText("服务已连接")).toBeVisible();
  await page
    .getByRole("button", { name: "导入示例研究", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "alpha006", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "第 8 页 ↗" }).first(),
  ).toHaveAttribute("href", /\/api\/papers\/.+\/pdf#page=8/);
  const firstSubmission = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/runs" && response.request().method() === "POST");
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  const firstSubmittedResponse = await firstSubmission;
  expect(firstSubmittedResponse.status()).toBe(201);
  const firstSubmitted = await firstSubmittedResponse.json();
  await expect(page.getByRole("button", { name: "审核此结果" })).toBeVisible();
  await expect(page.getByText(/已同步至事件快照/)).toBeVisible();
  await page.getByRole("button", { name: "从第一条加载历史", exact: true }).click();
  await expect(page.getByText(/历史快照.*显示第 1/)).toBeVisible();
  await page.getByRole("button", { name: "返回实时事件", exact: true }).click();
  await expect(page.getByText("Mean rank IC")).toHaveCount(2);
  await expect(
    page.getByText(
      "Missing data fields: ['vwap']. Substitution requires a new, explicitly modified hypothesis.",
    ),
  ).toBeVisible();
  const firstRuns = (await (await request.get("/api/runs")).json())
    .filter((row: { id: string }) => !existingRunIds.has(row.id));
  expect(firstRuns).toHaveLength(1);
  const firstRun = firstRuns[0];
  expect(firstRun.id).toBe(firstSubmitted.id);
  const result = await (await request.get(`/api/runs/${firstRun.id}`)).json();
  expect(result.verification.verified).toBe(true);
  expect(
    result.state.candidates.filter(
      (c: { status: string }) => c.status === "evaluated",
    ),
  ).toHaveLength(2);

  await page.getByRole("button", { name: "审核此结果" }).click();
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核结论", { exact: true }).selectOption("accepted");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page
    .getByLabel("审核依据", { exact: true })
    .fill(
      "核对第 8 页原式与本次算子规范化；数值由引擎计算，合成样例仅验证流程。",
    );
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.getByLabel("引用人工审核").locator("option")).toHaveCount(
    2,
  );
  await page.getByLabel("引用人工审核").selectOption({ index: 1 });
  await page.getByLabel("预期候选状态").selectOption("evaluated");
  await page
    .getByLabel("批准理由")
    .fill("已提供 open 和 volume；已登记的别名规范化后，应完成合成数据评估。");
  await page
    .getByRole("button", { name: "明确批准为回归用例", exact: true })
    .click();
  await expect(
    page.getByRole("checkbox", { name: "选择回归用例 alpha006" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "研究工作台", exact: true }).click();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await page.getByText("高级：直接编辑任务 JSON", { exact: true }).click();
  const editor = page.getByLabel("任务定义 JSON", { exact: true });
  const task = JSON.parse(await editor.inputValue());
  task.budget.max_tool_calls = 26;
  task.candidates[0].expression = task.candidates[0].expression.replace("correlation(", "ts_corr(");
  await editor.fill(JSON.stringify(task, null, 2));
  await page
    .getByLabel("修改说明", { exact: true })
    .fill("规范化已登记的算子别名并增加调用预算，公式含义保持一致。");
  await page.getByRole("button", { name: "保存新版本", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本 2 已保存");
  const secondSubmission = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/runs" && response.request().method() === "POST");
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  const secondSubmittedResponse = await secondSubmission;
  expect(secondSubmittedResponse.status()).toBe(201);
  const secondSubmitted = await secondSubmittedResponse.json();
  await expect(page.getByRole("button", { name: "审核此结果" })).toBeVisible();
  await page.getByRole("button", { name: "审核此结果" }).click();
  await page.getByRole("checkbox", { name: "选择回归用例 alpha006" }).check();
  await page.getByRole("button", { name: "执行回归检查" }).click();
  await expect(page.getByText("声明范围内通过", { exact: true })).toBeVisible();
  const saved = (await (await request.get("/api/regression-checks")).json())
    .filter((row: { id: string }) => !existingCheckIds.has(row.id));
  expect(saved).toHaveLength(1);
  expect(saved[0].run_id).toBe(secondSubmitted.id);
  expect(saved[0].passed).toBe(true);
  expect(saved[0].outcome).toBe("passed");
  expect(saved[0].results[0].checks.length).toBeGreaterThan(1);
  expect(saved[0].results[0].checks.every((check: { outcome: string }) => check.outcome === "passed")).toBe(true);
  await page.getByText(/查看逐项检查/).click();
  await expect(page.getByText("查看预期与实际值").first()).toBeVisible();
  const allRuns = (await (await request.get("/api/runs")).json())
    .filter((row: { id: string }) => !existingRunIds.has(row.id));
  expect(allRuns).toHaveLength(2);
  expect(allRuns.map((row: { id: string }) => row.id).sort()).toEqual([firstSubmitted.id, secondSubmitted.id].sort());
  const oldResult = await (
    await request.get(`/api/runs/${firstRun.id}`)
  ).json();
  expect(oldResult.revision_id).toBe(firstRun.revision_id);
  expect(oldResult.state.candidates[0].result.metrics).toEqual(
    result.state.candidates[0].result.metrics,
  );

  // Identical candidate IDs do not make a changed scientific formula comparable.
  await page.getByRole("button", { name: "研究工作台", exact: true }).click();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await page.getByText("高级：直接编辑任务 JSON", { exact: true }).click();
  const changedTask = JSON.parse(await editor.inputValue());
  changedTask.candidates[0].expression = "(-1 * ts_corr(open, volume, 5))";
  changedTask.candidates[0].origin = "user_modification";
  changedTask.candidates[0].changes = ["Test fixture: shorten the correlation window from 10 to 5 observations."];
  await editor.fill(JSON.stringify(changedTask, null, 2));
  await page.getByLabel("修改说明", { exact: true }).fill("显式修改相关窗口；旧研究条件不可直接比较。");
  await page.getByRole("button", { name: "保存新版本", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本 3 已保存");
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(page.getByRole("button", { name: "审核此结果" })).toBeVisible();
  await page.getByRole("button", { name: "审核此结果" }).click();
  await page.getByRole("checkbox", { name: "选择回归用例 alpha006" }).check();
  await page.getByRole("button", { name: "执行回归检查" }).click();
  await expect(page.getByText("存在不可比较项", { exact: true })).toBeVisible();
  await expect(page.getByText(/条件差异：/)).toBeVisible();
  const changedChecks = (await (await request.get("/api/regression-checks")).json())
    .filter((row: { id: string }) => !existingCheckIds.has(row.id));
  expect(changedChecks).toHaveLength(2);
  const incomparable = changedChecks.find((check: { outcome: string }) => check.outcome === "not_comparable");
  expect(incomparable.passed).toBe(false);
  expect(incomparable.results[0].compatible).toBe(false);
  expect(incomparable.results[0].differences.length).toBeGreaterThan(0);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "alpha006", exact: true }),
  ).toBeVisible();
  for (const width of [1440, 900, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`research-${width}.png`),
      fullPage: true,
    });
  }
});

test("basic forms create Alpha101, explicitly recover drafts and close a reviewed regression loop", async ({ page, request }, testInfo) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const example = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  await page.goto("/");
  await expect(page.getByText("服务已连接")).toBeVisible();
  await page.getByRole("button", { name: "新建研究", exact: true }).first().click();
  await page.getByLabel("研究标题", { exact: true }).fill("Alpha101 基础表单研究验收");
  await page.getByLabel("研究论文", { exact: true }).selectOption(example.paper_id);
  await page.getByLabel("数据版本", { exact: true }).selectOption(example.dataset_id);
  await page.getByRole("button", { name: "添加论文证据", exact: true }).click();
  await page.getByLabel("证据 1 页码", { exact: true }).selectOption("15");
  await page.getByText("查看此页提取原文并选择引用", { exact: true }).click();
  const quote = "((close - open) / ((high - low) + .001))";
  const original = page.getByRole("textbox", { name: "证据 1 页原文", exact: true });
  await original.evaluate((node, text) => {
    const input = node as HTMLTextAreaElement;
    const start = input.value.indexOf(text);
    if (start < 0) throw new Error("Formula quote must exist in the extracted page");
    input.setSelectionRange(start, start + text.length);
  }, quote);
  await page.getByRole("button", { name: "采用选中的原文", exact: true }).click();
  await expect(page.getByLabel("证据 1 引用原文", { exact: true })).toHaveValue(quote);
  await page.getByRole("button", { name: "添加研究假设", exact: true }).click();
  await page.getByLabel("假设 1 研究主张", { exact: true }).fill("论文第 15 页列出 Alpha#101 公式，先验证本地实现可复现。");
  await page.getByLabel("假设 1 主张归属", { exact: true }).selectOption("paper_original");
  await page.getByRole("checkbox", { name: /假设 1 引用证据 1/ }).check();
  await page.getByLabel("假设 1 经济机制", { exact: true }).fill("日内价格变化相对振幅的解释是我的待验证假设；该公式引用未证明因果机制。");
  await page.getByLabel("假设 1 机制归属", { exact: true }).selectOption("user_modification");
  await page.getByLabel("假设 1 信号方向", { exact: true }).fill("本地约定：高值进入多头，非论文已验证结论。");
  await page.getByLabel("假设 1 所需数据字段", { exact: true }).fill("close\nopen\nhigh\nlow");
  await page.getByLabel("假设 1 适用条件与假设", { exact: true }).fill("合成数据仅用于工程验收。\n日线完整后计算；沿用引擎次日执行与验证区间约定。\n经济解释待人工审核；不等同于 BRAIN 仿真。");
  await page.getByRole("button", { name: "添加候选实现", exact: true }).click();
  await page.getByLabel("候选 1 关联假设", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("候选 1 表达式", { exact: true }).fill(quote);
  await page.getByLabel("候选 1 实现来源", { exact: true }).selectOption("paper_original");
  for (const [name, bounds] of Object.entries(example.revisions[0].task.evaluation.splits) as [string, { start: string; end: string }][]) {
    await page.getByLabel(`${name} 开始日期`, { exact: true }).fill(bounds.start);
    await page.getByLabel(`${name} 结束日期`, { exact: true }).fill(bounds.end);
  }
  await page.getByLabel("最少有效资产数", { exact: true }).fill("5");
  await page.reload();
  await page.getByRole("button", { name: "新建研究", exact: true }).first().click();
  await expect(page.getByText(/草稿尚未应用/)).toBeVisible();
  await expect(page.getByLabel("研究标题", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "恢复此草稿", exact: true }).click();
  await expect(page.getByLabel("研究标题", { exact: true })).toHaveValue("Alpha101 基础表单研究验收");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`manual-form-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1100 });
  await expect(page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分")).not.toBeChecked();
  await page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分").check();
  await page.getByRole("button", { name: "创建研究", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Alpha101 基础表单研究验收", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(page.getByRole("button", { name: "审核此结果", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核此结果", exact: true }).click();
  await page.getByLabel("审核候选", { exact: true }).selectOption("candidate-1");
  await page.getByLabel("审核结论", { exact: true }).selectOption("accepted");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill("自动验收样例：字面引用和引擎结果已核验；经济语义仍待用户审核。");
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await page.getByLabel("引用人工审核").selectOption({ index: 1 });
  await page.getByLabel("预期候选状态").selectOption("evaluated");
  await page.getByLabel("批准理由").fill("自动回归仅冻结此合成研究的数值约定，未作人工投资判断。");
  await page.getByRole("button", { name: "明确批准为回归用例", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "选择回归用例 candidate-1" })).toBeVisible();
  await page.getByLabel("问题处理声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("选择原审核", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("待处理问题依据", { exact: true }).fill("自动验收：增加预算后复测，保持同一论文公式、数据和时间切分。");
  await page.getByRole("button", { name: "从审核创建问题", exact: true }).click();
  await expect(page.getByLabel("选择已记录问题", { exact: true })).not.toHaveValue("");
  const issueId = await page.getByLabel("选择已记录问题", { exact: true }).inputValue();
  expect(issueId).not.toBe("");
  await page.getByRole("button", { name: "研究工作台", exact: true }).click();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await page.getByLabel("最多工具调用次数", { exact: true }).fill("28");
  await page.getByLabel("修改说明", { exact: true }).fill("仅增加运行预算，科学定义保持一致；自动浏览器验收。");
  await expect(page.getByText("任务.budget.max_tool_calls", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await expect(page.getByText(/草稿尚未应用/)).toBeVisible();
  await expect(page.getByLabel("最多工具调用次数", { exact: true })).toHaveValue("24");
  await page.getByRole("button", { name: "恢复此草稿", exact: true }).click();
  await expect(page.getByLabel("最多工具调用次数", { exact: true })).toHaveValue("28");
  await page.getByRole("button", { name: "保存新版本", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本 2 已保存");
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(page.getByRole("button", { name: "审核此结果", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核此结果", exact: true }).click();
  await page.getByRole("checkbox", { name: "选择回归用例 candidate-1" }).check();
  await page.getByRole("button", { name: "执行回归检查", exact: true }).click();
  await expect(page.getByText("声明范围内通过", { exact: true })).toBeVisible();
  await page.getByLabel("选择已记录问题", { exact: true }).selectOption(issueId);
  await page.getByRole("button", { name: "读取最新问题记录", exact: true }).click();
  await page.getByLabel("问题处理声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("追加处理状态", { exact: true }).selectOption("resolved");
  await page.getByLabel("选择关联修订", { exact: true }).selectOption({ label: (await page.getByLabel("选择关联修订", { exact: true }).getByRole("option", { name: /^v2/ }).textContent())! });
  await page.getByLabel("选择目标实验", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("选择目标回归检查", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("本次处理依据", { exact: true }).fill("自动验收：明确关联新修订、已验证实验及通过的原回归案例，经济机制仍待人工核验。");
  await page.getByRole("button", { name: "保存追加处理记录", exact: true }).click();
  await expect(page.getByRole("heading", { name: /问题 .* · 已解决/ })).toBeVisible();
  const issue = await (await request.get(`/api/issues/${issueId}`)).json();
  expect(issue.events.at(-1).source).toBe("automation");
  expect(issue.events.at(-1).check_id).toBeTruthy();
  const summary = await (await request.get(`/api/feedback-summary?research_id=${issue.research_id}`)).json();
  expect(summary.metrics.human_review_coverage.numerator).toBe(0);
  expect(summary.metrics.same_condition_fix_verification.numerator).toBe(1);
  expect(await page.evaluate(() => Object.keys(localStorage).filter((key) => key.includes(":draft:")).length)).toBe(0);
});

test("PDF upload, manual definition and API-disabled scope are available without AI", async ({
  page,
  request,
}) => {
  // Prepare the declared dataset before the first catalog read; this scenario
  // must work alone and must not inherit another test's workspace state.
  const importedResponse = await request.post("/api/examples/alpha101", { data: {} });
  expect(importedResponse.status()).toBe(200);
  const imported = await importedResponse.json();
  const researchResponse = await request.get(`/api/researches/${imported.research_id}`);
  expect(researchResponse.status()).toBe(200);
  const research = await researchResponse.json();
  const task = research.revisions[0].task;
  await page.goto("/");
  await page
    .getByRole("button", { name: "新建研究", exact: true })
    .first()
    .click();
  await page
    .getByLabel("论文文件", { exact: true })
    .setInputFiles(resolve("../examples/alpha101/paper.pdf"));
  await page.getByLabel("论文标题", { exact: true }).fill("手动研究上传测试");
  const uploadedResponsePromise = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/papers" && response.request().method() === "POST");
  await page.getByRole("button", { name: "上传论文", exact: true }).click();
  const uploadedResponse = await uploadedResponsePromise;
  expect(uploadedResponse.status()).toBe(201);
  const uploaded = await uploadedResponse.json();
  expect(uploaded.sha256).toBe("1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9");
  await expect(page.getByRole("button", { name: "上传论文", exact: true })).toBeVisible();
  await expect(page.getByLabel("研究论文", { exact: true })).toHaveValue(uploaded.id);
  // The UI must bind the requested data version, not a default first option.
  await page.getByLabel("数据版本", { exact: true }).selectOption(research.dataset_id);
  await expect(page.getByLabel("数据版本", { exact: true })).toHaveValue(research.dataset_id);
  await page
    .getByLabel("研究标题", { exact: true })
    .fill("手动定义价格成交量研究");
  await page.getByText("高级：直接编辑任务 JSON", { exact: true }).click();
  await page
    .getByLabel("任务定义 JSON", { exact: false })
    .fill(JSON.stringify(task, null, 2));
  await page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分").check();
  const createButton = page.getByRole("button", { name: "创建研究", exact: true });
  await expect(createButton).toBeEnabled();
  const createdResponsePromise = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/researches" && response.request().method() === "POST");
  await createButton.click();
  const createdResponse = await createdResponsePromise;
  expect(createdResponse.status()).toBe(201);
  const created = await createdResponse.json();
  expect(created.dataset_id).toBe(research.dataset_id);
  expect(created.paper_id).toBe(uploaded.id);
  await expect(
    page.getByRole("heading", { name: "手动定义价格成交量研究", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("AI 未启用", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "alpha006", exact: true }),
  ).toBeVisible();
});


test("empty mounted dataset draft remains disabled after delayed real catalog; explicit dataset selection restores PDF workflow", async ({page, request}, testInfo) => {
  const importedResponse = await request.post("/api/examples/alpha101", {data: {}});
  expect(importedResponse.status()).toBe(200);
  const imported = await importedResponse.json();
  const researchResponse = await request.get(`/api/researches/${imported.research_id}`);
  expect(researchResponse.status()).toBe(200);
  const research = await researchResponse.json();
  let mounted = false, arrived!: () => void, release!: () => void;
  const arrival = new Promise<void>(resolve => {arrived = resolve;});
  const gate = new Promise<void>(resolve => {release = resolve;});
  let initialEmptyReads = 0, delayedReads = 0;
  await page.route("**/api/datasets", async route => {
    // App requires a completed initial catalog before mounting CreateResearch.
    // A controlled empty catalog represents that valid empty initial state.
    if (!mounted) {
      initialEmptyReads++;
      await route.fulfill({status: 200, contentType: "application/json", body: "[]"});
      return;
    }
    // A real PDF upload requests a catalog refresh. Delay this true response
    // until the existing form/draft is observed, not until an arbitrary sleep.
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    expect((await response.json()).some((row: {id: string}) => row.id === research.dataset_id)).toBe(true);
    delayedReads++; arrived(); await gate;
    await route.fulfill({response});
  });
  try {
    await page.goto("/");
    await page.getByRole("button", {name: "新建研究", exact: true}).first().click();
    await expect(page.getByRole("heading", {name: "从论文创建研究", exact: true})).toBeVisible();
    const dataset = page.getByLabel("数据版本", {exact: true});
    await expect(dataset).toHaveValue("");
    mounted = true;
    const uploadResponsePromise = page.waitForResponse(response =>
      new URL(response.url()).pathname === "/api/papers" && response.request().method() === "POST");
    await page.getByLabel("论文文件", {exact: true}).setInputFiles(resolve("../examples/alpha101/paper.pdf"));
    await page.getByLabel("论文标题", {exact: true}).fill("受控延迟目录的真实 PDF 上传");
    await page.getByRole("button", {name: "上传论文", exact: true}).click();
    const uploadResponse = await uploadResponsePromise;
    expect(uploadResponse.status()).toBe(201);
    const uploaded = await uploadResponse.json();
    expect(uploaded.sha256).toBe("1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9");
    await arrival;
    await expect(dataset).toHaveValue("");
    release();
    await expect(dataset.locator(`option[value="${research.dataset_id}"]`)).toHaveCount(1);
    await expect(page.getByRole("button", {name: "上传论文", exact: true})).toBeVisible();
    await expect(page.getByLabel("研究论文", {exact: true})).toHaveValue(uploaded.id);
    await expect(dataset).toHaveValue("");
    await page.getByLabel("研究标题", {exact: true}).fill("明确数据版本的受控 PDF 研究");
    await page.getByText("高级：直接编辑任务 JSON", {exact: true}).click();
    await page.getByLabel("任务定义 JSON", {exact: false}).fill(JSON.stringify(research.revisions[0].task, null, 2));
    const confirmation = page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分");
    await confirmation.check();
    const create = page.getByRole("button", {name: "创建研究", exact: true});
    await expect(create).toBeDisabled();
    await page.screenshot({path: testInfo.outputPath("blank-data-disabled.png"), fullPage: true});
    await dataset.selectOption(research.dataset_id);
    await expect(dataset).toHaveValue(research.dataset_id);
    await expect(confirmation).not.toBeChecked();
    await confirmation.check();
    await expect(create).toBeEnabled();
    await page.screenshot({path: testInfo.outputPath("explicit-data-enabled.png"), fullPage: true});
    const createdResponsePromise = page.waitForResponse(response =>
      new URL(response.url()).pathname === "/api/researches" && response.request().method() === "POST");
    await create.click();
    const createdResponse = await createdResponsePromise;
    expect(createdResponse.status()).toBe(201);
    const created = await createdResponse.json();
    expect(created.dataset_id).toBe(research.dataset_id);
    expect(created.paper_id).toBe(uploaded.id);
    await expect(page.getByRole("heading", {name: "明确数据版本的受控 PDF 研究", exact: true})).toBeVisible();
    await expect(page.getByText("AI 未启用", {exact: true})).toBeVisible();
    await expect(page.getByRole("heading", {name: "alpha006", exact: true})).toBeVisible();
    await testInfo.attach("controlled-catalog-evidence", {contentType: "application/json", body: JSON.stringify({
      initialEmptyReads, delayedReads, draft_remained_blank_after_catalog: true, create_disabled_without_dataset: true,
      explicit_dataset_id: research.dataset_id, created_dataset_id: created.dataset_id, uploaded_paper_id: uploaded.id,
      created_paper_id: created.paper_id, upload_sha256: uploaded.sha256, production_auto_selection_added: false,
      scope: "Controlled isolated browser reproduction; not an identification of missing Linux failure trace fields",
    })});
  } finally {
    release();
    await page.unroute("**/api/datasets");
  }
});

test("second synthetic dataset is uploaded, independently validated, explicitly registered and bound to a new research", async ({ page, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "导入示例研究", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "alpha006", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "复制当前任务新建研究", exact: true }).click();
  await page.getByText("上传、验证并登记新的合成数据版本", { exact: true }).click();
  await page.getByLabel("数据集标题", { exact: true }).fill("Browser fixture: second synthetic market");
  await page.getByLabel("行情 CSV 文件", { exact: true }).setInputFiles(resolve("../tests/fixtures/datasets/market.csv"));
  await page.getByLabel("数据元信息 JSON", { exact: true }).setInputFiles(resolve("../tests/fixtures/datasets/metadata.json"));
  await page.getByRole("button", { name: "上传并保存导入收据", exact: true }).click();
  await expect(page.getByLabel("导入收据 ID", { exact: true })).not.toHaveValue("");
  const receiptId = await page.getByLabel("导入收据 ID", { exact: true }).inputValue();
  await expect(page.getByRole("button", { name: "确认登记此数据版本", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "运行数据验证", exact: true }).click();
  await expect(page.getByRole("heading", { name: "数据质量报告 · valid", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认登记此数据版本", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "确认登记此数据版本", exact: true }).click();
  await expect(page.getByRole("button", { name: "在新研究中选择此版本", exact: true })).toBeVisible();
  const receipt = await (await request.get(`/api/dataset-imports/${receiptId}`)).json();
  expect(receipt.status).toBe("registered");
  await expect(page.getByLabel("数据版本", { exact: true })).toHaveValue(receipt.registered_dataset_id);
  await page.getByText("高级：直接编辑任务 JSON", { exact: true }).click();
  const editor = page.getByLabel("任务定义 JSON", { exact: true });
  const task = JSON.parse(await editor.inputValue());
  task.evaluation = JSON.parse(readFileSync(resolve("../tests/fixtures/datasets/research_config.json"), "utf8"));
  await editor.fill(JSON.stringify(task, null, 2));
  await page.getByLabel("研究标题", { exact: true }).fill("Imported dataset browser research");
  await page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分").check();
  await page.getByRole("button", { name: "创建研究", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Imported dataset browser research", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(page.getByRole("button", { name: "审核此结果", exact: true })).toBeVisible();
  const researches = await (await request.get("/api/researches")).json();
  const research = researches.find((item: { title: string }) => item.title === "Imported dataset browser research");
  expect(research.dataset_id).toBe(receipt.registered_dataset_id);
  const runs = await (await request.get("/api/runs")).json();
  const run = runs.find((item: { research_id: string }) => item.research_id === research.id);
  const result = await (await request.get(`/api/runs/${run.id}`)).json();
  expect(result.verification.verified).toBe(true);
  expect(result.state.candidates.filter((candidate: { status: string }) => candidate.status === "evaluated")).toHaveLength(2);
});

test("declared automation issue closes only with an explicit linked decision, and historical catalogs remain accessible beyond the first page", async ({ page, request }) => {
  const cases = await (await request.get("/api/regression-cases")).json();
  const originalCase = cases.find((item: { candidate_id: string }) => item.candidate_id === "alpha006");
  const checks = await (await request.get("/api/regression-checks")).json();
  const proof = checks.find((check: { passed: boolean; results: { case_id: string }[] }) => check.passed && check.results.some((result) => result.case_id === originalCase.id));
  expect(proof).toBeTruthy();
  const research = await (await request.get(`/api/researches/${originalCase.research_id}`)).json();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "alpha006", exact: true })).toBeVisible();
  // Deterministically reproduce navigation while a real import response is in flight.
  // The unchanged server response is held, rather than replacing it with a mock result.
  let releaseImport!: () => void;
  let importArrived!: () => void;
  const release = new Promise<void>((resolve) => { releaseImport = resolve; });
  const arrived = new Promise<void>((resolve) => { importArrived = resolve; });
  await page.route("**/api/examples/alpha101", async (route) => {
    const response = await route.fetch();
    importArrived();
    await release;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "导入示例研究", exact: true }).first().click();
  await arrived;
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("问题处理声明来源", { exact: true }).selectOption("automation");
  releaseImport();
  await expect(page.getByRole("button", { name: "导入示例研究", exact: true })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "审核与回归", exact: true })).toBeVisible();
  await expect(page.getByLabel("问题处理声明来源", { exact: true })).toHaveValue("automation");
  await page.unroute("**/api/examples/alpha101");
  await page.getByLabel("选择原审核", { exact: true }).selectOption(originalCase.review_id);
  await page.getByLabel("待处理问题依据", { exact: true }).fill("Automated browser acceptance: preserve the original review and prove the compatible alias revision.");
  await page.getByRole("button", { name: "从审核创建问题", exact: true }).click();
  await expect(page.getByLabel("已记录问题 ID", { exact: true })).not.toHaveValue("");
  const issueId = await page.getByLabel("已记录问题 ID", { exact: true }).inputValue();
  expect((await (await request.get(`/api/issues/${issueId}`)).json()).state).toBe("open");
  await page.getByLabel("追加处理状态", { exact: true }).selectOption("resolved");
  await page.getByLabel("选择关联修订", { exact: true }).selectOption(proof.revision_id);
  await page.getByLabel("选择目标实验", { exact: true }).selectOption(proof.run_id);
  await page.getByLabel("选择目标回归检查", { exact: true }).selectOption(proof.id);
  await page.getByLabel("本次处理依据", { exact: true }).fill("Automation fixture decision: exact target check passed for the original approved case; this is not a human research judgment.");
  await page.getByRole("button", { name: "保存追加处理记录", exact: true }).click();
  await expect(page.getByRole("heading", { name: /问题 .* · 已解决/ })).toBeVisible();
  const closed = await (await request.get(`/api/issues/${issueId}`)).json();
  expect(closed.events).toHaveLength(2);
  expect(closed.events[1].check_id).toBe(proof.id);
  expect(closed.events[1].source).toBe("automation");
  await page.getByRole("button", { name: "计算本研究汇总", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: "人工声明审核覆盖" })).toContainText("0 /");
  const summary = await (await request.get(`/api/feedback-summary?research_id=${research.id}`)).json();
  expect(summary.metrics.human_review_coverage.numerator).toBe(0);
  expect(summary.metrics.review_rows_by_source.automation).toBeGreaterThan(0);
  expect(summary.metrics.issue_closure.numerator).toBeGreaterThan(0);

  let lastResearch: { id: string; title: string } | undefined;
  for (let index = 0; index < 52; index += 1) {
    const response = await request.post("/api/researches", { data: {
      title: `Pagination fixture ${index}`, paper_id: research.paper_id, dataset_id: research.dataset_id,
      task: research.revisions[0].task,
    } });
    expect(response.status()).toBe(201);
    lastResearch = await response.json();
  }
  await page.getByRole("button", { name: "历史目录", exact: true }).click();
  await page.getByLabel("记录类型", { exact: true }).selectOption("reviews");
  await page.getByLabel("筛选审核来源", { exact: true }).selectOption("automation");
  await page.getByLabel("筛选研究 ID", { exact: true }).fill(research.id);
  await page.getByRole("button", { name: "按条件重新查询", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: originalCase.review_id })).toBeVisible();
  await page.getByLabel("记录类型", { exact: true }).selectOption("researches");
  await page.getByLabel("筛选研究 ID", { exact: true }).fill("");
  await page.getByRole("button", { name: "按条件重新查询", exact: true }).click();
  await expect(page.getByRole("button", { name: "下一页目录", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "下一页目录", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: lastResearch!.title });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "打开此记录", exact: true }).click();
  await expect(page.getByRole("heading", { name: lastResearch!.title, exact: true })).toBeVisible();
});
