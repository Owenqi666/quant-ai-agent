import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { ObservationDetail, ObservationTemplate, ObservationSummary, ResearchDetail } from "../src/generated/api-contract";
import { assertApiResponse } from "../src/generated/api-contract";

// Uses the configured isolated Playwright workspace. These cases create no runs,
// reviews or regression cases, preserving the existing workflow's global counts.
async function createResearch(request: APIRequestContext) {
  const exampleResponse = await request.post("/api/examples/alpha101", {data:{}});
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const original = await (await request.get(`/api/researches/${example.research_id}`)).json() as ResearchDetail;
  const created = await request.post("/api/researches", {data:{
    title:`Observation automation ${randomUUID()}`, paper_id:original.paper_id, dataset_id:original.dataset_id,
    task:original.revisions[0].task, idempotency_key:randomUUID(),
  }});
  expect(created.status()).toBe(201);
  return await created.json() as ResearchDetail;
}
async function openObservations(page: Page, research: ResearchDetail) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", {exact:true})).toBeVisible();
  await page.getByRole("button", {name:research.title, exact:true}).click();
  await page.getByRole("button", {name:"流程观测", exact:true}).click();
  await expect(page.getByRole("region", {name:"流程观测导入", exact:true})).toBeVisible();
  await page.getByText("高级：JSON 导入与原始模板", {exact:true}).click();
}
async function automationRecord(request: APIRequestContext) {
  const response = await request.get("/api/workflow-observation-template");
  expect(response.status()).toBe(200);
  const template = await response.json() as ObservationTemplate;
  assertApiResponse("/api/workflow-observation-template", "GET", 200, template);
  return {...template.observation, record_status:"observed", source:"automation",
    participant:`browser-fixture-${randomUUID()}`, session_id:randomUUID(), condition:"manual_cli", execution_order:1,
    code_commit:"a".repeat(40), environment:{machine:"automation fixture",os:"fixture",python:"fixture",dependencies:"fixture; not a real environment claim"},
    allowed_tools:["automation-only fixture"], prior_familiarity:"Synthetic browser acceptance, not a person's observation", practice_session:false,
    predeclared_stop_condition:"Stop after verifying immutable import behavior", started_at:"2026-01-01T00:00:00Z", finished_at:"2026-01-01T00:02:00Z",
    completion:"incomplete", incomplete_reason:"Automation fixture intentionally records only one phase; not human research",
    intervals:[{phase:"reading",kind:"active",started_at:"2026-01-01T00:00:00Z",ended_at:"2026-01-01T00:01:00Z",status:"ended",source_reference:null}],
    limitations:["Automation-only software fixture. All times and declarations are synthetic, excluded from human metrics."],
  };
}
async function enterAndValidate(page: Page, observation: Record<string,unknown>) {
  await page.getByLabel("观测 JSON", {exact:true}).fill(JSON.stringify(observation,null,2));
  await page.getByLabel("观测声明来源", {exact:true}).selectOption("automation");
  await page.getByRole("button", {name:"预检观测记录", exact:true}).click();
  await expect(page.getByRole("heading", {name:"预检通过，尚未保存", exact:true})).toBeVisible();
  await page.getByLabel("确认来源与不可变保存", {exact:true}).check();
}

test("blank template is rejected; automation import preserves partial timing and stays outside human metrics", async ({page,request}, testInfo) => {
  const research = await createResearch(request), path = `/api/researches/${research.id}/workflow-observations`;
  await openObservations(page,research);
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", {name:"下载空白观测模板", exact:true}).click();
  const downloaded = await downloadPromise;
  const blankPath = await downloaded.path(); expect(blankPath).not.toBeNull();
  const blankText = await readFile(blankPath!,"utf8"), blank = JSON.parse(blankText);
  expect(blank.record_status).toBe("pending_human_observation"); expect(blank.source).toBeNull();
  await page.getByLabel("上传观测 JSON", {exact:true}).setInputFiles({name:"blank.json",mimeType:"application/json",buffer:Buffer.from(blankText)});
  await expect(page.getByLabel("观测声明来源", {exact:true})).toHaveValue("");
  await page.getByLabel("观测声明来源", {exact:true}).selectOption("automation");
  await page.getByRole("button", {name:"预检观测记录", exact:true}).click();
  await expect(page.getByRole("alert").filter({hasText:"source 与当前声明不一致"})).toBeVisible();
  expect((await (await request.get(path)).json()).total).toBe(0);

  const observation = await automationRecord(request);
  await enterAndValidate(page,observation);
  const preflight = page.getByRole("region", {name:"观测预检结果", exact:true});
  await expect(preflight).toContainText("主动工作合计：60.00 秒");
  await expect(preflight).toContainText("六阶段完整主动耗时：未测量");
  await page.getByRole("button", {name:"确认导入不可变观测", exact:true}).click();
  await expect(page.getByText(/观测已保存为不可变记录/)).toBeVisible();
  const list = await (await request.get(path)).json(); expect(list.total).toBe(1);
  const saved = list.items[0] as ObservationDetail;
  expect(saved.observation.source).toBe("automation"); expect(saved.timing.full_active_seconds).toBeNull();
  expect(saved.timing.observed_active_seconds).toBe(60); expect(saved.timing.phases.filter((phase)=>phase.active_seconds===null)).toHaveLength(5);
  const detail = page.getByRole("article", {name:"不可变观测详情", exact:true});
  await expect(detail).toContainText(saved.payload_digest);
  const exported = await request.get(`${path}/${saved.id}/export`);
  expect(exported.status()).toBe(200); expect(await exported.json()).toEqual(saved);
  await expect(detail.getByRole("link", {name:`导出观测 JSON ${saved.id}`, exact:true})).toHaveAttribute("href",`${path}/${saved.id}/export`);
  await page.getByRole("button", {name:"计算观测汇总", exact:true}).click();
  const summaryElement = page.locator('[aria-label="服务计算观测汇总"]');
  await expect(summaryElement).toContainText("非练习人工声明记录：0；已完成：0；完成率：无适用样本");
  await expect(summaryElement).toContainText("自动化记录：1");
  const summary = await (await request.get(`${path}/summary`)).json() as ObservationSummary;
  assertApiResponse(`${path}/summary`,"GET",200,summary);
  expect(summary.human_completion_rate).toBeNull(); expect(summary.human_nonpractice_records).toBe(0);
  expect(summary.comparisons.every((item)=>!item.comparable && item.active_seconds_delta===null)).toBe(true);
  const summaryDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", {name:"下载当前服务汇总 JSON", exact:true}).click();
  const summaryFile = await (await summaryDownloadPromise).path();
  const snapshot = JSON.parse(await readFile(summaryFile!,"utf8"));
  expect(snapshot.observations[0].payload_digest).toBe(saved.payload_digest);
  expect(snapshot.automation_records).toBe(1); expect(snapshot.human_completion_rate).toBeNull();
  await page.screenshot({path:testInfo.outputPath("automation-observation-with-unmeasured-phases.png"),fullPage:true});
});

test("lost committed response replays the original observation after reload and survives list refresh failure", async ({page,request},testInfo) => {
  const research = await createResearch(request), path = `/api/researches/${research.id}/workflow-observations`;
  await openObservations(page,research); await enterAndValidate(page,await automationRecord(request));
  const bodies: unknown[] = []; let committed: ObservationDetail | undefined;
  await page.route(`**${path}`,async (route)=>{
    if (route.request().method()!=="POST") {await route.continue();return;}
    bodies.push(route.request().postDataJSON());
    if (bodies.length===1) {const response=await route.fetch();expect(response.status()).toBe(201);committed=await response.json();await route.abort("failed");}
    else await route.continue();
  });
  await page.getByRole("button", {name:"确认导入不可变观测", exact:true}).click();
  await expect.poll(()=>committed?.id||"").not.toBe("");
  await expect(page.getByRole("region", {name:"观测提交恢复", exact:true})).toContainText("待确认");
  await expect(page.getByLabel("观测 JSON",{exact:true})).toBeDisabled();
  const before = await (await request.get(`${path}/${committed!.id}/export`)).text();
  await page.reload();
  await page.getByRole("button", {name:"流程观测", exact:true}).click();
  await expect(page.getByRole("row").filter({hasText:committed!.id})).toBeVisible();
  await expect(page.getByRole("button", {name:"安全重试观测导入", exact:true})).toBeVisible();
  let failedList=false;
  await page.route(`**${path}?*`, async (route)=>{
    if (!failedList) {failedList=true;await route.fulfill({status:500,contentType:"application/json",body:JSON.stringify({detail:"Injected list failure after committed observation replay"})});}
    else await route.continue();
  });
  await page.getByRole("button", {name:"安全重试观测导入", exact:true}).click();
  await expect(page.getByText(/观测已保存，列表刷新暂未成功/)).toBeVisible();
  expect(failedList).toBe(true); expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  expect((await (await request.get(path)).json()).total).toBe(1);
  await expect(page.getByRole("article", {name:"不可变观测详情", exact:true})).toContainText(committed!.id);
  await expect(page.getByRole("button", {name:"安全重试观测导入", exact:true})).toHaveCount(0);
  await expect(page.getByLabel("观测 JSON", {exact:true})).toHaveValue("");
  expect(await (await request.get(`${path}/${committed!.id}/export`)).text()).toBe(before);
  await page.screenshot({path:testInfo.outputPath("observation-confirmed-after-response-loss.png"),fullPage:true});
});

test("late validation cannot contaminate another research or revive after switching back", async ({page,request})=>{
  const first=await createResearch(request), second=await createResearch(request), observation=await automationRecord(request);
  await openObservations(page,first);
  await page.getByLabel("观测 JSON", {exact:true}).fill(JSON.stringify(observation));
  await page.getByLabel("观测声明来源", {exact:true}).selectOption("automation");
  let release!: ()=>void, captured!:()=>void, finished!:()=>void;
  const gate=new Promise<void>((resolve)=>{release=resolve;}), started=new Promise<void>((resolve)=>{captured=resolve;}), routed=new Promise<void>((resolve)=>{finished=resolve;});
  await page.route(`**/api/researches/${first.id}/workflow-observations/validate`,async(route)=>{
    const response=await route.fetch(); captured(); await gate; await route.fulfill({response}); finished();
  });
  await page.getByRole("button", {name:"预检观测记录", exact:true}).click(); await started;
  try {
    await page.getByRole("button", {name:second.title, exact:true}).click();
    await page.getByRole("button", {name:"流程观测", exact:true}).click();
    await expect(page.getByLabel("观测 JSON", {exact:true})).toHaveValue("");
    await page.getByRole("button", {name:first.title, exact:true}).click();
    await page.getByRole("button", {name:"流程观测", exact:true}).click();
    await expect(page.getByLabel("观测 JSON", {exact:true})).toHaveValue(JSON.stringify(observation));
  } finally {release();}
  await routed;
  await expect(page.getByRole("region", {name:"观测预检结果", exact:true})).toHaveCount(0);
  await page.getByText("高级：JSON 导入与原始模板", {exact:true}).click();
  await expect(page.getByRole("button", {name:"预检观测记录", exact:true})).toBeEnabled();
});

test("unavailable durable request storage prevents sending an observation", async({page,request})=>{
  const research=await createResearch(request), path=`/api/researches/${research.id}/workflow-observations`;
  await openObservations(page,research); await enterAndValidate(page,await automationRecord(request));
  await page.evaluate(()=>{
    const original=Storage.prototype.setItem;
    Storage.prototype.setItem=function(key,value){
      if(key.includes("workflow-observation-import")) throw new DOMException("Injected quota error","QuotaExceededError");
      original.call(this,key,value);
    };
  });
  let sent=0;
  page.on("request",(req)=>{if(req.method()==="POST" && new URL(req.url()).pathname===path) sent++;});
  await page.getByRole("button", {name:"确认导入不可变观测", exact:true}).click();
  await expect(page.getByRole("alert").filter({hasText:"无法持久保存请求，尚未发送"})).toBeVisible();
  expect(sent).toBe(0); expect((await (await request.get(path)).json()).total).toBe(0);
  await expect(page.getByLabel("观测 JSON", {exact:true})).not.toHaveValue("");
});
