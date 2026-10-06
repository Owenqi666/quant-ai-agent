import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunDetail } from "../src/generated/api-contract";

const root = resolve("..");
const dimensions = ["引用与页码准确", "假设忠于所引原文", "经济解释归因恰当", "字段含义与替代说明", "实现与声明公式一致"];

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((ok, fail) => { server.once("error", fail); server.listen(0, "127.0.0.1", ok); });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("No loopback TCP port was assigned");
  await new Promise<void>((ok, fail) => server.close((error) => error ? fail(error) : ok()));
  return address.port;
}

// This file uses its own worker/API/workspace. Existing workflow acceptance
// intentionally counts all records in its own server, so never seed this case there.
const test = base.extend<{}, { qualityServer: string }>({
  qualityServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-quality-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Quality acceptance must never use the live workspace port");
    const url = `http://127.0.0.1:${port}`;
    const child = spawn(resolve(root, ".venv/bin/python"), ["-m", "paper_alpha.server.launcher", "--home", home, "--port", String(port)], {
      cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
      stdio: ["ignore", "pipe", "pipe"], detached: true,
    });
    let output = `Owned test workspace: ${home}\nOwned port: ${port}\n`;
    const capture = (data: Buffer) => { output = (output + data.toString()).slice(-131072); };
    child.stdout.on("data", capture); child.stderr.on("data", capture);
    const exited = new Promise<void>((ok) => child.once("exit", () => ok()));
    const startupError = new Promise<never>((_, fail) => child.once("error", fail));
    try {
      await Promise.race([startupError, (async () => {
        const deadline = Date.now() + 25000;
        while (Date.now() < deadline) {
          if (child.exitCode !== null) throw new Error(`Owned quality server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned quality server did not become healthy:\n${output}`);
      })()]);
      await use(url);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      const stopped = await Promise.race([exited.then(() => true), delay(12000, undefined, { ref: false }).then(() => false)]);
      if (!stopped && child.pid) {
        try { process.kill(-child.pid, "SIGKILL"); } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
        }
        await exited;
      }
      mkdirSync(workerInfo.project.outputDir, { recursive: true });
      writeFileSync(join(workerInfo.project.outputDir, `quality-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned quality launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ qualityServer }, use) => use(qualityServer),
});

async function openOptionalTimer(page: Page) {
  const summary = page.getByText("可选：本次审核的主动工作时间", { exact: true });
  if (!(await summary.locator("..").getAttribute("open"))) {
    // An open details attribute is an empty string; inspect its presence.
    if (!(await summary.locator("..").evaluate((element) => element.hasAttribute("open")))) await summary.click();
  }
}

async function completedExample(request: APIRequestContext): Promise<RunDetail> {
  const exampleResponse = await request.post("/api/examples/alpha101", { data: {} });
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const submitted = await request.post("/api/runs", { data: {
    revision_id: example.revision_id, mode: "normalized_fixed", idempotency_key: "v06-quality-run",
  } });
  expect(submitted.status()).toBe(201);
  const run = await submitted.json();
  await expect.poll(async () => {
    const current = await (await request.get(`/api/runs/${run.id}`)).json();
    return current.status;
  }, { timeout: 30000 }).toBe("completed");
  const result = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  expect(result.verification?.verified).toBe(true);
  return result;
}

async function fillReasons(page: Page, marker: string) {
  await page.getByLabel("审核人标识", { exact: true }).fill("Browser automation fixture");
  for (const label of dimensions) {
    await expect(page.getByLabel(`${label}判断`, { exact: true })).toHaveValue("not_assessed");
    await page.getByLabel(`${label}理由`, { exact: true }).fill(`${marker}: 未由人工评定；此记录只用于自动化流程验收。`);
  }
}

test("structured review preserves unknown judgments, declared automation, exact target and explicit active intervals", async ({ page, request }, testInfo) => {
  const run = await completedExample(request);
  const target = run.review_targets.find((item) => item.candidate_id === "alpha006")!;
  await page.goto("/");
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("添加分项研究审核", { exact: true }).check();
  await fillReasons(page, "draft-to-discard");
  await openOptionalTimer(page);
  await page.getByRole("button", { name: "开始主动工作计时", exact: true }).click();
  await expect(page.getByRole("button", { name: "保存审核", exact: true })).toBeDisabled();
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha101");
  await expect(page.getByLabel("审核人标识", { exact: true })).toHaveValue("");
  await openOptionalTimer(page);
  await expect(page.getByText(/已记录 0 个区间/)).toBeVisible();
  await expect(page.getByRole("button", { name: "暂停主动工作计时", exact: true })).toBeDisabled();
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByRole("button", { name: "丢弃审核草稿", exact: true }).click();
  await fillReasons(page, "timed-record");
  await page.getByLabel("引用与页码准确判断", { exact: true }).selectOption("passed");
  const note = "v0.6 自动验收：计时与绑定记录，不构成人工研究结论。";
  await page.getByLabel("审核依据", { exact: true }).fill(note);
  await openOptionalTimer(page);
  await page.getByRole("button", { name: "开始主动工作计时", exact: true }).click();
  await expect(page.getByRole("button", { name: "保存审核", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "暂停主动工作计时", exact: true }).click();
  await openOptionalTimer(page);
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  const first = page.locator(".review-history article").filter({ has: page.getByText(note, { exact: true }) });
  await expect(first.getByText("非人工审核记录", { exact: true })).toBeVisible();
  await expect(first.getByRole("cell", { name: "未评定", exact: true })).toHaveCount(4);
  await expect(first.getByText(/主动工作时间：.+秒（显式记录）/)).toBeVisible();
  await first.getByText("查看审核目标与分项记录", { exact: true }).click();
  await expect(first.locator("pre")).toContainText(target.attempt_id);
  await expect(first.locator("pre")).toContainText(target.result_digest);
  let saved = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  expect(saved.reviews).toHaveLength(1);
  const review = saved.reviews[0];
  expect(review.source).toBe("automation");
  expect(review.assessment_summary.semantic_status).toBe("not_human");
  expect(review.assessment_summary.timing_recorded).toBe(true);
  expect(review.assessment?.expected_attempt_id).toBe(target.attempt_id);
  expect(review.assessment?.expected_result_digest).toBe(target.result_digest);
  expect(review.assessment?.dimensions.hypothesis_fidelity.outcome).toBe("not_assessed");
  expect(review.assessment?.active_intervals).toHaveLength(1);
  const interval = review.assessment!.active_intervals[0];
  expect(review.assessment_summary.total_active_seconds).toBe((Date.parse(interval.ended_at) - Date.parse(interval.started_at)) / 1000);
  expect(review.assessment_summary.total_active_seconds).toBeGreaterThan(0);

  await fillReasons(page, "untimed-record");
  const untimedNote = "v0.6 自动验收：未记录工作区间，不能作为零耗时基线。";
  await page.getByLabel("审核依据", { exact: true }).fill(untimedNote);
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  const untimed = page.locator(".review-history article").filter({ has: page.getByText(untimedNote, { exact: true }) });
  await expect(untimed.getByText(/主动工作时间：未记录/)).toBeVisible();
  await expect(page.getByText("分项审核通过（人工声明）", { exact: true })).toHaveCount(0);
  saved = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  expect(saved.reviews).toHaveLength(2);
  expect(saved.reviews.every((item) => item.source === "automation")).toBe(true);
  expect(saved.reviews[1].assessment_summary.timing_recorded).toBe(false);
  expect(saved.reviews[1].assessment_summary.total_active_seconds).toBe(0);
  expect(saved.reviews[1].assessment?.active_intervals).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("structured-review.png"), fullPage: true });
});

test("uploaded duplicate CSV reports logical row 3 and its original row 2 without rewriting input", async ({ page, request }, testInfo) => {
  const example = await request.post("/api/examples/alpha101", { data: {} });
  expect(example.ok()).toBe(true);
  const lines = readFileSync(resolve(root, "tests/fixtures/datasets/market.csv"), "utf8").trimEnd().split("\n");
  lines[2] = lines[1];
  const csv = Buffer.from(lines.join("\n") + "\n");
  const metadata = JSON.parse(readFileSync(resolve(root, "tests/fixtures/datasets/metadata.json"), "utf8"));
  metadata.market_sha256 = createHash("sha256").update(csv).digest("hex");
  metadata.version = "v06-browser-duplicate-fixture";
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "alpha006", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "复制当前任务新建研究", exact: true }).click();
  await page.getByText("上传、验证并登记新的合成数据版本", { exact: true }).click();
  await page.getByLabel("数据集标题", { exact: true }).fill("v0.6 duplicate input diagnostic fixture");
  await page.getByLabel("行情 CSV 文件", { exact: true }).setInputFiles({ name: "duplicate.csv", mimeType: "text/csv", buffer: csv });
  await page.getByLabel("数据元信息 JSON", { exact: true }).setInputFiles({ name: "metadata.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(metadata)) });
  await page.getByRole("button", { name: "上传并保存导入收据", exact: true }).click();
  await expect(page.getByLabel("导入收据 ID", { exact: true })).not.toHaveValue("");
  const receiptId = await page.getByLabel("导入收据 ID", { exact: true }).inputValue();
  await page.getByRole("button", { name: "运行数据验证", exact: true }).click();
  await expect(page.getByRole("heading", { name: "数据质量报告 · invalid", exact: true })).toBeVisible();
  const diagnostics = page.getByRole("region", { name: "输入错误定位", exact: true });
  await expect(diagnostics.getByRole("cell", { name: "3 / 2", exact: true })).toBeVisible();
  await expect(diagnostics.getByRole("cell", { name: "date,asset", exact: true })).toBeVisible();
  await expect(diagnostics.getByRole("cell", { name: "2024-01-02 / ALT001", exact: true })).toBeVisible();
  await expect(diagnostics.getByRole("cell", { name: "duplicate_key", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认登记此数据版本", exact: true })).toBeDisabled();
  const receipt = await (await request.get(`/api/dataset-imports/${receiptId}`)).json();
  const report = receipt.latest_validation.report;
  expect(receipt.status).toBe("invalid");
  expect(receipt.registered_dataset_id).toBeNull();
  expect(report.schema_version).toBe(2);
  expect(report.checks.find((item: { name: string }) => item.name === "input_integrity").outcome).toBe("passed");
  expect(report.checks.find((item: { outcome: string }) => item.outcome === "failed").code).toBe("duplicate_key");
  expect(report.diagnostics.first_error_location).toMatchObject({ row: 3, column: "date,asset", date: "2024-01-02", asset: "ALT001", related_rows: [2] });
  expect(report.diagnostics.all_errors_enumerated).toBe(false);
  expect(report.diagnostics.samples).toHaveLength(1);
  expect(report.input_digest).toBe(receipt.input_digest);
  await page.screenshot({ path: testInfo.outputPath("duplicate-input-diagnostic.png"), fullPage: true });
});

test("same-result refresh keeps a running review draft and successful save clears it despite a catalog refresh error", async ({ page, request }) => {
  const run = await completedExample(request);
  const seedReview = await request.post(`/api/runs/${run.id}/reviews`, { data: {
    candidate_id: "alpha006", verdict: "accepted", category: "implementation", source: "automation",
    note: "Automatic setup for refresh-race acceptance; no human judgment.",
  } });
  expect(seedReview.status()).toBe(201);
  const caseResponse = await request.post("/api/regression-cases", { data: {
    review_id: (await seedReview.json()).id, expected_status: "evaluated", note: "Freeze fixture reference for refresh regression only.",
  } });
  expect(caseResponse.status()).toBe(201);
  const before = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  await page.goto("/");
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("添加分项研究审核", { exact: true }).check();
  await fillReasons(page, "refresh-retained-draft");
  const note = "v0.6 自动竞态验收：已提交记录不可在列表刷新失败时重复作为草稿。";
  await page.getByLabel("审核依据", { exact: true }).fill(note);
  await openOptionalTimer(page);
  await page.getByRole("button", { name: "开始主动工作计时", exact: true }).click();
  await page.getByRole("checkbox", { name: "选择回归用例 alpha006", exact: true }).check();
  await page.getByRole("button", { name: "执行回归检查", exact: true }).click();
  await expect(page.getByText("声明范围内通过", { exact: true })).toBeVisible();
  await expect(page.getByLabel("审核人标识", { exact: true })).toHaveValue("Browser automation fixture");
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue(note);
  await expect(page.getByLabel("假设忠于所引原文理由", { exact: true })).toHaveValue("refresh-retained-draft: 未由人工评定；此记录只用于自动化流程验收。");
  await expect(page.getByRole("button", { name: "暂停主动工作计时", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "保存审核", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "暂停主动工作计时", exact: true }).click();
  await openOptionalTimer(page);
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();

  let injected = false;
  const pattern = "**/api/catalog/runs?*";
  await page.route(pattern, async (route) => {
    if (!injected) {
      injected = true;
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Injected catalog refresh failure after a real saved review" }) });
    } else await route.continue();
  });
  try {
    await page.getByRole("button", { name: "保存审核", exact: true }).click();
    await expect(page.getByText(/审核已保存，列表刷新暂未成功/)).toBeVisible();
    expect(injected).toBe(true);
    await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");
    await expect(page.getByLabel("审核人标识", { exact: true })).toHaveValue("");
    await openOptionalTimer(page);
    await expect(page.getByText(/已记录 0 个区间/)).toBeVisible();
    await expect(page.getByRole("button", { name: "保存审核", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "暂停主动工作计时", exact: true })).toBeDisabled();
    const after = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
    expect(after.reviews).toHaveLength(before.reviews.length + 1);
    const added = after.reviews.filter((item) => item.note === note);
    expect(added).toHaveLength(1);
    expect(added[0].source).toBe("automation");
    expect(added[0].assessment_summary.semantic_status).toBe("not_human");
    expect(added[0].assessment?.active_intervals).toHaveLength(1);
  } finally { await page.unroute(pattern); }
});
