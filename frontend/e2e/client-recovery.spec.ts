import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ResearchDetail, RunDetail, RevisionResponse, IssueResponse, RegressionCheckResponse } from "../src/generated/api-contract";

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
const test = base.extend<{}, { clientRecoveryServer: { url:string; home:string } }>({
  clientRecoveryServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-client-recovery-e2e-"));
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
          if (child.exitCode !== null) throw new Error(`Owned client recovery server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned client recovery server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `client-recovery-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned client recovery launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ clientRecoveryServer }, use) => use(clientRecoveryServer.url),
});

async function completedResearch(request: APIRequestContext) {
  const exampleResponse = await request.post("/api/examples/alpha101", {data:{}});
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const original = await (await request.get(`/api/researches/${example.research_id}`)).json() as ResearchDetail;
  const task=structuredClone(original.revisions[0].task);
  const create = await request.post("/api/researches", {data:{title:`Client recovery automation ${randomUUID()}`,
    paper_id:original.paper_id,dataset_id:original.dataset_id,task,idempotency_key:randomUUID()}});
  expect(create.status()).toBe(201);
  const research = await create.json() as ResearchDetail;
  const queued = await request.post("/api/runs",{data:{revision_id:research.latest_revision_id,mode:"normalized_fixed",idempotency_key:randomUUID()}});
  expect(queued.status()).toBe(201);
  const {id}=await queued.json();
  await expect.poll(async()=> (await (await request.get(`/api/runs/${id}`)).json()).status,{timeout:30000}).toBe("completed");
  const run=await (await request.get(`/api/runs/${id}`)).json() as RunDetail;
  expect(run.verification?.verified).toBe(true);
  return {research,run};
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

async function openResearch(page:Page,research:ResearchDetail){
  await page.goto("/");await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:research.title,exact:true}).click();
  await page.getByRole("button",{name:"研究工作台",exact:true}).click();
}
async function seedReview(request:APIRequestContext,run:RunDetail){
  const target=run.review_targets.find(item=>item.candidate_id==="alpha101")!;
  const response=await request.post(`/api/runs/${run.id}/reviews`,{data:{candidate_id:"alpha101",verdict:"needs_changes",category:"implementation",
    note:"Automation: recovery test only, no human assessment",source:"automation",idempotency_key:randomUUID(),
    expected_attempt_id:target.attempt_id,expected_result_digest:target.result_digest}});
  expect(response.status()).toBe(201);return await response.json();
}

test("revision commit survives lost response and reload; confirming the original cannot remove newer saved draft bytes",async({page,request})=>{
  const {research}=await completedResearch(request);
  await openResearch(page,research);
  await page.getByRole("button",{name:"创建新版本",exact:true}).click();
  await page.getByLabel("修改说明",{exact:true}).fill("Automation revision response-loss recovery");
  let saved:RevisionResponse|undefined,first=true;
  const bodies:Record<string,unknown>[]=[];
  await page.route(`**/api/researches/${research.id}/revisions`,async route=>{
    bodies.push(route.request().postDataJSON());
    if(first){first=false;const response=await route.fetch();expect(response.status()).toBe(201);saved=await response.json();await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button",{name:"保存新版本",exact:true}).click();
  await expect(page.getByRole("region",{name:"修订提交恢复",exact:true})).toContainText("结果待确认");
  await expect.poll(()=>saved?.id).toBeTruthy();
  const pending=await page.evaluate(()=>Object.entries(localStorage).filter(([key])=>key.includes("revision-request")).map(([,raw])=>JSON.parse(raw))[0]);
  expect(pending.body.base_revision_id).toBe(research.latest_revision_id);
  const replacement=JSON.stringify({...JSON.parse(pending.context.draftRaw),savedAt:new Date().toISOString(),value:{...JSON.parse(pending.context.draftRaw).value,note:"Newer unsent edits must survive original acknowledgement"}});
  await page.evaluate(({key,raw})=>localStorage.setItem(key,raw),{key:pending.context.draftKey,raw:replacement});
  await page.reload();
  // Recovery is visible without opening the old editor or choosing its old base.
  await expect(page.getByRole("button",{name:"安全重试修订提交",exact:true})).toBeVisible();
  await page.getByRole("button",{name:"安全重试修订提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"修订提交恢复",exact:true})).toHaveCount(0);
  expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);
  const updated=await (await request.get(`/api/researches/${research.id}`)).json() as ResearchDetail;
  expect(updated.revisions).toHaveLength(2);expect(updated.latest_revision_id).toBe(saved!.id);
  expect(await page.evaluate(key=>localStorage.getItem(key),pending.context.draftKey)).toBe(replacement);
});

test("issue creation and appended decision reuse their original requests after committed response loss and reload",async({page,request})=>{
  const {research,run}=await completedResearch(request),review=await seedReview(request,run);
  await openReview(page,research,run,"alpha101");
  await page.getByLabel("问题处理声明来源",{exact:true}).selectOption("automation");
  await page.getByLabel("选择原审核",{exact:true}).selectOption(review.id);
  await page.getByLabel("待处理问题依据",{exact:true}).fill("Automation issue creation acknowledgement loss");
  let issue:IssueResponse|undefined,first=true;
  const bodies:Record<string,unknown>[]=[];
  await page.route("**/api/issues",async route=>{
    if(route.request().method()!=="POST"){await route.continue();return;}
    bodies.push(route.request().postDataJSON());
    if(first){first=false;const response=await route.fetch();expect(response.status()).toBe(201);issue=await response.json();await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button",{name:"从审核创建问题",exact:true}).click();
  await expect(page.getByRole("region",{name:"问题创建提交恢复",exact:true})).toContainText("结果待确认");
  await page.reload();await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await page.getByRole("button",{name:"安全重试问题创建提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"问题创建提交恢复",exact:true})).toHaveCount(0);
  expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);
  const listed=await (await request.get(`/api/catalog/issues?research_id=${research.id}`)).json();
  expect(listed.items).toHaveLength(1);expect(listed.items[0].id).toBe(issue!.id);
  await page.getByLabel("问题处理声明来源",{exact:true}).selectOption("automation");
  await page.getByLabel("追加处理状态",{exact:true}).selectOption("proposed");
  await page.getByLabel("本次处理依据",{exact:true}).fill("Automation appended decision acknowledgement loss");
  const decisions:Record<string,unknown>[]=[];let dropped=false;
  await page.route(`**/api/issues/${issue!.id}/events`,async route=>{
    decisions.push(route.request().postDataJSON());
    if(!dropped){dropped=true;const response=await route.fetch();expect(response.status()).toBe(201);await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button",{name:"保存追加处理记录",exact:true}).click();
  await expect(page.getByRole("region",{name:"问题处理提交恢复",exact:true})).toContainText("结果待确认");
  await page.reload();await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  // No issue has been selected after reload; the pending request still owns its route.
  await page.getByRole("button",{name:"安全重试问题处理提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"问题处理提交恢复",exact:true})).toHaveCount(0);
  expect(decisions).toHaveLength(2);expect(decisions[1]).toEqual(decisions[0]);
  const updated=await (await request.get(`/api/issues/${issue!.id}`)).json() as IssueResponse;
  expect(updated.events).toHaveLength(2);expect(updated.state).toBe("proposed");
});

test("a late revision response from an earlier workspace cannot clear or close the newer workspace editor",async({page,request})=>{
  const {research}=await completedResearch(request);
  const health=await (await request.get("/api/health")).json(),nextWorkspace=`${health.workspace_id}-recovery-fence`;
  let switched=false;
  await page.route("**/api/health",async route=>{
    const response=await route.fetch(),body=await response.json();
    if(switched)body.workspace_id=nextWorkspace;
    await route.fulfill({response,json:body});
  });
  await openResearch(page,research);await page.getByRole("button",{name:"创建新版本",exact:true}).click();
  await page.getByLabel("修改说明",{exact:true}).fill("Original workspace submitted revision");
  let release!:()=>void,arrived!:()=>void,finished!:()=>void;
  const held=new Promise<void>(resolve=>{release=resolve;}),arrival=new Promise<void>(resolve=>{arrived=resolve;}),done=new Promise<void>(resolve=>{finished=resolve;});
  let saved:RevisionResponse|undefined;
  await page.route(`**/api/researches/${research.id}/revisions`,async route=>{
    const response=await route.fetch();expect(response.status()).toBe(201);saved=await response.json();arrived();await held;
    try{await route.fulfill({response});}finally{finished();}
  });
  await page.getByRole("button",{name:"保存新版本",exact:true}).click();await arrival;
  const originalPendingKey=await page.evaluate(()=>Object.keys(localStorage).find(key=>key.includes("revision-request"))!);
  await page.evaluate(({workspace,id})=>localStorage.setItem(`paper-alpha:selection:v1:${workspace}`,id),{workspace:nextWorkspace,id:research.id});
  switched=true;
  await page.getByRole("button",{name:"刷新工作台",exact:true}).click();
  await expect(page.getByLabel("研究版本",{exact:true})).toHaveValue(saved!.id);
  await expect(page.getByRole("region",{name:"修订提交恢复",exact:true})).toHaveCount(0);
  await page.getByRole("button",{name:"创建新版本",exact:true}).click();
  await page.getByLabel("修改说明",{exact:true}).fill("New workspace unsent draft must survive");
  release();await done;
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.getByLabel("修改说明",{exact:true})).toHaveValue("New workspace unsent draft must survive");
  await expect(page.getByLabel("研究版本",{exact:true})).toHaveValue(saved!.id);
  expect(await page.evaluate(key=>localStorage.getItem(key),originalPendingKey)).not.toBeNull();
  const drafts=await page.evaluate(workspace=>Object.entries(localStorage).filter(([key])=>key.startsWith(`paper-alpha:draft:v1:${workspace}:revision:`)).map(([,raw])=>JSON.parse(raw)),nextWorkspace);
  expect(drafts.some(item=>item.value.note==="New workspace unsent draft must survive")).toBe(true);
});

test("regression check acknowledgement loss replays the exact run attempt and state digest once",async({page,request})=>{
  const {research,run}=await completedResearch(request),review=await seedReview(request,run);
  const approved=await request.post("/api/regression-cases",{data:{review_id:review.id,expected_status:"evaluated",note:"Automation independent reference",idempotency_key:randomUUID()}});
  expect(approved.status()).toBe(201);const regressionCase=await approved.json();
  await openReview(page,research,run,"alpha101");
  await page.getByLabel("选择回归用例 alpha101",{exact:true}).check();
  const bodies:Record<string,unknown>[]=[];let checked:RegressionCheckResponse|undefined,first=true;
  await page.route("**/api/regression-checks",async route=>{
    if(route.request().method()!=="POST"){await route.continue();return;}
    bodies.push(route.request().postDataJSON());
    if(first){first=false;const response=await route.fetch();expect(response.status()).toBe(201);checked=await response.json();await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button",{name:"执行回归检查",exact:true}).click();
  await expect(page.getByRole("region",{name:"回归检查提交恢复",exact:true})).toContainText("结果待确认");
  await page.reload();await page.getByRole("button",{name:"审核与回归",exact:true}).click();
  await page.getByLabel("选择已结束的实验",{exact:true}).selectOption(run.id);
  await page.getByRole("button",{name:"安全重试回归检查提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"回归检查提交恢复",exact:true})).toHaveCount(0);
  expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);
  expect(bodies[0].case_ids).toEqual([regressionCase.id]);
  expect(bodies[0].expected_attempt_id).toBe(run.regression_target!.attempt_id);
  expect(bodies[0].expected_result_digest).toBe(run.regression_target!.result_digest);
  const checks=await (await request.get("/api/regression-checks")).json();
  expect(checks.filter((item:RegressionCheckResponse)=>item.run_id===run.id)).toHaveLength(1);
  expect(checks.find((item:RegressionCheckResponse)=>item.run_id===run.id).id).toBe(checked!.id);
});

test("unchanged terminal polling uses status only; database changes and explicit verification refresh the full result",async({page,request,clientRecoveryServer})=>{
  const {research,run}=await completedResearch(request);
  let full=0,probes=0,series=0;
  page.on("request",request=>{
    const path=new URL(request.url()).pathname;
    if(path===`/api/runs/${run.id}`&&request.method()==="GET")full++;
    if(path===`/api/runs/${run.id}/status`)probes++;
    if(path===`/api/runs/${run.id}/candidates/alpha101/series`)series++;
  });
  await openReview(page,research,run,"alpha101");
  const charts=page.getByRole("region",{name:"候选结果图表",exact:true});
  await expect(charts.locator("[data-series-candidate]")).toHaveAttribute("data-series-candidate","alpha101");
  await expect(page.getByRole("region",{name:"实验核验状态",exact:true})).toContainText("后台轻量状态检查不会重新核验文件");
  const originalFull=full;
  await expect.poll(()=>probes,{timeout:12500}).toBeGreaterThanOrEqual(2);
  expect(full).toBe(originalFull);
  const review=await seedReview(request,run);
  await expect.poll(()=>full,{timeout:8000}).toBe(originalFull+1);
  await expect(page.getByText(review.note,{exact:true})).toBeVisible();
  const originalSeries=series;
  await page.getByRole("button",{name:"重新核验当前实验",exact:true}).click();
  await expect.poll(()=>full).toBe(originalFull+2);
  await expect.poll(()=>series).toBeGreaterThan(originalSeries);
  const target=run.review_targets.find(item=>item.candidate_id==="alpha101")!;
  const response=await request.get(`/api/runs/${run.id}/candidates/alpha101/series?${new URLSearchParams({attempt_id:target.attempt_id,result_digest:target.result_digest})}`);
  const data=await response.json();
  const path=join(clientRecoveryServer.home,"runs",run.id,"attempts",target.attempt_id,"output",data.source.artifact_name);
  const bytes=readFileSync(path);
  try{
    writeFileSync(path,Buffer.concat([bytes,Buffer.from("\n")]));
    // File corruption is intentionally invisible to the lightweight database probe.
    const probe=await (await request.get(`/api/runs/${run.id}/status`)).json();expect(probe.integrity_checked).toBe(false);
    await page.getByRole("button",{name:"重新核验当前实验",exact:true}).click();
    await expect(page.getByRole("region",{name:"本次候选输出",exact:true})).toContainText("当前输出尚未通过完整性校验或缺少审核目标");
    await expect(charts).toHaveCount(0);
  }finally{writeFileSync(path,bytes);}
});
