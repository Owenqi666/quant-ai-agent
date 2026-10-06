import {test,expect} from "@playwright/test";
import type {APIRequestContext,Page} from "@playwright/test";
import {execFileSync} from "node:child_process";
import {resolve} from "node:path";

async function blockedStudy(request:APIRequestContext, title:string) {
  const scan=JSON.parse(execFileSync(resolve("../.venv/bin/python"),["-c",`import json
from paper_alpha.eligibility import demo_scan
from paper_alpha.storage import digest
value=demo_scan(); value['plan']['minimum_assets']=100; value['plan_digest']=digest(value['plan'])
print(json.dumps(value))`],{cwd:resolve(".."),encoding:"utf8"}));
  const response=await request.post("/api/author-studies",{data:{title,note:"Automated aggregate fixture only; not raw-source verification.",scan,parent_review_id:null,author_panel_ids:[],idempotency_key:crypto.randomUUID()}});
  expect(response.status()).toBe(201); return response.json();
}
async function enter(page:Page){
  await page.goto("/"); await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"研究任务",exact:true}).click();
  await expect(page.getByRole("region",{name:"研究任务入口",exact:true})).toBeVisible();
}
async function choose(page:Page, source:string,title:string){
  const input=page.getByLabel("选择已完成的研究结果",{exact:true});
  if(!await input.isVisible())await page.getByText("从现有结果建立研究任务",{exact:true}).click();
  await input.selectOption(source);
  await expect(page.getByRole("button",{name:"保存研究任务关联",exact:true})).toBeEnabled();
  await page.getByLabel("研究任务名称",{exact:true}).fill(title);
}

test("blocked research displays source output and bounded automation draft, then opens the exact human review target",async({page,request},testInfo)=>{
  const study=await blockedStudy(request,"Case browser source");
  await enter(page);await choose(page,study.id,"Case browser blocked task");
  const saved=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/research-cases"&&r.request().method()==="POST");
  await page.getByRole("button",{name:"保存研究任务关联",exact:true}).click();
  const detail=await(await saved).json();expect(detail.context.state).toBe("data_insufficient");
  const region=page.getByRole("region",{name:"研究任务详情",exact:true});
  await expect(region).toHaveAttribute("data-case-id",detail.id);
  await expect(region).toContainText("数据不足，保留阻断结论");
  const output=page.getByRole("region",{name:"研究实际输出",exact:true});
  await expect(output).toContainText("aggregate consistency only");
  await expect(output).toContainText("execution_ready=false");
  await page.getByText("受限工具验证与建议记录（AI 接口留白）",{exact:true}).click();
  await page.getByRole("button",{name:"开始受限工具会话",exact:true}).click();
  await page.getByRole("button",{name:"核对实际计算结果",exact:true}).click();
  await expect(page.getByRole("region",{name:"工具执行与草稿",exact:true})).toContainText("已用调用 1/8");
  await page.getByLabel("建议的下一步",{exact:true}).selectOption("stop_data_insufficient");
  await page.getByLabel("工具建议依据",{exact:true}).fill("Automated stop draft preserves the predeclared sample gate.");
  await page.getByRole("button",{name:"保存待审核工具草稿",exact:true}).click();
  await expect(page.getByText("自动化草稿 · 语义尚未审核",{exact:true})).toBeVisible();
  const sessions=await(await request.get(`/api/research-tool-sessions?case_id=${detail.id}`)).json();
  const session=await(await request.get(`/api/research-tool-sessions/${sessions.items[0].id}`)).json();
  expect(session.usage.calls).toBe(2);expect(session.calls[1].response.proposal.actor).toBe("automation");
  expect((await(await request.get(`/api/author-studies/${study.id}/reviews`)).json()).items).toEqual([]);
  await page.screenshot({path:testInfo.outputPath("research-case-output-and-draft.png"),fullPage:true});
  await page.getByRole("button",{name:"打开对应结果与人工审核",exact:true}).click();
  await expect(page.getByRole("region",{name:"研究准入详情",exact:true})).toHaveAttribute("data-study-id",study.id);
});

test("case creation and tool-call response loss recover exact receipts after reload",async({page,request})=>{
  const study=await blockedStudy(request,"Case browser recovery source");
  await enter(page);await choose(page,study.id,"Case browser response recovery");
  let loseCase=true;const caseBodies:unknown[]=[];
  await page.route("**/api/research-cases",async route=>{
    if(route.request().method()!=="POST")return route.continue();
    caseBodies.push(route.request().postDataJSON());const response=await route.fetch();
    if(loseCase){loseCase=false;await route.abort();}else await route.fulfill({response});
  });
  await page.getByRole("button",{name:"保存研究任务关联",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试研究任务提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"研究任务",exact:true}).click();
  await page.getByRole("button",{name:"安全重试研究任务提交",exact:true}).click();
  await expect(page.getByRole("heading",{name:"Case browser response recovery",exact:true})).toBeVisible();
  expect(caseBodies).toHaveLength(2);expect(caseBodies[0]).toEqual(caseBodies[1]);
  await page.getByText("受限工具验证与建议记录（AI 接口留白）",{exact:true}).click();
  await page.getByRole("button",{name:"开始受限工具会话",exact:true}).click();
  let loseCall=true;const callBodies:unknown[]=[];let sessionId="";
  await page.route("**/api/research-tool-sessions/*/calls",async route=>{
    callBodies.push(route.request().postDataJSON());sessionId=new URL(route.request().url()).pathname.split("/")[3];
    const response=await route.fetch();if(loseCall){loseCall=false;await route.abort();}else await route.fulfill({response});
  });
  await page.getByRole("button",{name:"核对实际计算结果",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试工具调用提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"研究任务",exact:true}).click();
  await page.getByText("受限工具验证与建议记录（AI 接口留白）",{exact:true}).click();
  await page.getByRole("button",{name:"安全重试工具调用提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"工具执行与草稿",exact:true})).toContainText("已用调用 1/8");
  expect(callBodies).toHaveLength(2);expect(callBodies[0]).toEqual(callBodies[1]);
  const saved=await(await request.get(`/api/research-tool-sessions/${sessionId}`)).json();expect(saved.calls).toHaveLength(1);
});

test("verified daily results bind a case and a late source response does not override newer navigation",async({page,request})=>{
  const example=await(await request.post("/api/examples/alpha101")).json();
  const research=await(await request.get(`/api/researches/${example.research_id}`)).json();
  const submitted=await(await request.post("/api/runs",{data:{revision_id:research.latest_revision_id,mode:"normalized_fixed",idempotency_key:crypto.randomUUID()}})).json();
  const runId=submitted.run_id??submitted.id;
  await expect.poll(async()=>(await(await request.get(`/api/runs/${runId}/status`)).json()).status).toBe("completed");
  const preview=await(await request.get(`/api/research-cases/preview?source_kind=daily_run&source_id=${runId}`)).json();
  const created=await request.post("/api/research-cases",{data:{title:"Daily navigation case",note:"Controlled software example",source_kind:"daily_run",source_id:runId,source_digest:preview.source_digest,idempotency_key:crypto.randomUUID()}});
  expect(created.status()).toBe(201);
  const detail=await created.json();await enter(page);
  await page.getByRole("button",{name:`查看研究任务 ${detail.id}`,exact:true}).click();
  await expect(page.getByRole("region",{name:"研究实际输出",exact:true})).toBeVisible();
  let release:()=>void=()=>{};let entered:()=>void=()=>{};
  const seen=new Promise<void>(resolve=>{entered=resolve;});const held=new Promise<void>(resolve=>{release=resolve;});
  await page.route(`**/api/runs/${runId}`,async route=>{const response=await route.fetch();entered();await held;await route.fulfill({response});});
  await page.getByRole("button",{name:"打开对应结果与人工审核",exact:true}).click();
  await seen;await page.getByRole("button",{name:"月度实验",exact:true}).click();
  const received=page.waitForResponse(r=>new URL(r.url()).pathname===`/api/runs/${runId}`);
  release();await received;
  await expect(page.getByRole("heading",{name:"月度实验",exact:true,level:1})).toBeVisible();
  await expect(page.getByRole("button",{name:"月度实验",exact:true})).toHaveAttribute("aria-current","page");
});
