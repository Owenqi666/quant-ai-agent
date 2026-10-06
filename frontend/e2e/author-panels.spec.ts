import { test as base, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { AuthorPanelDetail, AuthorPanel } from "../src/generated/api-contract";

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
const test = base.extend<{}, { authorServer: { url:string; home:string } }>({
  authorServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-author-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Author acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned author server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned author server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `author-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned author launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ authorServer }, use) => use(authorServer.url),
});


// Deliberately synthetic normalized values for UI acceptance. No original MAT
// authentication or economic result is claimed by this fixture.
function panelFixture(rowCount = 22): AuthorPanel {
  const months = Array.from({length: 13}, (_, i) => { const d = new Date(Date.UTC(2017, 5 + i, 1)); return d.toISOString().slice(0, 7); });
  return {
    schema_version: 1, kind: "author_perturbed_monthly_panel", adapter_version: "gjs-v2-monthly-panel-v1",
    source: {doi: "10.7910/DVN/R1UI1J", version: "2.0", file_id: 10519940, filename: "USData.mat", sha256: "0b3f60867708c707816caa9ef856d5579c9c732af24acc734bb47d387ee4ab1d", repository_md5: "a84f4ab4be1b9a26f4822b048abad9fe", bytes: 93635256, assets: 25437, periods: 1140, first_month: "1926-01", last_month: "2020-12"},
    selection: {target_month: "2018-06", row_offset: 0, row_count: rowCount}, months,
    source_observation_dates: months.map(month => `${month}-27`),
    rows: Array.from({length: rowCount}, (_, i) => {
      const returns: Array<number|null> = Array(13).fill(0), states: Array<"value"|"nan"> = Array(13).fill("value");
      if (i === 0) { returns[12] = null; states[12] = "nan"; }
      if (i === 1) { returns[0] = null; states[0] = "nan"; returns[12] = 24; }
      return {source_row: i + 1, asset: `USData.mat:row:${i + 1}`, country: null, returns, return_states: states,
        dgw: i === 1 ? null : 0, dgw_state: i === 1 ? "nan" : "value", market_cap: i === 1 ? 0 : 100, market_cap_state: "value"};
    }),
  };
}
async function openAuthors(page: Page) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "作者数据", exact: true}).click();
  await expect(page.getByRole("region", {name: "作者面板导入", exact: true})).toBeVisible();
}
async function fillPanel(page: Page, title: string, panel = panelFixture()) {
  await page.getByLabel("作者面板标题", {exact: true}).fill(title);
  await page.getByLabel("作者面板说明", {exact: true}).fill("Automated normalized UI fixture; not source-authenticated raw MAT or human review.");
  await page.getByLabel("作者面板 input.json", {exact: true}).setInputFiles({name: "input.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(panel))});
  await expect(page.getByRole("button", {name: "导入并诊断作者面板", exact: true})).toBeEnabled();
}
async function importPanel(page: Page): Promise<AuthorPanelDetail> {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === "/api/author-panels" && r.request().method() === "POST");
  await page.getByRole("button", {name: "导入并诊断作者面板", exact: true}).click();
  const saved = await response; expect(saved.status()).toBe(201);
  return await saved.json();
}

test("author entry exposes independently checked source values, missing reasons and bounded row pages", async ({page, request}, testInfo) => {
  await openAuthors(page);
  await expect(page.getByRole("button", {name: "导入示例研究", exact: true})).toHaveCount(0);
  await fillPanel(page, "Author browser diagnostics");
  const saved = await importPanel(page);
  const output = page.getByRole("region", {name: "作者面板对齐结果", exact: true});
  await expect(output).toBeVisible();
  expect(saved.raw_source_reverified).toBe(false); expect(saved.reference.passed).toBe(true);
  expect(saved.result.rows[0].formation_ready).toBe(true); expect(saved.result.rows[0].label_ready).toBe(false);
  expect(saved.result.rows[1].formation_ready).toBe(false); expect(saved.result.rows[1].label_ready).toBe(true);
  await expect(output).toContainText("2017-06-27");
  await expect(output).toContainText("momentum_missing:2017-06:nan");
  await expect(output).toContainText("标签：label_missing:nan");
  await expect(output).toContainText("market_cap_not_positive");
  await expect(output).toContainText("当前显示第 1–20 行诊断，共 22 行");
  await output.getByRole("button", {name: "下一页原行", exact: true}).click();
  await expect(output).toContainText("当前显示第 21–22 行诊断，共 22 行");
  await output.getByRole("button", {name: "上一页原行", exact: true}).click();
  await output.getByText("检查逐月原 Return 与状态", {exact: true}).click();
  await output.getByText("USData.mat:row:2 · 原行 2", {exact: true}).click();
  await expect(output).toContainText("NaN（缺失）");
  const markdown = await request.get(`/api/author-panels/${saved.id}/markdown`);
  expect(markdown.ok()).toBe(true);
  const text = await markdown.text(); expect(text).toContain("Source: USData.mat");
  expect(text).toContain("| 2 | not encoded | unavailable | unavailable | 0 | 24 | False | True |");
  await expect(page.getByRole("link", {name: "下载作者面板报告", exact: true})).toHaveAttribute("href", `/api/author-panels/${saved.id}/markdown`);
  await page.screenshot({path: testInfo.outputPath("author-panel-source-and-diagnostics.png"), fullPage: true});
  expect((await (await request.get("/api/researches")).json()).length).toBe(0);
});

test("uncertain committed author import reloads and replays exactly the same frozen request", async ({page, request}) => {
  await openAuthors(page); await fillPanel(page, "Author browser recovery");
  const bodies: unknown[] = []; let lose = true, id = "";
  await page.route("**/api/author-panels", async route => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postDataJSON());
    if (!lose) return route.continue();
    lose = false; const response = await route.fetch(); expect(response.status()).toBe(201);
    id = (await response.json()).id; await route.abort("failed");
  });
  await page.getByRole("button", {name: "导入并诊断作者面板", exact: true}).click();
  await expect.poll(() => id).not.toBe("");
  await expect(page.getByRole("region", {name: "作者面板提交恢复", exact: true})).toContainText("结果待确认");
  await page.reload(); await page.getByRole("button", {name: "作者数据", exact: true}).click();
  await expect(page.getByLabel("作者面板标题", {exact: true})).toBeDisabled();
  await page.getByRole("button", {name: "安全重试作者面板提交", exact: true}).click();
  await expect(page.getByRole("region", {name: "作者面板提交恢复", exact: true})).toHaveCount(0);
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  await expect(page.getByRole("region", {name: "作者面板详情", exact: true})).toHaveAttribute("data-panel-id", id);
  const records = await (await request.get("/api/author-panels?limit=100&offset=0")).json();
  expect(records.items.filter((item: {title: string}) => item.title === "Author browser recovery")).toHaveLength(1);
});

test("durable storage failure sends no author import", async ({page}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (key.includes("author-panel-import-request")) throw new DOMException("Injected quota failure", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  let writes = 0;
  page.on("request", r => { if (new URL(r.url()).pathname === "/api/author-panels" && r.method() === "POST") writes++; });
  await openAuthors(page); await fillPanel(page, "Author browser storage failure");
  await page.getByRole("button", {name: "导入并诊断作者面板", exact: true}).click();
  await expect(page.getByRole("region", {name: "作者面板导入", exact: true})).toContainText("无法持久保存请求，尚未发送");
  expect(writes).toBe(0);
});

test("failed integrity refresh hides prior author result and export", async ({page}) => {
  await openAuthors(page); await fillPanel(page, "Author browser failed refresh");
  const saved = await importPanel(page);
  await expect(page.getByRole("region", {name: "作者面板对齐结果", exact: true})).toBeVisible();
  await page.route(`**/api/author-panels/${saved.id}`, route => route.fulfill({status: 409, json: {detail: "Injected immutable record digest mismatch"}}));
  await page.getByRole("button", {name: "完整核验并刷新作者面板", exact: true}).click();
  await expect(page.getByRole("region", {name: "作者面板详情", exact: true})).toContainText("旧结果已隐藏");
  await expect(page.getByRole("region", {name: "作者面板对齐结果", exact: true})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "下载作者面板报告", exact: true})).toHaveCount(0);
});

test("invalid artifact cannot reuse a previous file and double clicks dispatch one author request", async ({page}) => {
  await openAuthors(page); await fillPanel(page, "Author browser single dispatch");
  await page.getByLabel("作者面板 input.json", {exact: true}).setInputFiles({name: "manifest.json", mimeType: "application/json", buffer: Buffer.from('{"report":"wrong file"}')});
  await expect(page.getByRole("button", {name: "导入并诊断作者面板", exact: true})).toBeDisabled();
  await expect(page.getByRole("region", {name: "作者面板导入", exact: true})).toContainText("文件不是受支持的作者月度面板");
  await fillPanel(page, "Author browser single dispatch");
  let writes = 0;
  await page.route("**/api/author-panels", async route => {
    if (route.request().method() !== "POST") return route.continue();
    writes++; await delay(150); await route.continue();
  });
  await page.getByRole("button", {name: "导入并诊断作者面板", exact: true}).evaluate(element => { (element as HTMLButtonElement).click(); (element as HTMLButtonElement).click(); });
  await expect(page.getByRole("region", {name: "作者面板导入", exact: true})).toContainText("作者面板已保存");
  expect(writes).toBe(1);
});
