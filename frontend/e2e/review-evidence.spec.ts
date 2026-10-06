import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ArtifactResponse, ResearchDetail, RunDetail } from "../src/generated/api-contract";

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
const test = base.extend<{}, { reviewEvidenceServer: string }>({
  reviewEvidenceServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-review-evidence-e2e-"));
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
          if (child.exitCode !== null) throw new Error(`Owned review evidence server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned review evidence server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `review-evidence-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned review evidence launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ reviewEvidenceServer }, use) => use(reviewEvidenceServer),
});

async function completedResearch(request: APIRequestContext) {
  const exampleResponse = await request.post("/api/examples/alpha101", {data:{}});
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const original = await (await request.get(`/api/researches/${example.research_id}`)).json() as ResearchDetail;
  const create = await request.post("/api/researches", {data:{title:`Review evidence automation ${randomUUID()}`,
    paper_id:original.paper_id,dataset_id:original.dataset_id,task:original.revisions[0].task,idempotency_key:randomUUID()}});
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

// Assert against actual engine values and the UI's declared display precision.
// The downloaded JSON below must also match the unrounded saved metrics exactly.
async function assertMetrics(page: Page, run: RunDetail, candidateId: string) {
  const panel=page.getByRole("region",{name:"本次候选输出",exact:true});
  const metrics=run.state!.candidates.find(c=>c.id===candidateId)!.result!.metrics;
  const expected:Record<string,string>={
    "平均 Rank IC":metrics.mean_rank_ic===null?"未计算":Number(metrics.mean_rank_ic).toFixed(6),
    "平均日毛收益":metrics.mean_gross_return===null?"未计算":`${(Number(metrics.mean_gross_return)*100).toFixed(2)}%`,
    "有效评估天数":String(metrics.evaluated_days),
    "因子覆盖率":metrics.factor_coverage===null?"未计算":`${(Number(metrics.factor_coverage)*100).toFixed(2)}%`,
  };
  for(const [label,value] of Object.entries(expected)) {
    const row=panel.getByRole("table",{name:"候选计算指标",exact:true}).getByRole("row").filter({has:page.getByText(label,{exact:true})});
    await expect(row).toBeVisible();
    await expect(row.getByRole("cell").nth(1)).toHaveText(value);
  }
}

async function downloadArtifact(page: Page, run: RunDetail, artifacts:ArtifactResponse[], candidateId:string,
  name:"下载因子 CSV"|"下载计算结果 JSON"|"下载本次实验报告", suffix:string) {
  const target=run.review_targets.find(t=>t.candidate_id===candidateId)!;
  const expected=artifacts.find(a=>a.attempt_id===target.attempt_id&&
    (suffix==="report.md"?a.name==="report.md":a.name.startsWith(`candidates/${candidateId}/`)&&a.name.endsWith(suffix)));
  expect(expected).toBeDefined();
  const link=page.getByRole("region",{name:"本次候选输出",exact:true}).getByRole("link",{name,exact:true});
  await expect(link).toHaveAttribute("href",`/api/runs/${run.id}/artifacts/${expected!.id}`);
  const downloaded=page.waitForEvent("download"); await link.click();
  const path=await (await downloaded).path(); expect(path).not.toBeNull();
  const bytes=readFileSync(path!);
  // ArtifactResponse.sha256 identifies the exported bytes, including path redaction, not the raw engine file.
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(expected!.sha256);
  return bytes.toString("utf8");
}

test("selected candidate shows verified metrics and paper evidence with exact attempt-bound downloads on the review page",async({page,request},testInfo)=>{
  const {research,run,artifacts}=await completedResearch(request);
  const panel=await openReview(page,research,run);
  for(const id of ["alpha006","alpha101"]){
    await page.getByLabel("审核候选",{exact:true}).selectOption(id);
    const candidate=run.state!.candidates.find(c=>c.id===id)!;
    const frozen=research.revisions[0].task.candidates.find(c=>c.id===id)!;
    const hypothesis=research.revisions[0].task.hypotheses.find(h=>h.id===frozen.hypothesis_id)!;
    const evidence=research.revisions[0].task.evidence.find(e=>hypothesis.evidence_ids.includes(e.id))!;
    await expect(panel.locator("code.expression").nth(0)).toHaveText(frozen.expression);
    await expect(panel.locator("code.expression").nth(1)).toHaveText(String(candidate.result!.expression));
    await expect(panel.getByText("来源：论文原文",{exact:true})).toBeVisible();
    await expect(panel.getByText("经济解释（模型推测）：",{exact:true})).toBeVisible();
    await expect(panel).toContainText(evidence.quote);
    await expect(panel).toContainText(hypothesis.claim);
    await expect(panel).toContainText(hypothesis.economic_mechanism);
    await expect(panel.getByRole("link").filter({hasText:new RegExp(String(evidence.page))})).toHaveAttribute("href",`/api/papers/${research.paper_id}/pdf#page=${evidence.page}`);
    await assertMetrics(page,run,id);
    const csv=await downloadArtifact(page,run,artifacts,id,"下载因子 CSV","/factor.csv");
    expect(csv.split("\n")[0]).toMatch(/^date,/);
    const result=JSON.parse(await downloadArtifact(page,run,artifacts,id,"下载计算结果 JSON","/result.json"));
    expect(result.expression).toBe(candidate.result!.expression);
    expect(result.metrics).toEqual(candidate.result!.metrics);
    const report=await downloadArtifact(page,run,artifacts,id,"下载本次实验报告","report.md");
    expect(report).toContain(`### \`${id}\``);
    expect(report).toContain(frozen.expression);
    const other=run.state!.candidates.find(c=>c.id===(id==="alpha006"?"alpha101":"alpha006"))!;
    await expect(panel).not.toContainText(String(other.result!.expression));
  }
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(0);
  await page.screenshot({path:testInfo.outputPath("same-page-candidate-output-and-evidence.png"),fullPage:true});
});

test("new research revision cannot replace an older run's frozen expression, paper quote or hypothesis in review",async({page,request},testInfo)=>{
  const {research,run}=await completedResearch(request);
  const old=research.revisions[0].task;
  const task=structuredClone(old), candidate=task.candidates.find(c=>c.id==="alpha006")!;
  const hypothesis=task.hypotheses.find(h=>h.id===candidate.hypothesis_id)!;
  const evidence=task.evidence.find(e=>hypothesis.evidence_ids.includes(e.id))!;
  candidate.expression="rank(close)";candidate.origin="user_modification";candidate.changes=["Automation revision: deliberately replace formula for frozen-output regression"];
  hypothesis.claim="AUTOMATION REVISION TWO CLAIM MUST NOT LEAK INTO OLD OUTPUT";
  hypothesis.attribution="user_modification";
  hypothesis.economic_mechanism="AUTOMATION REVISION TWO MECHANISM";
  hypothesis.mechanism_attribution="user_modification";hypothesis.required_fields=["close"];
  evidence.quote=task.evidence.find(e=>e.id==="alpha101-formula")!.quote;
  evidence.page=15;
  const updated=await request.post(`/api/researches/${research.id}/revisions`,{data:{base_revision_id:research.latest_revision_id,task,note:"Automation: verify immutable old run review context"}});
  expect(updated.status()).toBe(201); const revision=await updated.json();
  const panel=await openReview(page,research,run);
  await expect(page.getByLabel("研究版本",{exact:true})).toHaveValue(revision.id);
  const original=old.candidates.find(c=>c.id==="alpha006")!;
  const originalHypothesis=old.hypotheses.find(h=>h.id===original.hypothesis_id)!;
  await expect(panel).toContainText(original.expression);
  await expect(panel).toContainText(originalHypothesis.claim);
  await expect(panel).toContainText(originalHypothesis.economic_mechanism);
  await expect(panel).not.toContainText(candidate.expression);
  await expect(panel).not.toContainText(hypothesis.claim);
  await expect(panel).not.toContainText(hypothesis.economic_mechanism);
  await expect(panel).not.toContainText(evidence.quote);
  await assertMetrics(page,run,"alpha006");
  await page.screenshot({path:testInfo.outputPath("old-run-evidence-after-new-revision.png"),fullPage:true});
});

test("unverified output or a missing candidate review target hides metrics and verified downloads",async({page,request},testInfo)=>{
  const {research,run}=await completedResearch(request);
  const panel=await openReview(page,research,run,"alpha101");
  await assertMetrics(page,run,"alpha101");
  let fault:"verification"|"target"|null=null;
  await page.route(`**/api/runs/${run.id}`,async route=>{
    const response=await route.fetch();const body=await response.json() as RunDetail;
    if(fault==="verification")body.verification={...body.verification!,verified:false};
    else if(fault==="target") body.review_targets=body.review_targets.filter(t=>t.candidate_id!=="alpha101");
    await route.fulfill({response,json:body});
  });
  for(const phase of ["verification","target"] as const){
    fault=phase;
    const refreshed=page.waitForResponse(response=>new URL(response.url()).pathname===`/api/runs/${run.id}`&&response.request().method()==="GET");
    await page.getByRole("button",{name:"刷新工作台",exact:true}).click();
    const shown=await (await refreshed).json() as RunDetail;
    if(phase==="verification")expect(shown.verification?.verified).toBe(false);
    else {expect(shown.verification?.verified).toBe(true);expect(shown.review_targets.some(t=>t.candidate_id==="alpha101")).toBe(false);}
    await expect(panel).toContainText("当前输出尚未通过完整性校验或缺少审核目标");
    for(const label of ["平均 Rank IC","平均日毛收益","有效评估天数","因子覆盖率"])
      await expect(panel.getByText(label,{exact:true})).toHaveCount(0);
    for(const name of ["下载因子 CSV","下载计算结果 JSON","下载本次实验报告"])
      await expect(panel.getByRole("link",{name,exact:true})).toHaveCount(0);
    await testInfo.attach(`blocked-${phase}`,{body:await panel.innerText(),contentType:"text/plain"});
    await page.screenshot({path:testInfo.outputPath(`unverified-review-output-${phase}.png`),fullPage:true});
    fault=null;
    await page.getByRole("button",{name:"刷新工作台",exact:true}).click();
    await assertMetrics(page,run,"alpha101");
  }
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(0);
});
