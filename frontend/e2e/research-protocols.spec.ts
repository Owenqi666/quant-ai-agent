import { test as base, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { ProtocolRecord } from "../src/generated/api-contract";

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
const test = base.extend<{}, { protocolServer: { url:string; home:string } }>({
  protocolServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-protocols-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Protocol acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned protocols server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned protocols server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `protocols-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned protocols launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ protocolServer }, use) => use(protocolServer.url),
});

async function openProtocols(page:Page){
  await page.goto("/");await expect(page.getByText("服务已连接",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"研究规则",exact:true}).click();
  await expect(page.getByLabel("规则预设",{exact:true})).toBeEnabled();
}

test("empty workspace supports paper blocked preview and explicit project variation with saved parent differences",async({page,request},testInfo)=>{
  await openProtocols(page);
  expect((await (await request.get("/api/researches")).json()).length).toBe(0);
  await expect(page.getByLabel("MOM 回看月数",{exact:true})).toHaveValue("11");
  await expect(page.getByLabel("MOM 跳过月数",{exact:true})).toHaveValue("1");
  await page.screenshot({path:testInfo.outputPath("project-default.png"),fullPage:true});
  await page.getByLabel("规则预设",{exact:true}).selectOption("paper");
  await expect(page.getByLabel("ID 回看月数",{exact:true})).toBeDisabled();
  await expect(page.getByLabel("ID 回看月数",{exact:true})).toHaveValue("");
  await page.getByRole("button",{name:"预览日期与覆盖",exact:true}).click();
  const diagnostic=page.getByRole("region",{name:"规则演示诊断",exact:true});
  await expect(diagnostic.getByRole("heading",{level:2})).toHaveText("未决规则，暂不可计算");
  const downloadPromise=page.waitForEvent("download");
  await page.getByRole("button",{name:"下载演示诊断 JSON",exact:true}).click();
  const download=await downloadPromise;const stream=await download.createReadStream();const chunks:Buffer[]=[];for await(const chunk of stream!)chunks.push(chunk);
  const exported=JSON.parse(Buffer.concat(chunks).toString());expect(exported.status).toBe("blocked");expect(exported.config.mode).toBe("paper");expect(exported.assets.every((asset:{momentum:number|null;id:number|null})=>asset.momentum===null&&asset.id===null)).toBe(true);
  await page.getByLabel("规则版本名称",{exact:true}).fill("Browser automation paper unresolved");
  const paperSaved=page.waitForResponse(response=>new URL(response.url()).pathname==="/api/research-protocols"&&response.request().method()==="POST");
  await page.getByRole("button",{name:"保存规则版本",exact:true}).click();
  const paper=await (await paperSaved).json() as ProtocolRecord;
  await expect(page.getByRole("article",{name:"保存规则详情",exact:true})).toContainText(paper.id);
  await page.getByRole("button",{name:"明确切换为项目变体",exact:true}).click();
  await expect(diagnostic).toHaveCount(0);
  await page.getByLabel("MOM 回看月数",{exact:true}).fill("6");
  await page.getByText("高级：覆盖与连续缺失规则",{exact:true}).click();
  await page.getByLabel("缺失观察规则",{exact:true}).selectOption("available");
  await page.getByLabel("最低有效覆盖率",{exact:true}).fill("0.9");
  await page.getByLabel("最大连续缺失交易日",{exact:true}).fill("3");
  await expect(page.getByText(/只复合已观察到的有效日收益/)).toBeVisible();
  await page.getByRole("button",{name:"预览日期与覆盖",exact:true}).click();
  await expect(diagnostic).toContainText("2024-06-01 至 2024-11-30");
  await expect(diagnostic).toContainText("controlled_fixture");
  await page.screenshot({path:testInfo.outputPath("project-variant-preview.png"),fullPage:true});
  await page.getByLabel("规则修改说明",{exact:true}).fill("Automation: explicit six-month and partial-coverage project convention");
  const variantSaved=page.waitForResponse(response=>new URL(response.url()).pathname==="/api/research-protocols"&&response.request().method()==="POST");
  await page.getByRole("button",{name:"保存规则版本",exact:true}).click();
  const variant=await (await variantSaved).json() as ProtocolRecord;
  expect(variant.parent_id).toBe(paper.id);expect(variant.config.mode).toBe("project");
  expect(variant.changes).toContainEqual({field:"mode",before:"paper",after:"project"});
  expect(variant.changes).toContainEqual({field:"mom_window_months",before:11,after:6});
  await page.reload();await page.getByRole("button",{name:"研究规则",exact:true}).click();
  await page.getByLabel("读取规则版本",{exact:true}).selectOption(variant.id);
  await expect(page.getByLabel("MOM 回看月数",{exact:true})).toHaveValue("6");
  await expect(page.getByRole("article",{name:"保存规则详情",exact:true})).toContainText(paper.id);
});

test("lost committed save response survives reload and replays original body without duplicate versions",async({page,request})=>{
  await openProtocols(page);await page.getByLabel("规则版本名称",{exact:true}).fill("Browser automation response-loss version");
  await page.getByLabel("MOM 回看月数",{exact:true}).fill("7");
  let first=true,saved:ProtocolRecord|undefined;const bodies:unknown[]=[];
  await page.route("**/api/research-protocols",async route=>{
    if(route.request().method()!=="POST"){await route.continue();return;}
    bodies.push(route.request().postDataJSON());
    if(first){first=false;const response=await route.fetch();expect(response.status()).toBe(201);saved=await response.json();await route.abort("failed");}else await route.continue();
  });
  await page.getByRole("button",{name:"保存规则版本",exact:true}).click();
  await expect(page.getByRole("region",{name:"规则保存恢复",exact:true})).toContainText("保存结果待确认");
  await page.reload();await page.getByRole("button",{name:"研究规则",exact:true}).click();
  await expect(page.getByLabel("规则版本名称",{exact:true})).toBeDisabled();
  await page.getByRole("button",{name:"安全重试原规则保存",exact:true}).click();
  await expect(page.getByRole("region",{name:"规则保存恢复",exact:true})).toHaveCount(0);
  expect(bodies).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);
  const listed=await (await request.get("/api/research-protocols?limit=100&offset=0")).json();
  expect(listed.items.filter((record:ProtocolRecord)=>record.title===saved!.title)).toHaveLength(1);
  await page.getByLabel("读取规则版本",{exact:true}).selectOption(saved!.id);
  await expect(page.getByLabel("MOM 回看月数",{exact:true})).toHaveValue("7");
});

test("editing clears the preview and late configuration or target responses cannot replace a newer diagnosis",async({page})=>{
  await openProtocols(page);let release!:()=>void,arrived!:()=>void,done!:()=>void,first=true;
  const hold=new Promise<void>(resolve=>{release=resolve;}),arrival=new Promise<void>(resolve=>{arrived=resolve;}),finished=new Promise<void>(resolve=>{done=resolve;});
  await page.route("**/api/research-protocols/preview",async route=>{
    if(!first){await route.continue();return;}first=false;
    const response=await route.fetch();expect(response.status()).toBe(200);arrived();await hold;
    try{await route.fulfill({response});}finally{done();}
  });
  await page.getByRole("button",{name:"预览日期与覆盖",exact:true}).click();await arrival;
  await page.getByLabel("MOM 回看月数",{exact:true}).fill("3");
  await page.getByLabel("诊断目标月份",{exact:true}).fill("2025-03");
  await page.getByRole("button",{name:"预览日期与覆盖",exact:true}).click();
  const diagnostic=page.getByRole("region",{name:"规则演示诊断",exact:true});
  await expect(diagnostic).toHaveAttribute("data-target-month","2025-03");
  await expect(diagnostic).toContainText("2024-11-01 至 2025-01-31");
  release();await finished;
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(diagnostic).toHaveAttribute("data-target-month","2025-03");
  await page.getByLabel("MOM 跳过月数",{exact:true}).fill("0");await expect(diagnostic).toHaveCount(0);
});

test("late saved version from another workspace cannot change the current editor or remove its original recovery request",async({page,request})=>{
  const health=await (await request.get("/api/health")).json();let switched=false;
  await page.route("**/api/health",async route=>{const response=await route.fetch(),body=await response.json();if(switched)body.workspace_id=`${health.workspace_id}-protocol-test`;await route.fulfill({response,json:body});});
  await openProtocols(page);await page.getByLabel("规则版本名称",{exact:true}).fill("Old workspace saved rule");
  let release!:()=>void,arrived!:()=>void,done!:()=>void;
  const hold=new Promise<void>(resolve=>{release=resolve;}),arrival=new Promise<void>(resolve=>{arrived=resolve;}),finished=new Promise<void>(resolve=>{done=resolve;});
  await page.route("**/api/research-protocols",async route=>{
    if(route.request().method()!=="POST"){await route.continue();return;}
    const response=await route.fetch();expect(response.status()).toBe(201);arrived();await hold;
    try{await route.fulfill({response});}finally{done();}
  });
  await page.getByRole("button",{name:"保存规则版本",exact:true}).click();await arrival;
  const pendingKey=await page.evaluate(()=>Object.keys(localStorage).find(key=>key.startsWith("paper-alpha:protocol-save:"))!);
  switched=true;await page.getByRole("button",{name:"刷新工作台",exact:true}).click();
  await expect(page.getByLabel("规则版本名称",{exact:true})).toBeEnabled();
  await page.getByLabel("规则版本名称",{exact:true}).fill("New workspace unsent rule");
  release();await finished;
  await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.getByLabel("规则版本名称",{exact:true})).toHaveValue("New workspace unsent rule");
  await expect(page.getByRole("region",{name:"规则保存恢复",exact:true})).toHaveCount(0);
  expect(await page.evaluate(key=>localStorage.getItem(key),pendingKey)).not.toBeNull();
});

test("late saved-record lookup cannot overwrite a newer edit or enable saving an unresolved selection",async({page,request})=>{
  const presets=await (await request.get("/api/research-protocols/presets")).json();
  const config={...presets.presets.find((item:{config:{mode:string}})=>item.config.mode==="project").config,mom_window_months:4};
  const created=await request.post("/api/research-protocols",{data:{title:"Browser automation delayed record read",note:"",config}});expect(created.status()).toBe(201);
  const record=await created.json() as ProtocolRecord;
  await openProtocols(page);
  let release!:()=>void,arrived!:()=>void,done!:()=>void;
  const hold=new Promise<void>(resolve=>{release=resolve;}),arrival=new Promise<void>(resolve=>{arrived=resolve;}),finished=new Promise<void>(resolve=>{done=resolve;});
  await page.route(`**/api/research-protocols/${record.id}`,async route=>{const response=await route.fetch();arrived();await hold;try{await route.fulfill({response});}finally{done();}});
  await page.getByLabel("读取规则版本",{exact:true}).selectOption(record.id);await arrival;
  await expect(page.getByRole("button",{name:"保存规则版本",exact:true})).toBeDisabled();
  await page.getByLabel("规则版本名称",{exact:true}).fill("New edit owns this form");
  release();await finished;await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  await expect(page.getByLabel("规则版本名称",{exact:true})).toHaveValue("New edit owns this form");
  await expect(page.getByLabel("MOM 回看月数",{exact:true})).toHaveValue("11");
  await expect(page.getByRole("button",{name:"保存规则版本",exact:true})).toBeEnabled();
});
