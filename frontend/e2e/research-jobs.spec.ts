import {test,expect} from "@playwright/test";
import type {APIRequestContext} from "@playwright/test";

async function isolatedResearch(request:APIRequestContext,title:string) {
  const seed=await(await request.post("/api/examples/alpha101")).json();
  const source=await(await request.get(`/api/researches/${seed.research_id}`)).json();
  const task=source.revisions.find((row:{id:string})=>row.id===seed.revision_id).task;
  const response=await request.post("/api/researches",{data:{title,paper_id:source.paper_id,dataset_id:source.dataset_id,task}});
  expect(response.status()).toBe(201);const research=await response.json();
  return {research_id:research.id,revision_id:research.latest_revision_id};
}

test("explicit candidate executes and saves only server-resolved claim values",async({page,request},testInfo)=>{
  const example=await isolatedResearch(request,"Execution browser isolated research");
  await page.goto("/");await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"研究执行",exact:true}).click();
  await page.getByText("创建一次研究执行",{exact:true}).click();
  await page.getByLabel("选择执行研究",{exact:true}).selectOption(example.research_id);
  await page.getByRole("checkbox",{name:"执行候选 alpha101",exact:true}).check();
  const created=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/research-jobs"&&r.request().method()==="POST");
  await page.getByRole("button",{name:"创建受控研究执行",exact:true}).click();
  const job=await(await created).json();const region=page.getByRole("region",{name:"研究执行详情",exact:true});
  await expect(region).toHaveAttribute("data-job-id",job.id);
  for(const name of ["校验候选草稿","保存已校验的研究修订","提交冻结实验","核对本次实验","完成研究执行"]){
    const button=region.getByRole("button",{name,exact:true});await expect(button).toBeEnabled();await button.click();
  }
  await expect(region.getByRole("heading",{name:"研究执行完成",exact:true})).toBeVisible();
  const finished=await(await request.get(`/api/research-jobs/${job.id}`)).json();
  expect(finished.steps).toHaveLength(5);expect(finished.provider_connected).toBe(false);
  expect(JSON.parse(finished.result_json).verification.verified).toBe(true);
  await region.getByRole("button",{name:"关联到研究任务",exact:true}).click();
  const detail=page.getByRole("region",{name:"研究任务详情",exact:true});await expect(detail).toBeVisible();
  await detail.getByText("带实际数值引用的结论草稿",{exact:true}).click();
  await detail.getByLabel("待审核的解释文字",{exact:true}).fill("Automated fixture narrative: this interpretation still needs human review.");
  await detail.getByRole("button",{name:"核对实际指标引用",exact:true}).click();
  const preview=detail.getByRole("region",{name:"结论引用核验",exact:true});await expect(preview).toContainText("服务计算的实际值");
  const saved=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/research-claims"&&r.request().method()==="POST");
  await detail.getByRole("button",{name:"保存已核对的结论草稿",exact:true}).click();
  const claims=await(await saved).json();expect(claims.claims[0].semantic_fidelity).toBe("unverified");
  expect(claims.claims[0].metrics[0].verification).toBe("verified_result_value");
  const run=await(await request.get(`/api/runs/${finished.run_id}`)).json();expect(run.reviews).toEqual([]);
  await expect(detail.getByRole("link",{name:"下载结论引用记录",exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("execution-and-authoritative-claim.png"),fullPage:true});
});

test("lost creation and commit responses replay exact requests without duplicate effects",async({page,request})=>{
  const example=await isolatedResearch(request,"Execution response recovery isolated research");
  await page.goto("/");await page.getByRole("button",{name:"研究执行",exact:true}).click();
  await page.getByText("创建一次研究执行",{exact:true}).click();
  await page.getByLabel("选择执行研究",{exact:true}).selectOption(example.research_id);
  await page.getByRole("checkbox",{name:"执行候选 alpha101",exact:true}).check();
  const bodies:unknown[]=[];let lose=true;
  await page.route("**/api/research-jobs",async route=>{
    if(route.request().method()!=="POST")return route.continue();
    bodies.push(route.request().postDataJSON());const response=await route.fetch();
    if(lose){lose=false;await route.abort();}else await route.fulfill({response});
  });
  await page.getByRole("button",{name:"创建受控研究执行",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试研究执行提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"研究执行",exact:true}).click();
  await page.getByRole("button",{name:"安全重试研究执行提交",exact:true}).click();
  expect(bodies).toHaveLength(2);expect(bodies[0]).toEqual(bodies[1]);
  const region=page.getByRole("region",{name:"研究执行详情",exact:true});await expect(region).toBeVisible();
  const id=await region.getAttribute("data-job-id");
  await region.getByRole("button",{name:"校验候选草稿",exact:true}).click();
  let loseCommit=true;const steps:unknown[]=[];
  await page.route(`**/api/research-jobs/${id}/advance`,async route=>{
    steps.push(route.request().postDataJSON());const response=await route.fetch();
    if(loseCommit){loseCommit=false;await route.abort();}else await route.fulfill({response});
  });
  await region.getByRole("button",{name:"保存已校验的研究修订",exact:true}).click();
  await expect(page.getByRole("button",{name:"安全重试执行步骤提交",exact:true})).toBeVisible();
  await page.reload();await page.getByRole("button",{name:"研究执行",exact:true}).click();
  await page.getByRole("button",{name:`查看研究执行 ${id}`,exact:true}).click();
  await page.getByRole("button",{name:"安全重试执行步骤提交",exact:true}).click();
  expect(steps).toHaveLength(2);expect(steps[0]).toEqual(steps[1]);
  const job=await(await request.get(`/api/research-jobs/${id}`)).json();expect(job.steps).toHaveLength(2);expect(job.state).toBe("committed");
  const research=await(await request.get(`/api/researches/${example.research_id}`)).json();
  expect(research.revisions.filter((r:{id:string})=>r.id===job.revision_id)).toHaveLength(1);
});
