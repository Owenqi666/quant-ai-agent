import { test as base, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ProtocolRecord, MonthlyDetail } from "../src/generated/api-contract";

const root = resolve("..");

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
const test = base.extend<{}, { monthlyServer: { url:string; home:string } }>({
  monthlyServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-monthly-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Monthly acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned monthly server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned monthly server did not become healthy:\n${output}`);
      })()]);
      await use({url,home});
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
      writeFileSync(join(workerInfo.project.outputDir, `monthly-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned monthly launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ monthlyServer }, use) => use(monthlyServer.url),
});

async function projectRule(request: import("@playwright/test").APIRequestContext, title: string) {
  const presets = await (await request.get("/api/research-protocols/presets")).json();
  const config = presets.presets.find((item: {config: {mode: string}}) => item.config.mode === "project").config;
  const response = await request.post("/api/research-protocols", {data: {title, note: "Browser automation controlled fixture", config}});
  expect(response.status()).toBe(201);
  return await response.json() as ProtocolRecord;
}
async function openMonthly(page: Page) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "月度实验", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度实验提交", exact: true})).toBeVisible();
}
async function configure(page: Page, rule: ProtocolRecord) {
  await page.getByLabel("月度实验规则版本", {exact: true}).selectOption(rule.id);
  await page.getByLabel("月度开始月份", {exact: true}).fill("2025-01");
  await page.getByLabel("月度结束月份", {exact: true}).fill("2025-02");
  await page.getByLabel("已确认规则、月份、受控数据及代理成本限制", {exact: true}).check();
}
async function completed(request: import("@playwright/test").APIRequestContext, id: string) {
  await expect.poll(async () => {
    const response = await request.get(`/api/monthly-experiments/${id}/status`);
    expect(response.ok()).toBe(true);
    return (await response.json()).status;
  }, {timeout: 45000, intervals: [100, 200, 500]}).toBe("completed");
  const response = await request.get(`/api/monthly-experiments/${id}`);
  expect(response.ok()).toBe(true);
  return await response.json() as MonthlyDetail;
}
async function apiExperiment(request: import("@playwright/test").APIRequestContext, rule: ProtocolRecord, key: string) {
  const defaults = await (await request.get("/api/monthly-experiments/defaults")).json();
  const response = await request.post("/api/monthly-experiments", {data: {
    protocol_id: rule.id, protocol_digest: rule.digest, config: {...defaults.config, start_month: "2025-01", end_month: "2025-02"}, idempotency_key: key,
  }});
  expect(response.status()).toBe(201);
  return (await response.json()).experiment_id as string;
}

test("independent monthly entry submits, exposes source output and freezes automation review with fixed downloads", async ({page, request}, testInfo) => {
  await openMonthly(page);
  expect((await (await request.get("/api/researches")).json()).length).toBe(0);
  await expect(page.getByText("尚无保存规则。先保存明确的研究规则版本，再回到这里提交实验。", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "前往研究规则", exact: true}).click();
  await expect(page.getByLabel("规则预设", {exact: true})).toBeVisible();
  const rule = await projectRule(request, "Monthly browser first rule");
  await page.getByRole("button", {name: "月度实验", exact: true}).click();
  await configure(page, rule);
  const submitted = page.waitForResponse(response => new URL(response.url()).pathname === "/api/monthly-experiments" && response.request().method() === "POST");
  await page.getByRole("button", {name: "提交月度实验", exact: true}).click();
  const response = await submitted; expect(response.status()).toBe(201);
  const {experiment_id: id} = await response.json();
  const detail = await completed(request, id);
  await page.getByRole("button", {name: "完整核验并刷新月度结果", exact: true}).click();
  const output = page.getByRole("region", {name: "月度计算结果", exact: true});
  await expect(output).toBeVisible();
  await expect(output).toContainText("controlled_fixture");
  await expect(output).toContainText("MOM 基线");
  await expect(output).toContainText("MOM × ID 对照");
  await output.locator("details[aria-label='月度明细 2025-01'] > summary").click();
  await page.getByText("2025-01 形成权重与信号分组", {exact: true}).click();
  await expect(output).toContainText(detail.result!.months[0].eligible_assets[0]);
  await page.getByText("2025-01 标签覆盖与排除原因", {exact: true}).click();
  await expect(output).toContainText("有效 / 应有日");
  await page.getByLabel("月度审核声明来源", {exact: true}).selectOption("automation");
  await page.getByLabel("月度审核依据", {exact: true}).fill("Browser automation: inspect exact stored result, no human research judgment.");
  await page.getByRole("button", {name: "保存月度审核", exact: true}).click();
  await expect.poll(async () => (await (await request.get(`/api/monthly-experiments/${id}`)).json()).reviews.length).toBe(1);
  await page.getByRole("button", {name: "冻结月度报告", exact: true}).click();
  await expect.poll(async () => (await (await request.get(`/api/monthly-experiments/${id}`)).json()).reports.length).toBe(1);
  const final = await (await request.get(`/api/monthly-experiments/${id}`)).json() as MonthlyDetail;
  const reportId = final.reports[0].id;
  const json = await (await request.get(`/api/monthly-experiments/${id}/reports/${reportId}/json`)).json();
  expect(json.reviews).toHaveLength(1); expect(json.reviews[0].source).toBe("automation");
  expect(json.result).toEqual(detail.result);
  const markdown = await request.get(`/api/monthly-experiments/${id}/reports/${reportId}/markdown`);
  expect(markdown.ok()).toBe(true);
  expect(await markdown.text()).toBe(json.markdown);
  await expect(page.getByRole("link", {name: `下载月度 JSON ${reportId}`, exact: true}).first()).toBeVisible();
  await page.screenshot({path: testInfo.outputPath("monthly-output-and-frozen-report.png"), fullPage: true});
  expect((await (await request.get("/api/researches")).json()).length).toBe(0);
});

test("committed monthly creation response loss reloads and confirms exactly the original request", async ({page, request}) => {
  const rule = await projectRule(request, "Monthly browser recovery rule");
  await openMonthly(page); await configure(page, rule);
  const bodies: unknown[] = []; let first = true, committed = "";
  await page.route("**/api/monthly-experiments", async route => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postDataJSON());
    if (!first) return route.continue();
    first = false; const response = await route.fetch(); expect(response.status()).toBe(201);
    committed = (await response.json()).experiment_id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "提交月度实验", exact: true}).click();
  await expect.poll(() => committed).not.toBe("");
  await expect(page.getByRole("region", {name: "月度实验提交恢复", exact: true})).toContainText("结果待确认");
  await page.reload(); await page.getByRole("button", {name: "月度实验", exact: true}).click();
  await expect(page.getByLabel("月度实验规则版本", {exact: true})).toBeDisabled();
  await page.getByRole("button", {name: "安全重试月度实验提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度实验提交恢复", exact: true})).toHaveCount(0);
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  const records = await (await request.get("/api/monthly-experiments?limit=100&offset=0")).json();
  expect(records.items.filter((item: {protocol_id: string}) => item.protocol_id === rule.id)).toHaveLength(1);
  await expect(page.getByRole("region", {name: "月度实验详情", exact: true})).toHaveAttribute("data-experiment-id", committed);
});

test("browser durable storage failure blocks monthly POST instead of creating an untraceable request", async ({page, request}) => {
  const rule = await projectRule(request, "Monthly browser storage failure rule");
  await page.addInitScript(() => {
    const set = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.includes("monthly-create-request")) throw new DOMException("Injected local storage failure", "QuotaExceededError");
      return set.call(this, key, value);
    };
  });
  let writes = 0;
  page.on("request", req => { if (new URL(req.url()).pathname === "/api/monthly-experiments" && req.method() === "POST") writes++; });
  await openMonthly(page); await configure(page, rule);
  await page.getByRole("button", {name: "提交月度实验", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度实验提交", exact: true})).toContainText("无法持久保存请求，尚未发送");
  expect(writes).toBe(0);
  const records = await (await request.get("/api/monthly-experiments?limit=100&offset=0")).json();
  expect(records.items.filter((item: {protocol_id: string}) => item.protocol_id === rule.id)).toHaveLength(0);
});

test("review and frozen report response losses replay exact original targets after reload even when detail later fails", async ({page, request}) => {
  const rule = await projectRule(request, "Monthly browser review-report recovery rule");
  const id = await apiExperiment(request, rule, "monthly-review-report-recovery");
  const initial = await completed(request, id);
  await openMonthly(page);
  await page.getByRole("button", {name: `查看月度实验 ${id}`, exact: true}).click();
  await expect(page.getByLabel("月度审核依据", {exact: true})).toBeEnabled();
  await page.getByLabel("月度审核声明来源", {exact: true}).selectOption("automation");
  await page.getByLabel("月度审核依据", {exact: true}).fill("Automation response-loss recovery fixture; no human judgment.");
  const reviews: unknown[] = [], reports: unknown[] = []; let reviewId = "", reportId = "", loseReview = true, loseReport = true;
  await page.route(`**/api/monthly-experiments/${id}/reviews`, async route => {
    reviews.push(route.request().postDataJSON());
    if (!loseReview) return route.continue();
    loseReview = false; const response = await route.fetch(); expect(response.status()).toBe(201);
    reviewId = (await response.json()).id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "保存月度审核", exact: true}).click();
  await expect.poll(() => reviewId).not.toBe("");
  await page.reload(); await page.getByRole("button", {name: "月度实验", exact: true}).click();
  await page.getByRole("button", {name: `查看月度实验 ${id}`, exact: true}).click();
  await page.getByRole("button", {name: "安全重试月度审核提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度审核提交恢复", exact: true})).toHaveCount(0);
  expect(reviews).toHaveLength(2); expect(reviews[1]).toEqual(reviews[0]);
  const withReview = await (await request.get(`/api/monthly-experiments/${id}`)).json() as MonthlyDetail;
  expect(withReview.reviews.map(item => item.id)).toEqual([reviewId]);
  expect(withReview.reviews[0].result_digest).toBe(initial.verification.result_digest);
  await page.route(`**/api/monthly-experiments/${id}/reports`, async route => {
    reports.push(route.request().postDataJSON());
    if (!loseReport) return route.continue();
    loseReport = false; const response = await route.fetch(); expect(response.status()).toBe(201);
    reportId = (await response.json()).id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "冻结月度报告", exact: true}).click();
  await expect.poll(() => reportId).not.toBe("");
  // A failing current detail must not hide confirmation of an already committed report.
  await page.route(`**/api/monthly-experiments/${id}`, route => route.fulfill({status: 409, json: {detail: "Injected later detail integrity failure"}}));
  await page.reload(); await page.getByRole("button", {name: "月度实验", exact: true}).click();
  await page.getByRole("button", {name: `查看月度实验 ${id}`, exact: true}).click();
  await expect(page.getByRole("region", {name: "月度计算结果", exact: true})).toHaveCount(0);
  await page.getByRole("button", {name: "安全重试月度报告提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度报告提交恢复", exact: true})).toHaveCount(0);
  expect(reports).toHaveLength(2); expect(reports[1]).toEqual(reports[0]);
  await expect(page.getByRole("link", {name: `下载月度 JSON ${reportId}`, exact: true})).toBeVisible();
  const final = await (await request.get(`/api/monthly-experiments/${id}`)).json() as MonthlyDetail;
  expect(final.reports.map(item => item.id)).toEqual([reportId]);
  const frozen = await (await request.get(`/api/monthly-experiments/${id}/reports/${reportId}/json`)).json();
  expect(frozen.reviews.map((item: {id: string}) => item.id)).toEqual([reviewId]);
});

test("late selected detail and a failed full integrity read cannot restore an older result", async ({page, request}) => {
  const rule = await projectRule(request, "Monthly browser detail scope rule");
  const first = await apiExperiment(request, rule, "monthly-scope-first"); await completed(request, first);
  const second = await apiExperiment(request, rule, "monthly-scope-second"); await completed(request, second);
  await openMonthly(page);
  await page.getByRole("button", {name: `查看月度实验 ${second}`, exact: true}).click();
  await expect(page.getByRole("region", {name: "月度计算结果", exact: true})).toBeVisible();
  let release!: () => void, arrival!: () => void, finished!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; }), arrived = new Promise<void>(resolve => { arrival = resolve; }), done = new Promise<void>(resolve => { finished = resolve; });
  await page.route(`**/api/monthly-experiments/${first}`, async route => {
    const response = await route.fetch(); arrival(); await hold;
    try { await route.fulfill({response}); } catch { /* Selected detail request was deliberately aborted. */ } finally { finished(); }
  });
  await page.getByRole("button", {name: `查看月度实验 ${first}`, exact: true}).click(); await arrived;
  let statusReads = 0;
  await page.route(`**/api/monthly-experiments/${second}/status`, async route => {
    const response = await route.fetch(); statusReads++; await route.fulfill({response});
  });
  await page.getByRole("button", {name: `查看月度实验 ${second}`, exact: true}).click();
  await expect(page.getByRole("region", {name: "月度实验详情", exact: true})).toHaveAttribute("data-experiment-id", second);
  release(); await done;
  await expect(page.getByRole("region", {name: "月度计算结果", exact: true})).toBeVisible();
  await expect.poll(() => statusReads).toBeGreaterThanOrEqual(1);
  // The transport fixture proves that a contract-valid 409 clears cached metrics.
  // Backend file-tamper detection is separately tested by the service suite.
  let failedDetailReads = 0;
  await page.route(`**/api/monthly-experiments/${second}`, route => { failedDetailReads++; return route.fulfill({status: 409, json: {detail: "Injected source integrity failure"}}); });
  await page.getByRole("button", {name: "完整核验并刷新月度结果", exact: true}).click();
  await expect(page.getByRole("region", {name: "月度计算结果", exact: true})).toHaveCount(0);
  await expect(page.getByRole("region", {name: "月度实验详情", exact: true})).toContainText("旧结果已隐藏");
  await expect.poll(() => statusReads, {timeout: 10000}).toBeGreaterThanOrEqual(2);
  expect(failedDetailReads).toBe(1);
});
