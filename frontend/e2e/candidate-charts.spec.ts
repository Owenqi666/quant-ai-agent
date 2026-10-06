import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ArtifactResponse, CandidateSeriesResponse, ResearchDetail, RunDetail } from "../src/generated/api-contract";

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
const test = base.extend<{}, { candidateChartsServer: { url:string; home:string } }>({
  candidateChartsServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-candidate-charts-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Review evidence acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned candidate charts server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned candidate charts server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `candidate-charts-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned candidate charts launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ candidateChartsServer }, use) => use(candidateChartsServer.url),
});

async function completedResearch(request: APIRequestContext, constant=false) {
  const exampleResponse = await request.post("/api/examples/alpha101", {data:{}});
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const original = await (await request.get(`/api/researches/${example.research_id}`)).json() as ResearchDetail;
  const task=structuredClone(original.revisions[0].task);
  if(constant) {
    const candidate=task.candidates.find(item=>item.id==="alpha101")!;
    candidate.expression="close - close";candidate.origin="user_modification";
    candidate.changes=["Automation fixture: constant factor must remain not evaluable"];
    task.candidates=[candidate];
  }
  const create = await request.post("/api/researches", {data:{title:`Candidate charts automation ${randomUUID()}`,
    paper_id:original.paper_id,dataset_id:original.dataset_id,task,idempotency_key:randomUUID()}});
  expect(create.status()).toBe(201);
  const research = await create.json() as ResearchDetail;
  const queued = await request.post("/api/runs",{data:{revision_id:research.latest_revision_id,mode:"normalized_fixed",idempotency_key:randomUUID()}});
  expect(queued.status()).toBe(201);
  const {id}=await queued.json();
  await expect.poll(async()=> (await (await request.get(`/api/runs/${id}`)).json()).status,{timeout:30000}).toBe("completed");
  const run=await (await request.get(`/api/runs/${id}`)).json() as RunDetail;
  expect(run.verification?.verified).toBe(true);
  const artifacts=await (await request.get(`/api/runs/${id}/artifacts`)).json() as ArtifactResponse[];
  return {research,run,artifacts};
}
async function openReview(page: Page, research: ResearchDetail, run: RunDetail, candidate="alpha006") {
  await page.goto("/");
  await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:research.title,exact:true}).click();
  await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await page.getByLabel("选择已结束的实验",{exact:true}).selectOption(run.id);
  await page.getByLabel("审核候选",{exact:true}).selectOption(candidate);
  return page.getByRole("region",{name:"本次候选输出",exact:true});
}

function seriesURL(run:RunDetail,candidateId:string) {
  const target=run.review_targets.find(item=>item.candidate_id===candidateId)!;
  return `/api/runs/${run.id}/candidates/${candidateId}/series?${new URLSearchParams({attempt_id:target.attempt_id,result_digest:target.result_digest})}`;
}

test("real series, saved result, SVG values and accessible table refer to the same immutable candidate",async({page,request},testInfo)=>{
  const {research,run,artifacts}=await completedResearch(request);
  const response=await request.get(seriesURL(run,"alpha101"));expect(response.status()).toBe(200);
  const series=await response.json() as CandidateSeriesResponse;
  const artifact=artifacts.find(item=>item.id===series.source.artifact_id)!;
  const result=await (await request.get(`/api/runs/${run.id}/artifacts/${artifact.id}`)).json();
  expect(series.points.map(point=>point.gross_return)).toEqual(result.daily.map((point:{gross_return:number|null})=>point.gross_return));
  expect(series.summary.mean_gross_return).toBe(result.metrics.mean_gross_return);
  let cumulative=0;
  for(const point of series.points) {
    if(point.status==="evaluated") {cumulative+=point.gross_return!;expect(point.cumulative_gross_return).toBeCloseTo(cumulative,14);}
    else expect(point.cumulative_gross_return).toBeNull();
    if(point.status==="purged") {expect(point.coverage).toBeNull();expect(point.available_assets).toBeNull();}
  }
  await openReview(page,research,run,"alpha101");
  const panel=page.getByRole("region",{name:"候选结果图表",exact:true});
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-digest",series.result_digest);
  await expect(panel.getByText("合成数据，仅用于检查软件链路。",{exact:false})).toBeVisible();
  const returns=panel.getByRole("figure",{name:"每日毛收益",exact:true});
  const rendered=await returns.locator("circle").evaluateAll(nodes=>nodes.map(node=>({date:node.getAttribute("data-date"),value:Number(node.getAttribute("data-value"))})));
  expect(rendered).toEqual(series.points.filter(point=>point.gross_return!==null).map(point=>({date:point.exit_date,value:point.gross_return})));
  const details=panel.getByRole("table",{name:"候选逐日计算明细",exact:true});
  await expect(details).toBeHidden();
  await panel.getByText(/^逐日明细与缺失原因/).click();
  for(const point of series.points.slice(0,15)) {
    const row=details.getByRole("row").filter({has:page.getByRole("rowheader",{name:point.signal_date,exact:true})});
    await expect(row.getByRole("cell").nth(2)).toHaveText(point.gross_return===null?"—":`${(point.gross_return*100).toFixed(4)}%`);
    await expect(row.getByRole("cell").nth(4)).toHaveText(point.rank_ic===null?"—":point.rank_ic.toFixed(6));
  }
  while(await panel.getByRole("button",{name:"下一页明细",exact:true}).isEnabled())await panel.getByRole("button",{name:"下一页明细",exact:true}).click();
  const purged=details.getByRole("row").filter({hasText:"边界剔除"});
  await expect(purged).toHaveCount(series.summary.purged_days);
  await expect(purged.first()).toContainText("不适用");
  await panel.getByText(/^逐日明细与缺失原因/).click();
  await panel.screenshot({path:testInfo.outputPath("candidate-results-charts.png")});
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(0);
});

test("a late series response cannot replace another selected candidate and a failed read can be retried",async({page,request})=>{
  const {research,run}=await completedResearch(request);
  let release!:()=>void, observed!:()=>void, settled!:()=>void;
  const held=new Promise<void>(resolve=>{release=resolve;}), entered=new Promise<void>(resolve=>{observed=resolve;}), finished=new Promise<void>(resolve=>{settled=resolve;});
  let hold=true;
  await page.route(`**/api/runs/${run.id}/candidates/alpha006/series?*`,async route=>{
    const response=await route.fetch();
    if(hold) {hold=false;observed();await held;}
    try {await route.fulfill({response});} catch(error) {
      if(!String(error).includes("Invalid InterceptionId")&&!String(error).includes("Target page")&&!String(error).includes("canceled"))throw error;
    } finally {settled();}
  });
  await openReview(page,research,run,"alpha006");
  await entered;
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  const panel=page.getByRole("region",{name:"候选结果图表",exact:true});
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  release();
  await finished;
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  let fail=true;
  await page.route(`**/api/runs/${run.id}/candidates/alpha101/series?*`,async route=>{
    if(fail)await route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({detail:"Injected temporary series read failure"})});
    else await route.continue();
  });
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha006");
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha006");
  await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
  await expect(panel.getByRole("alert")).toContainText("图表暂不可用");
  await expect(panel.locator("svg")).toHaveCount(0);
  fail=false;await panel.getByRole("button",{name:"重试读取图表",exact:true}).click();
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
});

test("constant-factor output has no invented zero returns or IC while coverage and skip reasons remain inspectable",async({page,request})=>{
  const {research,run}=await completedResearch(request,true);
  const response=await request.get(seriesURL(run,"alpha101"));expect(response.status()).toBe(200);
  const series=await response.json() as CandidateSeriesResponse;
  expect(series.status).toBe("not_evaluable");expect(series.summary.evaluated_days).toBe(0);
  expect(series.points.every(point=>point.gross_return===null&&point.rank_ic===null)).toBe(true);
  await openReview(page,research,run,"alpha101");
  const panel=page.getByRole("region",{name:"候选结果图表",exact:true});
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  await expect(panel.getByText("本序列没有可绘制值；请在逐日明细中查看原因。",{exact:true})).toHaveCount(3);
  await expect(panel.getByRole("figure",{name:"有效资产覆盖比例",exact:true}).locator("svg")).toBeVisible();
  await panel.getByText(/^逐日明细与缺失原因/).click();
  const first=panel.getByRole("table",{name:"候选逐日计算明细",exact:true}).getByRole("row").nth(1);
  await expect(first).toContainText("因子横截面为常量");
  await expect(first.getByRole("cell").nth(2)).toHaveText("—");
  await expect(first.getByRole("cell").nth(4)).toHaveText("—");
});

test("tampered calculation bytes are refused instead of rendering another candidate or retained chart",async({page,request,candidateChartsServer})=>{
  const {research,run}=await completedResearch(request);
  const response=await request.get(seriesURL(run,"alpha101"));expect(response.status()).toBe(200);
  const series=await response.json() as CandidateSeriesResponse;
  await openReview(page,research,run,"alpha101");
  const panel=page.getByRole("region",{name:"候选结果图表",exact:true});
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  const path=join(candidateChartsServer.home,"runs",run.id,"attempts",series.attempt_id,"output",series.source.artifact_name);
  const original=readFileSync(path);
  try {
    writeFileSync(path,Buffer.concat([original,Buffer.from("\n")]));
    await page.getByLabel("审核候选",{exact:true}).selectOption("alpha006");
    await expect(panel.getByRole("alert")).toContainText("图表暂不可用");
    await page.getByLabel("审核候选",{exact:true}).selectOption("alpha101");
    await expect(panel.getByRole("alert")).toContainText("图表暂不可用");
    await expect(panel.locator("svg")).toHaveCount(0);
    expect((await request.get(seriesURL(run,"alpha101"))).status()).toBe(409);
  } finally {writeFileSync(path,original);}
  await panel.getByRole("button",{name:"重试读取图表",exact:true}).click();
  await expect(panel.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
});
