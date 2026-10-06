import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunDetail, ResearchReport } from "../src/generated/api-contract";
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
const test = base.extend<{}, { dailyServer: string }>({
  dailyServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-daily-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Daily acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned daily server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned daily server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `daily-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned daily launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ dailyServer }, use) => use(dailyServer),
});

interface SeedResearch { id: string; title: string; latest_revision_id: string; revisions: {id:string; task: Record<string,unknown>}[] }
async function researchExample(request: APIRequestContext, fixedObservationTask = false): Promise<SeedResearch> {
  const response = await request.post("/api/examples/alpha101", { data: {} });
  expect(response.status()).toBe(200);
  const example = await response.json();
  const research = await (await request.get(`/api/researches/${example.research_id}`)).json();
  const fixed = JSON.parse(readFileSync(join(root, "evaluation_suites/v05/task.json"), "utf8"));
  const task = fixedObservationTask ? Object.fromEntries(["evidence", "hypotheses", "candidates", "evaluation", "budget"].map((key) => [key, fixed[key]])) : research.revisions[0].task;
  const created = await request.post("/api/researches", { data: {
    title: `Daily automation ${randomUUID()}`, paper_id: research.paper_id, dataset_id: research.dataset_id,
    task, idempotency_key: randomUUID(),
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
async function openResearch(page: Page, research: SeedResearch) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact:true})).toBeVisible();
  await page.getByRole("button", {name:research.title,exact:true}).click();
  await expect(page.getByRole("region", {name:"日常研究流程",exact:true})).toBeVisible();
}

test("daily four-step flow produces only the explicitly selected frozen experiment report and replays a lost response", async ({page,request},testInfo) => {
  const research = await researchExample(request);
  const revision2Response = await request.post(`/api/researches/${research.id}/revisions`, {data:{base_revision_id:research.latest_revision_id,task:research.revisions[0].task,note:"Automation: second revision to check frozen experiment identity"}});
  expect(revision2Response.status()).toBe(201);
  const revision2 = await revision2Response.json();
  let observationPosts = 0;
  page.on("request",(req)=>{ if(req.method()==="POST"&&req.url().includes("workflow-observations")) observationPosts++; });
  await openResearch(page,research);
  await page.getByRole("button",{name:"1 · 确认候选",exact:true}).click();
  await expect(page.getByRole("heading",{name:/候选与论文依据/})).toBeVisible();
  await page.getByLabel("研究版本",{exact:true}).selectOption(research.latest_revision_id);
  await page.getByRole("button",{name:"2 · 运行实验",exact:true}).click();
  await page.getByLabel("执行方式",{exact:true}).selectOption("normalized_fixed");
  const queuedResponse = page.waitForResponse((response)=>response.url().endsWith("/api/runs")&&response.request().method()==="POST");
  await page.getByRole("button",{name:"提交实验",exact:true}).click();
  const run = await (await queuedResponse).json() as RunDetail;
  await expect(page.getByRole("button",{name:"继续审核当前实验",exact:true})).toBeEnabled({timeout:30000});
  await page.getByLabel("研究版本",{exact:true}).selectOption(revision2.id);
  const daily = page.getByRole("region",{name:"日常研究流程",exact:true});
  await expect(daily).toContainText("当前实验绑定 v1，与待提交的 v2 不同");
  await expect(daily).toContainText(run.id.slice(0,8));
  await page.getByRole("button",{name:"继续审核当前实验",exact:true}).click();
  await expect(page.getByLabel("选择已结束的实验",{exact:true})).toHaveValue(run.id);
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  await page.getByLabel("审核声明来源",{exact:true}).selectOption("automation");
  await page.getByLabel("审核依据",{exact:true}).fill("Daily automation: verify saved output identity; no human semantic judgment or timing claim.");
  await page.getByRole("button",{name:"保存审核",exact:true}).click();
  await expect(page.locator(".review-history article")).toHaveCount(1);
  await page.getByRole("button",{name:"继续为当前实验生成报告",exact:true}).click();
  const range = page.getByRole("region",{name:"当前实验报告范围",exact:true});
  await expect(range).toContainText(run.id.slice(0,8)); await expect(range).toContainText("v1");
  const path=`/api/researches/${research.id}/reports`;
  expect((await (await request.get(path)).json()).total).toBe(0);
  const bodies:Record<string,unknown>[]=[]; let committed:ResearchReport|undefined;
  await page.route(`**${path}`,async route=>{
    if(route.request().method()!=="POST")return route.continue();
    bodies.push(route.request().postDataJSON());
    if(bodies.length===1){const response=await route.fetch();expect(response.status()).toBe(201);committed=await response.json();await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button",{name:"为当前实验生成报告",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试研究总结",exact:true})).toBeVisible();
  await page.getByRole("button",{name:"安全重试研究总结",exact:true}).click();
  await expect(page.getByRole("article",{name:"固定研究总结详情",exact:true})).toContainText(committed!.id);
  expect(bodies).toHaveLength(2); expect(bodies[0]).toEqual(bodies[1]);
  expect(bodies[0].run_ids).toEqual([run.id]);
  expect(committed!.selected_run_ids).toEqual([run.id]);
  assertApiResponse(path,"POST",201,committed);
  expect((await (await request.get(path)).json()).total).toBe(1);
  const savedRun=await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  expect(savedRun.revision_id).toBe(research.latest_revision_id);
  expect(savedRun.reviews[0].source).toBe("automation");
  expect(savedRun.reviews[0].assessment).toBeNull();
  expect(observationPosts).toBe(0);
  await page.screenshot({path:testInfo.outputPath("daily-selected-experiment-report.png"),fullPage:true});
});

test("review timer must pause before observation; a mounted observation survives navigation and preserves earlier review intervals", async ({page,request},testInfo) => {
  const research=await researchExample(request,true);
  const run=await completedRun(request,research.latest_revision_id);
  await openResearch(page,research);
  await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await page.getByLabel("选择已结束的实验",{exact:true}).selectOption(run.id);
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  await page.getByLabel("审核声明来源",{exact:true}).selectOption("automation");
  await page.getByLabel("添加分项研究审核",{exact:true}).check();
  await page.getByText("可选：本次审核的主动工作时间",{exact:true}).click();
  await page.getByRole("button",{name:"开始主动工作计时",exact:true}).click();
  await page.getByRole("button",{name:"流程观测",exact:true}).click();
  await expect(page.getByRole("alert").filter({hasText:"请先暂停主动工作计时"})).toBeVisible();
  await expect(page.getByRole("button",{name:"暂停主动工作计时",exact:true})).toBeEnabled();
  await page.getByRole("button",{name:"暂停主动工作计时",exact:true}).click();
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();
  await page.getByRole("button",{name:"流程观测",exact:true}).click();
  await page.getByLabel("参与者标识",{exact:true}).fill("Automation local timer fixture; never saved as human observation");
  await page.getByLabel("对材料与工具的熟悉程度",{exact:true}).fill("Unsaved browser fixture; no real human measurement");
  await page.getByText("版本、环境和允许工具（首次记录需确认）",{exact:true}).click();
  await page.getByLabel("执行代码的提交标识",{exact:true}).fill("a".repeat(40));
  await page.getByRole("button",{name:"开始本次记录",exact:true}).click();
  await expect(page.getByRole("button",{name:"结束本次记录",exact:true})).toBeVisible();
  await page.getByRole("button",{name:"暂停流程计时",exact:true}).click();
  // Even a paused but unfinished observation keeps the exclusive timer session.
  await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await expect(page.getByRole("region",{name:"正在记录流程",exact:true})).toContainText("已暂停");
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  await page.getByRole("button",{name:"恢复审核草稿",exact:true}).click();
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();
  await expect(page.getByRole("button",{name:"开始主动工作计时",exact:true})).toHaveCount(0);
  await expect(page.getByText(/本次审核不再启动独立计时/)).toBeVisible();
  await page.getByRole("button",{name:"历史目录",exact:true}).click();
  await expect(page.getByRole("region",{name:"正在记录流程",exact:true})).toContainText("已暂停");
  await page.getByRole("button",{name:"返回流程记录",exact:true}).click();
  await page.getByRole("button",{name:"结束本次记录",exact:true}).click();
  await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  await page.getByRole("button",{name:"恢复审核草稿",exact:true}).click();
  await page.getByText("可选：本次审核的主动工作时间",{exact:true}).click();
  await expect(page.getByRole("button",{name:"开始主动工作计时",exact:true})).toBeVisible();
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();
  expect((await (await request.get(`/api/researches/${research.id}/workflow-observations`)).json()).total).toBe(0);
  await page.screenshot({path:testInfo.outputPath("observation-review-timing-handoff.png"),fullPage:true});
});
