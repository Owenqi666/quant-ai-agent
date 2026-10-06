import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { ResearchDetail } from "../src/generated/api-contract";

// Creates only isolated research setup. Guided human declarations stop at read-only validation,
// or an intercepted/aborted POST; no synthetic browser activity becomes a saved human observation.
async function research(request:APIRequestContext){
  const example=await (await request.post("/api/examples/alpha101",{data:{}})).json();
  const original=await (await request.get(`/api/researches/${example.research_id}`)).json() as ResearchDetail;
  const file=JSON.parse(await readFile("../evaluation_suites/v05/task.json","utf8"));
  const task=Object.fromEntries(["evidence","hypotheses","candidates","evaluation","budget"].map(k=>[k,file[k]]));
  const response=await request.post("/api/researches",{data:{title:`Guided UI fixture ${randomUUID()}`,paper_id:original.paper_id,dataset_id:original.dataset_id,task,idempotency_key:randomUUID()}});
  expect(response.status()).toBe(201);return await response.json() as ResearchDetail;
}
async function setup(page:Page,r:ResearchDetail,condition="manual_cli"){
  await page.goto("/");await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:r.title,exact:true}).click();
  await page.getByRole("button",{name:"流程观测",exact:true}).click();
  await expect(page.getByRole("region",{name:"引导流程记录",exact:true})).toBeVisible();
  await expect(page.getByLabel("练习记录",{exact:true})).toBeChecked();
  await page.getByLabel("参与者标识",{exact:true}).fill(`browser-fixture-${randomUUID()}`);
  await page.getByLabel("本次路径",{exact:true}).selectOption(condition);
  await page.getByLabel("对材料与工具的熟悉程度",{exact:true}).fill("Automated browser fixture; not an actual person's observation.");
  await page.getByText("版本、环境和允许工具（首次记录需确认）",{exact:true}).click();
  await expect(page.getByLabel("机器说明",{exact:true})).not.toHaveValue("");
  await page.getByLabel("执行代码的提交标识",{exact:true}).fill("a".repeat(40));
  await page.getByRole("button",{name:"开始本次记录",exact:true}).click();
  await expect(page.getByRole("button",{name:"结束本次记录",exact:true})).toBeVisible();
}
async function draft(page:Page,researchId:string){return await page.evaluate(id=>{
  const key=Object.keys(localStorage).find(k=>k.includes('"guided-observation"')&&k.includes(id));
  if(!key)throw new Error("Expected durable guided draft");return JSON.parse(localStorage.getItem(key)!).draft;
},researchId);}
async function prepareIncomplete(page:Page){
  await page.getByRole("button",{name:"结束本次记录",exact:true}).click();
  await page.getByLabel("未完成或放弃原因",{exact:true}).fill("Automated UI acceptance stops early; no real human observation is being saved.");
  await page.getByRole("button",{name:"检查并准备保存",exact:true}).click();
  await expect(page.getByRole("heading",{name:"预检通过，尚未保存",exact:true})).toBeVisible();
}

test("basic form prepares a partial observation without JSON, IDs or fabricated missing phases",async({page,request},testInfo)=>{
  const r=await research(request);await setup(page,r);
  await expect(page.getByLabel("观测 JSON",{exact:true})).not.toBeVisible();
  await page.getByRole("button",{name:"假设",exact:true}).click();
  await page.getByRole("button",{name:"实现",exact:true}).click();
  await page.getByRole("button",{name:"暂停流程计时",exact:true}).click();
  const recorded=await draft(page,r.id);expect(recorded.session.intervals).toHaveLength(3);
  expect(recorded.session.intervals.map((i:{phase:string})=>i.phase)).toEqual(["reading","hypothesis","implementation"]);
  let validated:Record<string,unknown>|undefined;
  page.on("request",req=>{if(req.method()==="POST"&&req.url().endsWith("/workflow-observations/validate"))validated=req.postDataJSON().observation;});
  await prepareIncomplete(page);
  expect(validated).toMatchObject({source:"human",practice_session:true,completion:"incomplete",dimensions:null,bound_outputs:{revision_id:null,run_id:null,attempt_id:null,review_ids:[]}});
  expect(validated?.session_id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByRole("region",{name:"观测预检结果",exact:true})).toContainText("六阶段完整主动耗时：未测量");
  await expect(page.getByRole("button",{name:"确认导入不可变观测",exact:true})).toBeDisabled();
  expect((await (await request.get(`/api/researches/${r.id}/workflow-observations`)).json()).total).toBe(0);
  await page.screenshot({path:testInfo.outputPath("guided-partial-preflight-no-human-record.png"),fullPage:true});
});

test("cross-page timer survives navigation; paused refresh resumes and active refresh preserves an unknown interruption",async({page,request})=>{
  const r=await research(request);await setup(page,r,"workbench");
  await page.getByRole("button",{name:"研究工作台",exact:true}).click();
  const compact=page.getByRole("region",{name:"正在记录流程",exact:true});await expect(compact).toBeVisible();
  await compact.getByLabel("当前记录阶段",{exact:true}).selectOption("hypothesis");
  await compact.getByRole("button",{name:"等待工具",exact:true}).click();
  await compact.getByRole("button",{name:"暂停流程计时",exact:true}).click();
  const paused=await draft(page,r.id);expect(paused.session.open).toBeNull();
  await page.reload();await expect(compact).toContainText("已暂停");
  expect((await draft(page,r.id)).session.intervals).toEqual(paused.session.intervals);
  await compact.getByRole("button",{name:"开始主动计时",exact:true}).click();
  const running=await draft(page,r.id);expect(running.session.open.phase).toBe("hypothesis");
  await page.reload();await expect(compact).toContainText("已中断");
  const interrupted=await draft(page,r.id);expect(interrupted.session.intervals.at(-1)).toMatchObject({status:"interrupted",ended_at:null,started_at:running.session.open.startedAt});
  await expect(compact.getByRole("button",{name:"开始主动计时",exact:true})).toBeDisabled();
  await compact.getByRole("button",{name:"返回流程记录",exact:true}).click();
  await prepareIncomplete(page);
  expect((await (await request.get(`/api/researches/${r.id}/workflow-observations`)).json()).total).toBe(0);
});

test("interrupted submission durably keeps its original guided snapshot and request across reload",async({page,request})=>{
  const r=await research(request),path=`/api/researches/${r.id}/workflow-observations`;await setup(page,r);await prepareIncomplete(page);
  const bodies:unknown[]=[];
  await page.route(`**${path}`,async route=>{if(route.request().method()!=="POST"){await route.continue();return;}bodies.push(route.request().postDataJSON());await route.abort("failed");});
  await page.getByLabel("确认来源与不可变保存",{exact:true}).check();await page.getByRole("button",{name:"确认导入不可变观测",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试观测导入",exact:true})).toBeVisible();
  const pending=await page.evaluate(id=>{const key=Object.keys(localStorage).find(k=>k.includes('"workflow-observation-import"')&&k.includes(id));return JSON.parse(localStorage.getItem(key!)!);},r.id);
  expect(JSON.parse(pending.context.guidedSnapshot)).toEqual(await draft(page,r.id));
  await page.reload();await page.getByRole("button",{name:"流程观测",exact:true}).click();
  await page.getByRole("button",{name:"安全重试观测导入",exact:true}).click();
  expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);
  expect((await (await request.get(path)).json()).total).toBe(0);
});

test("newer edits invalidate prepared output and preserve the durable guide when the older JSON is inspected",async({page,request})=>{
  const r=await research(request);await setup(page,r);await prepareIncomplete(page);
  const before=await draft(page,r.id);
  await page.getByLabel("未完成或放弃原因",{exact:true}).fill("Newer unsent edits kept separate from original serialized preflight.");
  await expect(page.getByRole("region",{name:"观测预检结果",exact:true})).toHaveCount(0);
  await page.getByText("高级：JSON 导入与原始模板",{exact:true}).click();
  const old=JSON.parse(await page.getByLabel("观测 JSON",{exact:true}).inputValue());expect(old.incomplete_reason).toBe(before.reason);
  await page.getByRole("button",{name:"预检观测记录",exact:true}).click();
  await expect(page.getByRole("region",{name:"观测预检结果",exact:true})).toBeVisible();
  expect((await draft(page,r.id)).reason).toBe("Newer unsent edits kept separate from original serialized preflight.");
  expect((await (await request.get(`/api/researches/${r.id}/workflow-observations`)).json()).total).toBe(0);
});
