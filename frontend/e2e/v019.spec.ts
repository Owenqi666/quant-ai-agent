import {test,expect} from "@playwright/test";
import {readFileSync} from "node:fs";

const dimensions=["引用与页码准确","假设忠于所引原文","经济解释归因恰当","字段含义与替代说明","实现与声明公式一致"];

test("material decisions start blank and lost automation responses recover without human labels",async({page,request},info)=>{
  await page.goto("/");await page.getByRole("button",{name:"语义评测",exact:true}).click();
  const region=page.getByRole("region",{name:"语义材料标注",exact:true});await expect(region).toHaveAttribute("data-material-id","alpha101_formula");
  for(const label of dimensions)await expect(region.getByLabel(`${label}判断`,{exact:true})).toHaveValue("");
  await expect(region.getByLabel("标注声明来源",{exact:true})).toHaveValue("");
  await expect(region.getByRole("button",{name:"保存自己的材料标注",exact:true})).toBeDisabled();
  await expect(region.getByRole("link",{name:"打开来源 alpha101_paper",exact:true})).toHaveAttribute("href","/api/semantic-material-sources/alpha101_paper");
  await region.getByLabel("标注声明来源",{exact:true}).selectOption("automation");
  await region.getByLabel("材料审核者标识",{exact:true}).fill("Browser automation fixture, not human review");
  await region.getByRole("button",{name:"记录当前确认时间",exact:true}).click();
  for(const label of dimensions){await region.getByLabel(`${label}判断`,{exact:true}).selectOption("not_assessed");await region.getByLabel(`${label}理由`,{exact:true}).fill("Browser recovery test only; semantic truth not assessed.");}
  await region.getByRole("button",{name:"预检材料标注",exact:true}).click();
  await expect(region.getByRole("button",{name:"保存自己的材料标注",exact:true})).toBeEnabled();
  const bodies:unknown[]=[];let lose=true;
  await page.route("**/api/semantic-annotations",async route=>{if(route.request().method()!=="POST")return route.continue();bodies.push(route.request().postDataJSON());const response=await route.fetch();if(lose){lose=false;await route.abort();}else await route.fulfill({response});});
  await region.getByRole("button",{name:"保存自己的材料标注",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试材料标注提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"语义评测",exact:true}).click();
  await page.getByRole("button",{name:"安全重试材料标注提交",exact:true}).click();
  await expect(page.getByRole("region",{name:"已保存材料标注",exact:true})).toBeVisible();expect(bodies).toHaveLength(2);expect(bodies[0]).toEqual(bodies[1]);
  const summary=await(await request.get("/api/semantic-annotations/summary")).json();expect(summary.human_records).toBe(0);expect(summary.pending_cases).toBe(7);expect(summary.semantic_quality_score).toBeNull();
  await page.setViewportSize({width:390,height:1100});
  await page.getByText("材料与来源的准确身份",{exact:true}).click();
  await page.getByText(/^查看标注 semantic_annotation_/).first().click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath("semantic-annotation-recovery.png"),fullPage:true});
});

test("bounded monthly path runs the original worker and opens its exact output",async({page,request})=>{
  const config=(await(await request.get("/api/research-protocols/presets")).json()).presets[1].config;
  const protocolResponse=await request.post("/api/research-protocols",{data:{title:"v019 isolated browser monthly protocol",note:"Synthetic project example, no paper or market claim",config}});expect(protocolResponse.status()).toBe(201);const protocol=await protocolResponse.json();
  await page.goto("/");await page.getByRole("button",{name:"研究执行",exact:true}).click();
  const form=page.getByRole("region",{name:"跨路径研究执行",exact:true});await form.getByText("创建一次跨路径研究执行",{exact:true}).click();
  await form.getByLabel("选择领域研究来源",{exact:true}).selectOption(protocol.id);
  await form.getByLabel("受控月度结束月份",{exact:true}).fill("2025-02");
  const created=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/domain-research-jobs"&&r.request().method()==="POST");
  await form.getByRole("button",{name:"创建受控领域执行",exact:true}).click();const job=await(await created).json();
  const detail=page.getByRole("region",{name:"领域研究执行详情",exact:true});await expect(detail).toHaveAttribute("data-domain-job-id",job.id);
  for(const name of ["校验领域来源与配置","提交受控月度实验","核对受控月度结果","完成领域研究执行"]){const button=detail.getByRole("button",{name,exact:true});await expect(button).toBeEnabled();await button.click();}
  await expect(detail.getByRole("heading",{name:"领域执行完成",exact:true})).toBeVisible();
  const finished=await(await request.get(`/api/domain-research-jobs/${job.id}`)).json();expect(finished.provider_connected).toBe(false);expect(JSON.parse(finished.result_json).verification.verified).toBe(true);
  await detail.getByRole("button",{name:"查看本次月度实际输出",exact:true}).click();
  await expect(page.getByRole("region",{name:"月度实验详情",exact:true})).toHaveAttribute("data-experiment-id",finished.experiment_id);
  await expect(page.getByRole("region",{name:"月度计算结果",exact:true})).toBeVisible();
});

test("switching material while preflight is pending never offers the old judgment on the new material",async({page})=>{
  await page.goto("/");await page.getByRole("button",{name:"语义评测",exact:true}).click();
  const region=page.getByRole("region",{name:"语义材料标注",exact:true});await region.getByLabel("标注声明来源",{exact:true}).selectOption("automation");
  await region.getByLabel("材料审核者标识",{exact:true}).fill("Stale response browser fixture");await region.getByRole("button",{name:"记录当前确认时间",exact:true}).click();
  for(const label of dimensions){await region.getByLabel(`${label}判断`,{exact:true}).selectOption("not_assessed");await region.getByLabel(`${label}理由`,{exact:true}).fill("No actual semantic judgment.");}
  let release!:()=>void,arrive!:()=>void;const gate=new Promise<void>(r=>{release=r;}),arrived=new Promise<void>(r=>{arrive=r;});
  await page.route("**/api/semantic-annotations/preview",async route=>{const response=await route.fetch();arrive();await gate;await route.fulfill({response});});
  await region.getByRole("button",{name:"预检材料标注",exact:true}).click();await arrived;
  await page.getByLabel("选择语义评测材料",{exact:true}).selectOption("wrong_quote");
  await expect(region).toHaveAttribute("data-material-id","wrong_quote");release();
  await expect(region.getByRole("button",{name:"保存自己的材料标注",exact:true})).toBeDisabled();
  await expect(region.getByLabel("引用与页码准确判断",{exact:true})).toHaveValue("");
});

test("author diagnostic remains stopped and opens its source without creating a portfolio",async({page,request})=>{
  const scan=JSON.parse(readFileSync("../evaluation_suites/v017/blocked_scan.json","utf8"));
  const sourceResponse=await request.post("/api/author-studies",{data:{title:"v019 isolated browser author diagnostic",note:"Synthetic aggregate fixture",scan,parent_review_id:null,author_panel_ids:[],idempotency_key:"v019-browser-author"}});expect(sourceResponse.status()).toBe(201);const study=await sourceResponse.json();
  const before=await(await request.get("/api/monthly-experiments")).json();
  await page.goto("/");await page.getByRole("button",{name:"研究执行",exact:true}).click();const form=page.getByRole("region",{name:"跨路径研究执行",exact:true});
  await form.getByText("创建一次跨路径研究执行",{exact:true}).click();await form.getByLabel("领域执行路径",{exact:true}).selectOption("author_study_diagnostic");await form.getByLabel("选择领域研究来源",{exact:true}).selectOption(study.id);
  await form.getByRole("button",{name:"创建受控领域执行",exact:true}).click();const detail=page.getByRole("region",{name:"领域研究执行详情",exact:true});await detail.getByRole("button",{name:"校验领域来源与配置",exact:true}).click();
  await expect(detail.getByRole("heading",{name:"研究条件阻断",exact:true})).toBeVisible();expect((await(await request.get("/api/monthly-experiments")).json()).total).toBe(before.total);
  await detail.getByRole("button",{name:"查看作者准入诊断",exact:true}).click();await expect(page.getByRole("region",{name:"研究准入详情",exact:true})).toHaveAttribute("data-study-id",study.id);
});

test("basic material and cross-path entries fit a narrow screen without sending mutations",async({page})=>{
  const writes:string[]=[];page.on("request",r=>{if(!["GET","HEAD"].includes(r.method()))writes.push(r.url());});
  await page.setViewportSize({width:390,height:1100});await page.goto("/");
  await page.getByRole("button",{name:"语义评测",exact:true}).click();
  const material=page.getByRole("region",{name:"语义材料标注",exact:true});
  for(const id of ["alpha101_formula","wrong_quote","momentum_window"]){
    await page.getByLabel("选择语义评测材料",{exact:true}).selectOption(id);
    await expect(material).toHaveAttribute("data-material-id",id);
    await material.getByText("材料与来源的准确身份",{exact:true}).click();
    await material.getByText("查看未经人工确认的参考提示",{exact:true}).click();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  }
  await page.getByRole("button",{name:"研究执行",exact:true}).click();
  const form=page.getByRole("region",{name:"跨路径研究执行",exact:true});
  await form.getByText("创建一次跨路径研究执行",{exact:true}).click();
  for(const kind of ["monthly_fixture","author_study_diagnostic"]){
    await form.getByLabel("领域执行路径",{exact:true}).selectOption(kind);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  }
  expect(writes).toEqual([]);
});
