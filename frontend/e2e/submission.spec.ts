import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunDetail, ResearchDetail } from "../src/generated/api-contract";
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
const test = base.extend<{}, { submissionServer: string }>({
  submissionServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-submission-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Submission acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned submission server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned submission server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `submission-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned submission launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ submissionServer }, use) => use(submissionServer),
});

async function exampleResearch(request: APIRequestContext): Promise<ResearchDetail> {
  const response = await request.post("/api/examples/alpha101", {data:{}});
  expect(response.status()).toBe(200);
  const example = await response.json();
  return await (await request.get(`/api/researches/${example.research_id}`)).json();
}
async function cloneResearch(request: APIRequestContext): Promise<ResearchDetail> {
  const source = await exampleResearch(request);
  const response = await request.post("/api/researches", {data:{
    title:`Submission automation ${randomUUID()}`, paper_id:source.paper_id, dataset_id:source.dataset_id,
    task:source.revisions[0].task, idempotency_key:randomUUID(),
  }});
  expect(response.status()).toBe(201);
  return await response.json();
}
async function openResearch(page: Page, research: ResearchDetail) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact:true})).toBeVisible();
  await page.getByRole("button", {name:research.title, exact:true}).click();
  await expect(page.getByRole("heading", {name:research.title, exact:true})).toBeVisible();
  await expect(page.getByRole("button", {name:"提交实验", exact:true})).toBeEnabled();
}
async function fillCopy(page: Page, title: string) {
  await page.getByRole("button", {name:"复制当前任务新建研究", exact:true}).click();
  await page.getByLabel("研究标题", {exact:true}).fill(title);
  await page.getByLabel("已确认所选数据版本及 train / validation / test 时间切分", {exact:true}).check();
  await expect(page.getByRole("button", {name:"创建研究", exact:true})).toBeEnabled();
}

test("research creation replays its frozen copy after response loss and reload without clearing an unrelated blank draft", async ({page, request}, testInfo) => {
  const source = await exampleResearch(request);
  await openResearch(page, source);
  const title = `Recovered copy ${randomUUID()}`;
  await fillCopy(page, title);
  const bodies: unknown[] = [];
  let committed: ResearchDetail | undefined;
  await page.route("**/api/researches", async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      const response = await route.fetch(); expect(response.status()).toBe(201);
      committed = await response.json() as ResearchDetail;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", {name:"创建研究", exact:true}).click();
  await expect.poll(() => committed?.id || "").not.toBe("");
  await expect(page.getByRole("region", {name:"研究创建恢复", exact:true})).toContainText("待确认");
  await expect(page.getByLabel("研究标题", {exact:true})).toBeDisabled();
  const stored = await page.evaluate(() => {
    const key = Object.keys(localStorage).find((key) => key.includes('"research-request"'))!;
    const pending = JSON.parse(localStorage.getItem(key)!);
    const draft = JSON.parse(localStorage.getItem(pending.context.draft_key)!);
    return {key, pending, draft};
  });
  expect(stored.pending.body.title).toBe(title);
  expect(stored.pending.context.draft_key).toContain(source.id);
  expect(stored.pending.context.draft_key).not.toContain("create:blank");
  const health = await (await request.get("/api/health")).json();
  const blankKey = `paper-alpha:draft:v1:${health.workspace_id}:create:blank`;
  const blankDraft = JSON.stringify({...stored.draft, scope:blankKey, value:{...stored.draft.value, title:"Unrelated blank draft; keep this work"}});
  await page.evaluate(({key, raw}) => localStorage.setItem(key, raw), {key:blankKey, raw:blankDraft});
  await page.reload();
  await page.getByRole("button", {name:"新建研究", exact:true}).first().click();
  await expect(page.getByRole("button", {name:"安全重试研究创建", exact:true})).toBeVisible();
  await page.getByRole("button", {name:"安全重试研究创建", exact:true}).click();
  await expect(page.getByRole("heading", {name:title, exact:true})).toBeVisible();
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  assertApiResponse("/api/researches", "POST", 201, committed);
  const matches = (await (await request.get("/api/researches")).json()).filter((item:ResearchDetail) => item.title === title);
  expect(matches).toHaveLength(1); expect(matches[0].id).toBe(committed!.id);
  const original = await (await request.get(`/api/researches/${committed!.id}`)).json() as ResearchDetail;
  expect(original.revisions).toHaveLength(1);
  expect(original.latest_revision_id).toBe(committed!.latest_revision_id);
  expect(await page.evaluate((key) => localStorage.getItem(key), stored.key)).toBeNull();
  expect(await page.evaluate((key) => localStorage.getItem(key), stored.pending.context.draft_key)).toBeNull();
  expect(await page.evaluate((key) => localStorage.getItem(key), blankKey)).toBe(blankDraft);
  await page.screenshot({path:testInfo.outputPath("research-replay-created-once.png"), fullPage:true});
});

test("run submission confirms the original queued run after response loss and reload even if catalog refresh fails", async ({page, request}, testInfo) => {
  const research = await cloneResearch(request);
  await openResearch(page, research);
  const bodies: unknown[] = [];
  let committed: RunDetail | undefined;
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      const response = await route.fetch(); expect(response.status()).toBe(201);
      committed = await response.json() as RunDetail;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", {name:"提交实验", exact:true}).click();
  await expect.poll(() => committed?.id || "").not.toBe("");
  await expect(page.getByRole("region", {name:"实验提交恢复", exact:true})).toContainText("待确认");
  await expect(page.getByLabel("执行方式", {exact:true})).toBeDisabled();
  const health = await (await request.get("/api/health")).json();
  const key = `paper-alpha:recovery:v1:${JSON.stringify([health.workspace_id,"run-request",research.id])}`;
  const frozen = await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), key);
  expect(frozen.body.revision_id).toBe(research.latest_revision_id);
  expect(frozen.body.mode).toBe("normalized_fixed");
  await page.reload();
  await expect(page.getByRole("button", {name:"安全重试实验提交", exact:true})).toBeVisible();
  let refreshFailed = false;
  await page.route("**/api/catalog/researches?*", async (route) => {
    if (!refreshFailed) { refreshFailed=true; await route.fulfill({status:500, contentType:"application/json", body:JSON.stringify({detail:"Injected catalog failure after confirmed original run"})}); }
    else await route.continue();
  });
  await page.getByRole("button", {name:"安全重试实验提交", exact:true}).click();
  await expect(page.getByText(/实验已保存到队列，列表刷新暂未成功/)).toBeVisible();
  expect(refreshFailed).toBe(true);
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  expect(await page.evaluate((key) => localStorage.getItem(key), key)).toBeNull();
  const catalog = await (await request.get(`/api/catalog/runs?research_id=${research.id}`)).json();
  expect(catalog.total_records).toBe(1); expect(catalog.items[0].id).toBe(committed!.id);
  await expect.poll(async () => (await (await request.get(`/api/runs/${committed!.id}`)).json()).status, {timeout:30000}).toBe("completed");
  const result = await (await request.get(`/api/runs/${committed!.id}`)).json() as RunDetail;
  expect(result.verification?.verified).toBe(true); expect(result.attempt_count).toBe(1);
  await page.getByRole("button", {name:"研究工作台", exact:true}).click();
  await expect(page.getByRole("button", {name:"安全重试实验提交", exact:true})).toHaveCount(0);
  await expect(page.getByRole("button", {name:"提交实验", exact:true})).toBeEnabled();
  await page.screenshot({path:testInfo.outputPath("run-confirmed-despite-catalog-failure.png"), fullPage:true});
});

test("unwritable durable request storage prevents research and run POSTs", async ({page, request}, testInfo) => {
  const research = await cloneResearch(request);
  await openResearch(page, research);
  const beforeResearches = await (await request.get("/api/researches")).json();
  const beforeRuns = await (await request.get(`/api/catalog/runs?research_id=${research.id}`)).json();
  const observed: string[] = [];
  await page.route(/\/api\/(researches|runs)$/, async (route) => {
    if (route.request().method() === "POST") observed.push(route.request().url());
    await route.continue();
  });
  const health = await (await request.get("/api/health")).json();
  const researchRequestKey = `paper-alpha:recovery:v1:${JSON.stringify([health.workspace_id,"research-request"])}`;
  const runRequestKey = `paper-alpha:recovery:v1:${JSON.stringify([health.workspace_id,"run-request",research.id])}`;
  const guidedDraftKey = `paper-alpha:recovery:v1:${JSON.stringify([health.workspace_id,"guided-observation",research.id])}`;
  // Automatic form defaults are legitimate existing drafts, not pending POSTs.
  // Wait for this draft so the refusal scenario also proves byte-for-byte preservation.
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), guidedDraftKey)).not.toBeNull();
  const beforeRecovery = await page.evaluate(() => {
    const snapshot = Object.fromEntries(Object.keys(localStorage).filter((key) => key.startsWith("paper-alpha:recovery:")).sort()
      .map((key) => [key, localStorage.getItem(key)]));
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key: string, value: string) {
      if (key.startsWith("paper-alpha:recovery:")) throw new DOMException("Injected recovery storage refusal", "QuotaExceededError");
      return original.call(this,key,value);
    };
    return snapshot;
  });
  expect(beforeRecovery[researchRequestKey]).toBeUndefined();
  expect(beforeRecovery[runRequestKey]).toBeUndefined();
  await fillCopy(page, `Must not be sent ${randomUUID()}`);
  await page.getByRole("button", {name:"创建研究", exact:true}).click();
  await expect(page.getByText(/无法持久保存请求，尚未发送/)).toBeVisible();
  expect(observed).toEqual([]);
  expect((await (await request.get("/api/researches")).json()).length).toBe(beforeResearches.length);
  await page.getByRole("button", {name:"返回工作台", exact:true}).click();
  await page.getByRole("button", {name:"提交实验", exact:true}).click();
  await expect(page.getByText(/无法持久保存请求，尚未发送/)).toBeVisible();
  expect(observed).toEqual([]);
  const afterRuns = await (await request.get(`/api/catalog/runs?research_id=${research.id}`)).json();
  expect(afterRuns.total_records).toBe(beforeRuns.total_records);
  const afterRecovery = await page.evaluate(() => Object.fromEntries(Object.keys(localStorage)
    .filter((key) => key.startsWith("paper-alpha:recovery:")).sort().map((key) => [key, localStorage.getItem(key)])));
  expect(afterRecovery).toEqual(beforeRecovery);
  expect(await page.evaluate(([researchKey, runKey]) => [localStorage.getItem(researchKey), localStorage.getItem(runKey)],
    [researchRequestKey, runRequestKey])).toEqual([null, null]);
  await testInfo.attach("recovery-storage-before-and-after", {body:JSON.stringify({before:beforeRecovery,after:afterRecovery,
    absentRequestKeys:[researchRequestKey,runRequestKey]},null,2),contentType:"application/json"});
  await page.screenshot({path:testInfo.outputPath("requests-blocked-before-post.png"), fullPage:true});
});
