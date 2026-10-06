import { test as base, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { spawn, execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { StudyDetail, StudyReview, EligibilityScan } from "../src/generated/api-contract";

const root = resolve("..");

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((ok, fail) => { server.once("error", fail); server.listen(0, "127.0.0.1", ok); });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("No loopback TCP port was assigned");
  await new Promise<void>((ok, fail) => server.close((error) => error ? fail(error) : ok()));
  return address.port;
}

// Own workspace isolates normalized browser fixtures from existing acceptance and live records.
const test = base.extend<{}, { studyServer: { url:string; home:string } }>({
  studyServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-study-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Study acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned study server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned study server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `study-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned study launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ studyServer }, use) => use(studyServer.url),
});


// The fixture constructs aggregate counts. It never represents a raw MAT scan.
function scanFixture(minimum = 100, months = 22): EligibilityScan {
  const program = `import json,sys
from paper_alpha.eligibility import demo_scan
from paper_alpha.storage import digest
minimum, count = map(int, sys.argv[1:])
scan=demo_scan(); scan['plan']['minimum_assets']=minimum
start=1993*12+2
keys=[f'{i//12:04d}-{i%12+1:02d}' for i in range(start,start+count)]
scan['plan']['development_end']=keys[-1]
scan['plan_digest']=digest(scan['plan'])
scan['months']=[dict(scan['months'][0],month=month) for month in keys]
print(json.dumps(scan))`;
  return JSON.parse(execFileSync(resolve(root, ".venv/bin/python"), ["-c", program, String(minimum), String(months)], {cwd: root, encoding: "utf8"}));
}
async function openStudies(page: Page) {
  await page.goto("/"); await expect(page.getByText("服务已连接", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "研究准入", exact: true}).click();
  await expect(page.getByRole("region", {name: "研究准入导入", exact: true})).toBeVisible();
}
async function fillStudy(page: Page, title: string, scan = scanFixture()) {
  await page.getByLabel("研究准入标题", {exact: true}).fill(title);
  await page.getByLabel("研究准入说明", {exact: true}).fill("Automated aggregate fixture; not raw source verification or human judgment.");
  await page.getByLabel("研究准入扫描 input.json", {exact: true}).setInputFiles({name: "input.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(scan))});
  await expect(page.getByRole("button", {name: "导入并检查研究准入", exact: true})).toBeEnabled();
}
async function importStudy(page: Page): Promise<StudyDetail> {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === "/api/author-studies" && r.request().method() === "POST");
  await page.getByRole("button", {name: "导入并检查研究准入", exact: true}).click();
  const saved = await response; expect(saved.status()).toBe(201); return saved.json();
}
async function fillReview(page: Page) {
  await expect(page.getByLabel("准入审核结论", {exact: true})).toBeEnabled();
  await page.getByLabel("准入审核结论", {exact: true}).selectOption("data_insufficient");
  await page.getByLabel("准入审核声明来源", {exact: true}).selectOption("automation");
  await page.getByLabel("准入审核依据", {exact: true}).fill("Automated verification of the exact aggregate decision and declared boundary; not a human conclusion.");
}

test("blocked scan exposes evidence and four counts, links a declared automation review and creates an unreviewed revision", async ({page, request}, testInfo) => {
  await openStudies(page); await fillStudy(page, "Study browser blocked case");
  const first = await importStudy(page);
  const output = page.getByRole("region", {name: "研究准入输出", exact: true});
  await expect(output).toContainText("筛查完成：未达到门槛");
  await expect(output).toContainText("组合执行就绪：否");
  await expect(output).toContainText("aggregate_consistency_only");
  await expect(output).toContainText("原 MAT 在服务器重新核验：否");
  await expect(output).toContainText(first.result.evidence.doi);
  await expect(output.getByRole("columnheader", {name: "MOM + DGW + MV", exact: true})).toBeVisible();
  await expect(output).toContainText("当前显示第 1–20 月，共 22 月");
  await output.getByRole("button", {name: "下一页准入月份", exact: true}).click();
  await expect(output).toContainText("当前显示第 21–22 月，共 22 月");
  await expect(page.getByLabel("准入审核声明来源", {exact: true})).toHaveValue("human");
  await fillReview(page);
  const response = page.waitForResponse(r => new URL(r.url()).pathname === `/api/author-studies/${first.id}/reviews` && r.request().method() === "POST");
  await page.getByRole("button", {name: "保存准入审核", exact: true}).click();
  const savedReview = await (await response).json() as StudyReview;
  expect(savedReview.actor).toBe("automation"); expect(savedReview.study_digest).toBe(first.digest);
  await page.getByRole("button", {name: `以审核 ${savedReview.id} 创建修订`, exact: true}).click();
  await expect(page.getByRole("region", {name: "修订来源", exact: true})).toContainText(savedReview.id);
  await expect(page.getByRole("button", {name: "导入并检查研究准入", exact: true})).toBeDisabled();
  await fillStudy(page, "Study browser revised gate", scanFixture(30));
  const revised = await importStudy(page);
  expect(revised.parent_review_id).toBe(savedReview.id); expect(revised.parent_study_id).toBe(first.id);
  expect(revised.result.summary.status).toBe("screen_passed"); expect(revised.result.execution_ready).toBe(false);
  expect(revised.changes.some(change => change.field === "plan.minimum_assets")).toBe(true);
  await expect(page.getByRole("region", {name: "研究准入详情", exact: true})).toHaveAttribute("data-study-id", revised.id);
  await expect(page.getByRole("region", {name: "研究准入输出", exact: true})).toContainText("筛查完成：达到声明的数量门槛");
  await expect(page.getByRole("region", {name: "研究准入审核历史", exact: true})).toContainText("本版本尚无审核，不继承旧版本审核结论。");
  expect((await (await request.get(`/api/author-studies/${revised.id}/reviews`)).json()).items).toEqual([]);
  expect((await (await request.get(`/api/author-studies/${first.id}/reviews`)).json()).items).toHaveLength(1);
  const report = await request.get(`/api/author-studies/${revised.id}/markdown`);
  expect(report.ok()).toBe(true); expect(await report.text()).toContain("screen_passed");
  await expect(page.getByRole("link", {name: "下载研究准入报告", exact: true})).toBeVisible();
  await page.screenshot({path: testInfo.outputPath("study-evidence-decision-and-unreviewed-revision.png"), fullPage: true});
});

test("study creation and review response loss replay the original records across reload even after detail failure", async ({page, request}) => {
  await openStudies(page); await fillStudy(page, "Study browser recovery", scanFixture(100, 2));
  let lose = true, studyId = ""; const bodies: unknown[] = [];
  await page.route("**/api/author-studies", async route => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postDataJSON());
    if (!lose) return route.continue(); lose = false;
    const response = await route.fetch(); expect(response.status()).toBe(201); studyId = (await response.json()).id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "导入并检查研究准入", exact: true}).click();
  await expect.poll(() => studyId).not.toBe("");
  await expect(page.getByRole("region", {name: "研究准入提交恢复", exact: true})).toContainText("结果待确认");
  await page.reload(); await page.getByRole("button", {name: "研究准入", exact: true}).click();
  await expect(page.getByLabel("研究准入标题", {exact: true})).toBeDisabled();
  await page.getByRole("button", {name: "安全重试研究准入提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "研究准入提交恢复", exact: true})).toHaveCount(0);
  expect(bodies).toHaveLength(2); expect(bodies[0]).toEqual(bodies[1]);
  await fillReview(page);
  let loseReview = true, reviewId = ""; const reviews: unknown[] = [];
  await page.route(`**/api/author-studies/${studyId}/reviews`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    reviews.push(route.request().postDataJSON());
    if (!loseReview) return route.continue(); loseReview = false;
    const response = await route.fetch(); expect(response.status()).toBe(201); reviewId = (await response.json()).id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "保存准入审核", exact: true}).click();
  await expect.poll(() => reviewId).not.toBe("");
  await page.route(`**/api/author-studies/${studyId}`, route => route.fulfill({status: 409, json: {detail: "Injected integrity failure after committed review"}}));
  await page.reload(); await page.getByRole("button", {name: "研究准入", exact: true}).click();
  await page.getByRole("button", {name: `查看准入研究 ${studyId}`, exact: true}).click();
  await expect(page.getByRole("region", {name: "研究准入输出", exact: true})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "下载研究准入报告", exact: true})).toHaveCount(0);
  await expect(page.getByLabel("准入审核结论", {exact: true})).toBeDisabled();
  await page.getByRole("button", {name: "安全重试准入审核提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "准入审核提交恢复", exact: true})).toHaveCount(0);
  expect(reviews).toHaveLength(2); expect(reviews[0]).toEqual(reviews[1]);
  const saved = (await (await request.get(`/api/author-studies/${studyId}/reviews`)).json()).items;
  expect(saved.map((item: StudyReview) => item.id)).toEqual([reviewId]);
});

test("unavailable durable storage prevents create and review requests", async ({page, request}) => {
  const seed = await request.post("/api/author-studies", {data: {title: "Study browser storage seed", note: "Automation fixture", scan: scanFixture(100, 2), parent_review_id: null, author_panel_ids: [], idempotency_key: "study-storage-seed"}});
  expect(seed.status()).toBe(201); const id = (await seed.json()).id;
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {if (key.includes("author-study-create-request") || key.includes("author-study-review-request")) throw new DOMException("Injected storage error", "QuotaExceededError"); return original.call(this, key, value);};
  });
  let writes = 0;
  page.on("request", r => {if (new URL(r.url()).pathname.startsWith("/api/author-studies") && r.method() === "POST") writes++;});
  await openStudies(page); await fillStudy(page, "Never sent study", scanFixture(100, 2));
  await page.getByRole("button", {name: "导入并检查研究准入", exact: true}).click();
  await expect(page.getByRole("region", {name: "研究准入导入", exact: true})).toContainText("无法持久保存请求，尚未发送");
  await page.getByRole("button", {name: `查看准入研究 ${id}`, exact: true}).click(); await fillReview(page);
  await page.getByRole("button", {name: "保存准入审核", exact: true}).click();
  await expect(page.getByRole("region", {name: "研究准入审核", exact: true})).toContainText("无法持久保存请求，尚未发送");
  expect(writes).toBe(0);
});

test("wrong artifact clears prior input and double clicks send one study", async ({page}) => {
  await openStudies(page); await fillStudy(page, "Study browser single request", scanFixture(100, 2));
  await page.getByLabel("研究准入扫描 input.json", {exact: true}).setInputFiles({name: "plan.json", mimeType: "application/json", buffer: Buffer.from('{"plan":"wrong artifact"}')});
  await expect(page.getByRole("button", {name: "导入并检查研究准入", exact: true})).toBeDisabled();
  await expect(page.getByRole("region", {name: "研究准入导入", exact: true})).toContainText("文件不是受支持的作者资格扫描");
  await fillStudy(page, "Study browser single request", scanFixture(100, 2));
  let writes = 0;
  await page.route("**/api/author-studies", async route => {if (route.request().method() !== "POST") return route.continue(); writes++; await delay(100); await route.continue();});
  await page.getByRole("button", {name: "导入并检查研究准入", exact: true}).evaluate(element => {(element as HTMLButtonElement).click(); (element as HTMLButtonElement).click();});
  await expect(page.getByRole("region", {name: "研究准入导入", exact: true})).toContainText("准入记录已保存"); expect(writes).toBe(1);
});
