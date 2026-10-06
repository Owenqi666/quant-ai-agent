import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunDetail, RunComparison, ResearchReport } from "../src/generated/api-contract";
import { assertApiResponse } from "../src/generated/api-contract";

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
const test = base.extend<{}, { insightsServer: string }>({
  insightsServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-insights-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Insights acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned insights server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned insights server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `insights-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned insights launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ insightsServer }, use) => use(insightsServer),
});

interface SeedResearch { id: string; title: string; latest_revision_id: string; revisions: {id:string; task: Record<string,unknown>}[] }
async function researchExample(request: APIRequestContext): Promise<SeedResearch> {
  const response = await request.post("/api/examples/alpha101", { data: {} });
  expect(response.status()).toBe(200);
  const example = await response.json();
  const research = await (await request.get(`/api/researches/${example.research_id}`)).json();
  const created = await request.post("/api/researches", { data: {
    title: `Insights automation ${randomUUID()}`, paper_id: research.paper_id, dataset_id: research.dataset_id,
    task: research.revisions[0].task, idempotency_key: randomUUID(),
  } });
  expect(created.status()).toBe(201);
  return await created.json();
}
async function completedRun(request: APIRequestContext, revisionId: string): Promise<RunDetail> {
  const response = await request.post("/api/runs", { data: {revision_id:revisionId, mode:"normalized_fixed", idempotency_key:randomUUID()} });
  expect(response.status()).toBe(201);
  const submitted = await response.json();
  await expect.poll(async () => (await (await request.get(`/api/runs/${submitted.id}`)).json()).status, {timeout:30000}).toBe("completed");
  const result = await (await request.get(`/api/runs/${submitted.id}`)).json() as RunDetail;
  expect(result.verification?.verified).toBe(true);
  return result;
}
async function openInsights(page: Page, research: SeedResearch) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact:true})).toBeVisible();
  await page.getByRole("button", {name:research.title, exact:true}).click();
  await page.getByRole("button", {name:"研究对比与总结", exact:true}).click();
  await expect(page.getByRole("region", {name:"冻结研究总结", exact:true})).toBeVisible();
  await page.getByText("高级：比较两次实验", {exact:true}).click();
  await page.getByText("高级：自选多次实验的报告范围", {exact:true}).click();
}
async function pickComparison(page: Page, baseline: string, candidate: string) {
  await page.getByLabel("基准实验", {exact:true}).selectOption(baseline);
  await page.getByLabel("候选实验", {exact:true}).selectOption(candidate);
  await page.getByRole("button", {name:"比较所选实验", exact:true}).click();
  return page.getByRole("generic", {name:"已保存结果对比", exact:true});
}

test("comparison uses real saved metrics, clears stale selections and suppresses deltas after evaluation changes", async ({page, request}, testInfo) => {
  const research = await researchExample(request);
  const first = await completedRun(request, research.latest_revision_id);
  const repeat = await completedRun(request, research.latest_revision_id);
  await openInsights(page, research);
  await pickComparison(page, first.id, repeat.id);
  const comparison = page.locator('[aria-label="已保存结果对比"]');
  await expect(comparison).toContainText("评估条件可比较");
  const sameResponse = await request.get(`/api/run-comparison?baseline_run_id=${first.id}&candidate_run_id=${repeat.id}`);
  expect(sameResponse.status()).toBe(200);
  const same = await sameResponse.json() as RunComparison;
  assertApiResponse("/api/run-comparison", "GET", 200, same);
  expect(same.comparable).toBe(true);
  const finite = same.candidates.flatMap((candidate) => candidate.metrics.filter((metric) => metric.delta !== null));
  expect(finite.length).toBeGreaterThan(0);
  for (const metric of finite) {
    expect(metric.delta).toBe(0);
    expect(metric.baseline_source?.run_id).toBe(first.id);
    expect(metric.candidate_source?.run_id).toBe(repeat.id);
  }
  const candidate = same.candidates.find((item) => item.candidate_id === "alpha006")!;
  const metric = candidate.metrics.find((item) => item.delta !== null)!;
  const candidateRegion = page.locator('[aria-label="候选对比 alpha006"]');
  const metricRow = candidateRegion.getByRole("row").filter({has:page.getByRole("rowheader", {name:metric.name, exact:true})});
  await expect(metricRow.getByRole("cell").nth(2)).toHaveText("0");
  await metricRow.getByText(`查看 ${metric.name} 的指标来源`, {exact:true}).click();
  await expect(metricRow.locator("pre")).toContainText(first.id);
  await expect(metricRow.locator("pre")).toContainText(metric.baseline_source!.json_pointer);
  await page.getByLabel("候选实验", {exact:true}).selectOption(first.id);
  await expect(comparison).toHaveCount(0);

  const task = structuredClone(research.revisions[0].task) as {evaluation:{min_assets:number}};
  task.evaluation.min_assets += 1;
  const revised = await request.post(`/api/researches/${research.id}/revisions`, {data:{base_revision_id:research.latest_revision_id, task, note:"Automation: deliberately change evaluation min_assets"}});
  expect(revised.status()).toBe(201);
  const revision = await revised.json();
  const changed = await completedRun(request, revision.id);
  await page.getByRole("button", {name:"刷新候选实验选项", exact:true}).click();
  await pickComparison(page, first.id, changed.id);
  await expect(comparison).toContainText("存在不一致或未知条件");
  const different = await (await request.get(`/api/run-comparison?baseline_run_id=${first.id}&candidate_run_id=${changed.id}`)).json() as RunComparison;
  expect(different.comparable).toBe(false);
  expect(different.conditions.some((item) => item.name === "evaluation" && item.status === "changed" && item.blocking)).toBe(true);
  for (const item of different.candidates) for (const value of item.metrics) expect(value.delta).toBeNull();
  await expect(candidateRegion.getByText("不列指标差值", {exact:false})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("incompatible-evaluation-comparison.png"), fullPage:true});
});

test("report creation survives lost committed response and list failure; later review cannot change frozen downloads", async ({page, request}, testInfo) => {
  const research = await researchExample(request);
  const run = await completedRun(request, research.latest_revision_id);
  await openInsights(page, research);
  await page.getByLabel("加入总结的实验", {exact:true}).selectOption(run.id);
  await page.getByRole("button", {name:"加入总结范围", exact:true}).click();
  await expect(page.getByRole("list", {name:"总结实验范围", exact:true})).toContainText(run.id);
  const requestBodies: unknown[] = [];
  let committed: ResearchReport | undefined;
  const path = `/api/researches/${research.id}/reports`;
  await page.route(`**${path}`, async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    requestBodies.push(route.request().postDataJSON());
    if (requestBodies.length === 1) {
      const response = await route.fetch(); expect(response.status()).toBe(201);
      committed = await response.json() as ResearchReport;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", {name:"生成冻结研究总结", exact:true}).click();
  await expect.poll(() => committed?.id || "").not.toBe("");
  await expect(page.getByRole("region", {name:"研究总结提交恢复", exact:true})).toContainText("待确认");
  const before = await (await request.get(path)).json();
  expect(before.total).toBe(1);
  const report = committed!;
  const jsonBefore = await (await request.get(`/api/research-reports/${report.id}/export?format=json`)).text();
  const markdownBefore = await (await request.get(`/api/research-reports/${report.id}/export?format=markdown`)).text();
  await page.reload();
  await page.getByRole("button", {name:"研究对比与总结", exact:true}).click();
  await expect(page.getByRole("button", {name:"安全重试研究总结", exact:true})).toBeVisible();
  await expect(page.getByRole("row").filter({hasText:report.id})).toBeVisible();
  let failedList = false;
  await page.route(`**${path}?*`, async (route) => {
    if (!failedList) { failedList = true; await route.fulfill({status:500, contentType:"application/json", body:JSON.stringify({detail:"Injected report list failure after saved replay"})}); }
    else await route.continue();
  });
  await page.getByRole("button", {name:"安全重试研究总结", exact:true}).click();
  await expect(page.getByText(/研究总结已保存，列表刷新暂未成功/)).toBeVisible();
  expect(failedList).toBe(true);
  expect(requestBodies).toHaveLength(2); expect(requestBodies[1]).toEqual(requestBodies[0]);
  expect((await (await request.get(path)).json()).total).toBe(1);
  const detail = page.getByRole("article", {name:"固定研究总结详情", exact:true});
  await expect(detail).toContainText(report.id);
  await expect(detail.getByRole("link", {name:`下载 JSON 总结 ${report.id}`, exact:true})).toHaveAttribute("href", `/api/research-reports/${report.id}/export?format=json`);
  await expect(detail.getByRole("link", {name:`下载 Markdown 总结 ${report.id}`, exact:true})).toHaveAttribute("href", `/api/research-reports/${report.id}/export?format=markdown`);
  await expect(page.getByRole("button", {name:"安全重试研究总结", exact:true})).toHaveCount(0);
  await page.getByText("高级：自选多次实验的报告范围", {exact:true}).click();
  await expect(page.getByRole("button", {name:"生成冻结研究总结", exact:true})).toBeDisabled();
  const target = run.review_targets.find((item) => item.candidate_id === "alpha006")!;
  const review = await request.post(`/api/runs/${run.id}/reviews`, {data:{
    candidate_id:target.candidate_id, verdict:"needs_changes", category:"evidence", source:"automation",
    note:"Later automation-only record; not a human research judgment", expected_attempt_id:target.attempt_id,
    expected_result_digest:target.result_digest, idempotency_key:randomUUID(),
  }});
  expect(review.status()).toBe(201);
  const after = await (await request.get(`/api/research-reports/${report.id}`)).json() as ResearchReport;
  expect(after.payload_digest).toBe(report.payload_digest);
  expect(after.payload).toEqual(report.payload);
  expect(await (await request.get(`/api/research-reports/${report.id}/export?format=json`)).text()).toBe(jsonBefore);
  expect(await (await request.get(`/api/research-reports/${report.id}/export?format=markdown`)).text()).toBe(markdownBefore);
  // Simulate the server's page-size resource rejection; smaller pages still
  // read real frozen reports from the owned API rather than mock summary rows.
  const secondResponse = await request.post(path, {data:{run_ids:[run.id], idempotency_key:randomUUID()}});
  expect(secondResponse.status()).toBe(201);
  const second = await secondResponse.json() as ResearchReport;
  const pageRequests: {limit:string|null; offset:string|null}[] = [];
  await page.route(`**${path}?*`, async (route) => {
    const query = new URL(route.request().url()).searchParams;
    pageRequests.push({limit:query.get("limit"), offset:query.get("offset")});
    if (query.get("limit") === "20") await route.fulfill({status:413, contentType:"application/json", body:JSON.stringify({detail:"Injected report page resource limit; select a smaller page"})});
    else await route.continue();
  });
  await page.getByRole("button", {name:"刷新总结列表", exact:true}).click();
  await expect(page.getByText(/本页报告超过读取大小限制/)).toBeVisible();
  await page.getByLabel("总结列表每页数量", {exact:true}).selectOption("1");
  await expect(page.getByText(/已加载 1 项，总计 2 项/)).toBeVisible();
  await expect(page.getByRole("row").filter({hasText:second.id})).toBeVisible();
  await expect(page.getByText(/本页报告超过读取大小限制/)).toHaveCount(0);
  await page.getByRole("button", {name:"加载更多总结", exact:true}).click();
  await expect(page.getByText(/已加载 2 项，总计 2 项/)).toBeVisible();
  await expect(page.getByRole("row").filter({hasText:report.id})).toBeVisible();
  expect(pageRequests).toEqual([{limit:"20",offset:"0"},{limit:"1",offset:"0"},{limit:"1",offset:"1"}]);
  await page.screenshot({path:testInfo.outputPath("frozen-report-confirmed-after-lost-response.png"), fullPage:true});
});
