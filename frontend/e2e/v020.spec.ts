import {test,expect} from "@playwright/test";
import {readFileSync} from "node:fs";
const labels=["引用与页码准确","假设忠于所引原文","经济解释归因恰当","字段含义与替代说明","实现与声明公式一致"];

test("pending reference freezes once after response loss and exports an exact unscored bundle",async({page,request})=>{
  await page.goto("/");await page.getByRole("button",{name:"语义评测",exact:true}).click();
  const region=page.getByRole("region",{name:"版本化评测参考集",exact:true});
  await region.getByText("选择材料并冻结参考集",{exact:true}).click();
  await region.getByRole("button",{name:"预览本次参考快照",exact:true}).click();
  await expect(region.getByText(/共 35 个维度/)).toBeVisible();
  const bodies:unknown[]=[];let lose=true;
  await page.route("**/api/semantic-evaluation-sets",async route=>{if(route.request().method()!=="POST")return route.continue();bodies.push(route.request().postDataJSON());const response=await route.fetch();if(lose){lose=false;await route.abort();}else await route.fulfill({response});});
  await region.getByRole("button",{name:"冻结本次参考版本",exact:true}).click();
  await expect(region.getByRole("button",{name:"安全重试参考集冻结提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"语义评测",exact:true}).click();
  await region.getByRole("button",{name:"安全重试参考集冻结提交",exact:true}).click();
  const detail=page.getByRole("region",{name:"评测参考版本详情",exact:true});await expect(detail).toBeVisible();
  expect(bodies).toHaveLength(2);expect(bodies[0]).toEqual(bodies[1]);
  const id=await detail.getAttribute("data-set-id"),bundle=await(await request.get(`/api/semantic-evaluation-sets/${id}/export`)).json();
  expect(bundle.reference.summary.active_human_records).toBe(0);expect(bundle.reference.semantic_quality_score).toBeNull();expect(bundle.reference.summary.pending_dimensions).toBe(35);expect(bundle.binary_sources_included).toBe(false);
});

test("exact claim review displays real counts and recovers automation without approving text",async({page,request})=>{
  const scan=JSON.parse(readFileSync("../evaluation_suites/v017/blocked_scan.json","utf8"));
  const study=await(await request.post("/api/author-studies",{data:{title:"v020 browser count fixture",note:"Synthetic aggregate, no human review",scan,parent_review_id:null,author_panel_ids:[],idempotency_key:"v020-browser-study"}})).json();
  const context=await(await request.get(`/api/research-cases/preview?source_kind=author_study&source_id=${study.id}`)).json();
  const caseResponse=await request.post("/api/research-cases",{data:{title:"v020 browser exact claim",note:"Synthetic counts",source_kind:"author_study",source_id:study.id,source_digest:context.source_digest,idempotency_key:"v020-browser-case"}});expect(caseResponse.status()).toBe(201);const task=await caseResponse.json(),result=task.context.results[0];
  const batch=await(await request.post("/api/research-claims",{data:{case_id:task.id,case_digest:task.digest,claims:[{id:"count",kind:"metric",attribution:"project_convention",text:"Untrusted prose falsely claims 999999 assets",evidence_ids:[],metric_references:[{case_id:task.id,case_digest:task.digest,result_id:result.id,result_digest:result.digest,pointer:"/summary/min_selected"}]}],idempotency_key:"v020-browser-claims"}})).json();
  await page.goto("/");await page.getByRole("button",{name:"研究任务",exact:true}).click();
  await page.getByRole("button",{name:`查看研究任务 ${task.id}`,exact:true}).click();
  await page.getByText("带实际数值引用的结论草稿",{exact:true}).click();
  await expect(page.getByLabel("指标 JSON pointer",{exact:true})).toHaveValue("/summary/min_selected");
  await page.getByRole("button",{name:`查看结论草稿 ${batch.created_at}`,exact:true}).click();
  const region=page.getByRole("region",{name:"精确结论审核",exact:true});await expect(region.getByText(/^本次待审核文字：Untrusted prose falsely claims 999999 assets$/)).toBeVisible();
  await expect(region.getByRole("region",{name:"本版本原始审核资料",exact:true}).getByRole("status")).toContainText("原始资料暂不可用");
  await region.getByText("填写本版本的五维判断",{exact:true}).click();
  await expect(region.getByLabel("结论审核声明来源",{exact:true})).toHaveValue("");
  for(const label of labels)await expect(region.getByLabel(`结论${label}判断`,{exact:true})).toHaveValue("");
  await region.getByLabel("结论审核声明来源",{exact:true}).selectOption("automation");await region.getByLabel("结论审核者标识",{exact:true}).fill("Browser automation, no human semantic approval");await region.getByRole("button",{name:"记录结论确认时间",exact:true}).click();
  for(const label of labels){await region.getByLabel(`结论${label}判断`,{exact:true}).selectOption("not_assessed");await region.getByLabel(`结论${label}理由`,{exact:true}).fill("Recovery fixture only, no semantic judgment");}
  await region.getByRole("button",{name:"预检本版本结论审核",exact:true}).click();await expect(region.getByRole("button",{name:"保存本版本结论审核",exact:true})).toBeEnabled();
  const bodies:unknown[]=[];let lose=true;
  await page.route("**/api/claim-reviews",async route=>{if(route.request().method()!=="POST")return route.continue();bodies.push(route.request().postDataJSON());const response=await route.fetch();if(lose){lose=false;await route.abort();}else await route.fulfill({response});});
  await region.getByRole("button",{name:"保存本版本结论审核",exact:true}).click();await region.getByRole("button",{name:"安全重试结论审核提交",exact:true}).click();
  await expect(region.getByRole("status").filter({hasText:"已保存结论审核"})).toBeVisible();expect(bodies).toHaveLength(2);expect(bodies[0]).toEqual(bodies[1]);
  const status=await(await request.get(`/api/claim-reviews/status?claims_id=${batch.id}&claim_id=count`)).json();expect(status.human_records).toBe(0);expect(status.human_declared_status).toBe("pending");
  expect((await(await request.get(`/api/research-claims/${batch.id}`)).json()).claims[0].semantic_fidelity).toBe("unverified");
});

test("changing reference selection invalidates delayed preview",async({page})=>{
  await page.goto("/");await page.getByRole("button",{name:"语义评测",exact:true}).click();const region=page.getByRole("region",{name:"版本化评测参考集",exact:true});await region.getByText("选择材料并冻结参考集",{exact:true}).click();
  let arrive!:()=>void,release!:()=>void;const arrived=new Promise<void>(r=>{arrive=r;}),gate=new Promise<void>(r=>{release=r;});
  await page.route("**/api/semantic-evaluation-sets/preview",async route=>{const response=await route.fetch();arrive();await gate;await route.fulfill({response});});
  await region.getByRole("button",{name:"预览本次参考快照",exact:true}).click();await arrived;
  // Inputs are frozen during preflight. Navigating away discards the pending selection safely.
  await page.getByRole("button",{name:"研究执行",exact:true}).click();release();
  await page.getByRole("button",{name:"语义评测",exact:true}).click();await region.getByText("选择材料并冻结参考集",{exact:true}).click();await expect(region.getByRole("button",{name:"冻结本次参考版本",exact:true})).toBeDisabled();
});

test("basic new entries fit narrow screens without writes",async({page})=>{
  const writes:string[]=[];page.on("request",r=>{if(!["GET","HEAD"].includes(r.method()))writes.push(r.url());});await page.setViewportSize({width:390,height:1100});await page.goto("/");await page.getByRole("button",{name:"语义评测",exact:true}).click();
  await page.getByText("选择材料并冻结参考集",{exact:true}).click();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);expect(writes).toEqual([]);
});
