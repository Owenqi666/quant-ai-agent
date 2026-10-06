import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunDetail } from "../src/generated/api-contract";
import { assertApiResponse } from "../src/generated/api-contract";

const root = resolve("..");
const dimensions = ["引用与页码准确", "假设忠于所引原文", "经济解释归因恰当", "字段含义与替代说明", "实现与声明公式一致"];

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
const test = base.extend<{}, { recoveryServer: string }>({
  recoveryServer: [async ({}, use, workerInfo) => {
    const home = mkdtempSync(join(tmpdir(), "paper-alpha-recovery-e2e-"));
    const port = await freePort();
    if (port === 8765) throw new Error("Recovery acceptance must never use the live workspace port");
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
          if (child.exitCode !== null) throw new Error(`Owned recovery server exited during startup:\n${output}`);
          try {
            const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(750) });
            const health = await response.json();
            if (response.ok && health.worker?.online) return;
          } catch { /* Startup is bounded and does not contact the live workspace. */ }
          await delay(100);
        }
        throw new Error(`Owned recovery server did not become healthy:\n${output}`);
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
      writeFileSync(join(workerInfo.project.outputDir, `recovery-server-${workerInfo.workerIndex}.log`), output);
      if (!stopped) throw new Error("Owned recovery launcher required forced cleanup; inspect its log");
    }
  }, { scope: "worker" }],
  baseURL: async ({ recoveryServer }, use) => use(recoveryServer),
});

async function openOptionalTimer(page: Page) {
  const summary = page.getByText("可选：本次审核的主动工作时间", { exact: true });
  if (!(await summary.locator("..").getAttribute("open"))) {
    // An open details attribute is an empty string; inspect its presence.
    if (!(await summary.locator("..").evaluate((element) => element.hasAttribute("open")))) await summary.click();
  }
}

async function completedExample(request: APIRequestContext): Promise<RunDetail> {
  const exampleResponse = await request.post("/api/examples/alpha101", { data: {} });
  expect(exampleResponse.status()).toBe(200);
  const example = await exampleResponse.json();
  const submitted = await request.post("/api/runs", { data: {
    revision_id: example.revision_id, mode: "normalized_fixed", idempotency_key: `v07-recovery-${randomUUID()}`,
  } });
  expect(submitted.status()).toBe(201);
  const run = await submitted.json();
  await expect.poll(async () => {
    const current = await (await request.get(`/api/runs/${run.id}`)).json();
    return current.status;
  }, { timeout: 30000 }).toBe("completed");
  const result = await (await request.get(`/api/runs/${run.id}`)).json() as RunDetail;
  expect(result.verification?.verified).toBe(true);
  return result;
}

async function openReview(page: Page, runId: string) {
  await page.goto("/");
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(runId);
}

async function fillStructured(page: Page, note: string) {
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill(note);
  await page.getByLabel("添加分项研究审核", { exact: true }).check();
  await page.getByLabel("审核人标识", { exact: true }).fill("Recovery automation fixture");
  for (const label of dimensions) {
    await expect(page.getByLabel(`${label}判断`, { exact: true })).toHaveValue("not_assessed");
    await page.getByLabel(`${label}理由`, { exact: true }).fill(`未由人工评定：${label}`);
  }
}

test("committed review and approval survive dropped responses and reload without duplicate records", async ({ page, request }, testInfo) => {
  const run = await completedExample(request);
  await openReview(page, run.id);
  await fillStructured(page, "v07 response-loss fixture; no human judgment");
  const reviewBodies: unknown[] = [];
  let savedReviewId = "";
  const reviewRoute = `**/api/runs/${run.id}/reviews`;
  await page.route(reviewRoute, async (route) => {
    reviewBodies.push(route.request().postDataJSON());
    if (reviewBodies.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      savedReviewId = (await response.json()).id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.getByRole("region", { name: "审核提交恢复", exact: true })).toContainText("结果待确认");
  await expect.poll(() => savedReviewId).not.toBe("");
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(1);
  await expect(page.getByLabel("审核依据", { exact: true })).toBeDisabled();
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await expect(page.getByRole("button", { name: "安全重试审核提交", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "安全重试审核提交", exact: true }).click();
  await expect(page.getByText("审核已保存。", { exact: true })).toBeVisible();
  expect(reviewBodies).toHaveLength(2);
  expect(reviewBodies[1]).toEqual(reviewBodies[0]);
  const reviews = (await (await request.get(`/api/runs/${run.id}`)).json()).reviews;
  expect(reviews).toHaveLength(1); expect(reviews[0].id).toBe(savedReviewId);
  expect(reviews[0].source).toBe("automation");
  expect(reviews[0].assessment_summary.semantic_status).toBe("not_human");
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await expect(page.getByRole("button", { name: "恢复审核草稿", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");

  await page.getByLabel("引用人工审核", { exact: true }).selectOption(savedReviewId);
  await page.getByLabel("预期候选状态", { exact: true }).selectOption("evaluated");
  await page.getByLabel("批准理由", { exact: true }).fill("Automatic frozen-reference fixture; no economic claim");
  const caseBodies: unknown[] = [];
  let caseId = "";
  await page.route("**/api/regression-cases", async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    caseBodies.push(route.request().postDataJSON());
    if (caseBodies.length === 1) {
      const response = await route.fetch(); expect(response.status()).toBe(201);
      caseId = (await response.json()).id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "明确批准为回归用例", exact: true }).click();
  await expect(page.getByRole("region", { name: "回归批准提交恢复", exact: true })).toContainText("结果待确认");
  await expect.poll(() => caseId).not.toBe("");
  expect((await (await request.get("/api/regression-cases")).json()).filter((item: {id:string}) => item.id === caseId)).toHaveLength(1);
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  let injected = false;
  await page.route("**/api/catalog/runs?*", async (route) => {
    if (!injected) { injected = true; await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({detail:"Injected refresh failure after confirmed replay"}) }); }
    else await route.continue();
  });
  await page.getByRole("button", { name: "安全重试回归批准", exact: true }).click();
  await expect(page.getByText(/回归用例已保存，列表刷新暂未成功/)).toBeVisible();
  expect(injected).toBe(true);
  expect(caseBodies).toHaveLength(2); expect(caseBodies[1]).toEqual(caseBodies[0]);
  await expect(page.getByLabel("批准理由", { exact: true })).toHaveValue("");
  await expect(page.getByRole("button", { name: "明确批准为回归用例", exact: true })).toBeDisabled();
  const cases = await (await request.get("/api/regression-cases")).json();
  expect(cases.filter((item: { review_id:string }) => item.review_id === savedReviewId)).toHaveLength(1);
  // Generic review still freezes the displayed output without inventing a
  // structured assessment. A separate intentional action receives a new key.
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha101");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  const genericNote = "Generic recovery fixture with explicit target";
  await page.getByLabel("审核依据", { exact: true }).fill(genericNote);
  await expect(page.getByLabel("添加分项研究审核", { exact: true })).not.toBeChecked();
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.locator(".review-history article").filter({has:page.getByText(genericNote,{exact:true})})).toBeVisible();
  expect(reviewBodies).toHaveLength(3);
  const genericBody = reviewBodies[2] as Record<string,unknown>;
  const genericTarget = run.review_targets.find((item) => item.candidate_id === "alpha101")!;
  expect(genericBody.expected_attempt_id).toBe(genericTarget.attempt_id);
  expect(genericBody.expected_result_digest).toBe(genericTarget.result_digest);
  expect(genericBody.assessment).toBeUndefined();
  expect(genericBody.idempotency_key).not.toBe((reviewBodies[0] as Record<string,unknown>).idempotency_key);
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(2);
  await page.screenshot({path:testInfo.outputPath("replayed-review-and-approval.png"), fullPage:true});
});

test("explicit draft restoration retains judgments and completed intervals but excludes interrupted time and other targets", async ({ page, request }, testInfo) => {
  const run = await completedExample(request);
  await openReview(page, run.id);
  await fillStructured(page, "Recover this exact candidate and frozen output");
  await page.getByLabel("引用与页码准确判断", { exact: true }).selectOption("failed");
  await openOptionalTimer(page);
  await page.getByRole("button", { name: "开始主动工作计时", exact: true }).click();
  await page.getByRole("button", { name: "暂停主动工作计时", exact: true }).click();
  await openOptionalTimer(page);
  await page.getByRole("button", { name: "开始主动工作计时", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("审核依据", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "恢复审核草稿", exact: true }).click();
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("Recover this exact candidate and frozen output");
  await expect(page.getByLabel("审核声明来源", { exact: true })).toHaveValue("automation");
  await expect(page.getByLabel("引用与页码准确判断", { exact: true })).toHaveValue("failed");
  await expect(page.getByLabel("假设忠于所引原文判断", { exact: true })).toHaveValue("not_assessed");
  await expect(page.getByLabel("假设忠于所引原文理由", { exact: true })).toHaveValue("未由人工评定：假设忠于所引原文");
  await expect(page.getByText(/未结束区间已排除/)).toBeVisible();
  await openOptionalTimer(page);
  await expect(page.getByText(/已记录 1 个区间/)).toBeVisible();
  await expect(page.getByRole("button", { name: "暂停主动工作计时", exact: true })).toBeDisabled();
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha101");
  await expect(page.getByRole("button", { name: "恢复审核草稿", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");
  // A changed server-issued digest must never load the old output's draft.
  await page.route(`**/api/runs/${run.id}`, async (route) => {
    const response = await route.fetch(); const body = await response.json();
    body.review_targets = body.review_targets.map((target: {candidate_id:string;result_digest:string}) => target.candidate_id === "alpha006" ? {...target,result_digest:"b".repeat(64)} : target);
    await route.fulfill({response,json:body});
  });
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await expect(page.getByRole("button", { name: "恢复审核草稿", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill("Stale generic target must be rejected before writing");
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.getByRole("button", { name: "解除被拒绝的审核请求", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "审核提交恢复", exact: true })).toContainText("已明确拒绝");
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(0);
  await page.getByRole("button", { name: "解除被拒绝的审核请求", exact: true }).click();
  await expect(page.getByLabel("审核依据", { exact: true })).toBeEnabled();
  await expect(page.getByRole("region", { name: "审核提交恢复", exact: true })).toHaveCount(0);
  await page.screenshot({path:testInfo.outputPath("result-isolated-draft.png"),fullPage:true});
});

test("unavailable durable storage is visible and prevents an unsafe POST", async ({ page, request }) => {
  const run = await completedExample(request);
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key: string, value: string) {
      if (key.startsWith("paper-alpha:recovery:")) throw new DOMException("Injected quota failure", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  await openReview(page, run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill("Storage failure fixture");
  await expect(page.getByText(/浏览器无法保存审核草稿/)).toBeVisible();
  let posts = 0;
  await page.route(`**/api/runs/${run.id}/reviews`, async (route) => { posts++; await route.continue(); });
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.getByText(/无法持久保存请求，尚未发送/)).toBeVisible();
  expect(posts).toBe(0);
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(0);
});


test("saved review retains its original request across failed draft cleanup and safe replay", async ({ page, request }, testInfo) => {
  const run = await completedExample(request);
  await page.addInitScript(() => {
    const original = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function(key: string) {
      if (sessionStorage.getItem("allow-draft-cleanup") !== "true" && key.startsWith("paper-alpha:recovery:") && key.includes('"review-draft"')) throw new DOMException("Injected draft removal failure", "SecurityError");
      return original.call(this, key);
    };
  });
  await openReview(page, run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill("Cleanup failure recovery fixture");
  const bodies: unknown[] = [];
  await page.route(`**/api/runs/${run.id}/reviews`, async (route) => { bodies.push(route.request().postDataJSON()); await route.continue(); });
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect(page.getByText(/审核已保存。本机恢复记录尚未清理/)).toBeVisible();
  await expect(page.getByRole("button", { name: "安全重试审核提交", exact: true })).toBeVisible();
  await expect(page.getByLabel("审核依据", { exact: true })).toBeDisabled();
  const storage = await page.evaluate(() => Object.entries(localStorage).filter(([key]) => key.startsWith("paper-alpha:recovery:")));
  expect(storage.some(([key]) => key.includes('"review-draft"'))).toBe(true);
  expect(storage.some(([key]) => key.includes('"review-request"'))).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  // Remove only the deliberately injected storage fault; the request itself
  // remains persisted and is replayed through the real backend.
  await page.evaluate(() => sessionStorage.setItem("allow-draft-cleanup", "true"));
  await page.getByRole("button", { name: "安全重试审核提交", exact: true }).click();
  await expect(page.getByText("审核已保存。", { exact: true })).toBeVisible();
  expect(bodies).toHaveLength(2); expect(bodies[0]).toEqual(bodies[1]);
  expect((await (await request.get(`/api/runs/${run.id}`)).json()).reviews).toHaveLength(1);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await expect(page.getByRole("button", { name: "恢复审核草稿", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("审核依据", { exact: true })).toHaveValue("");
  await page.screenshot({path:testInfo.outputPath("draft-cleanup-recovery.png"),fullPage:true});
});


test("an already saved review remains recoverable when the displayed run advances to an unverified attempt", async ({ page, request }, testInfo) => {
  const run = await completedExample(request);
  await openReview(page, run.id);
  await page.getByLabel("审核候选", { exact: true }).selectOption("alpha006");
  await page.getByLabel("审核声明来源", { exact: true }).selectOption("automation");
  await page.getByLabel("审核依据", { exact: true }).fill("Recover original receipt while the current run is not reviewable");
  const bodies: unknown[] = [];
  let savedId = "";
  await page.route(`**/api/runs/${run.id}/reviews`, async (route) => {
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      const response = await route.fetch(); expect(response.status()).toBe(201);
      savedId = (await response.json()).id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "保存审核", exact: true }).click();
  await expect.poll(() => savedId).not.toBe("");
  // Only GET presentation is changed here. The first write and its replay both
  // use the real service/receipt. Backend tests separately mutate real attempts.
  await page.route(`**/api/runs/${run.id}`, async (route) => {
    const response = await route.fetch(); const body = await response.json();
    body.status = "running";
    body.attempt_count += 1;
    body.finished_at = null;
    body.attempts = [...body.attempts, {
      ...body.attempts[body.attempts.length - 1], id: "11111111-1111-4111-8111-111111111111",
      number: body.attempt_count, status: "running", finished_at: null, state_digest: null,
    }];
    body.state = null;
    body.verification = null;
    body.review_targets = [];
    assertApiResponse(`/api/runs/${run.id}`, "GET", 200, body);
    await route.fulfill({response,json:body});
  });
  await page.reload();
  await page.getByRole("button", { name: "审核与回归", exact: true }).click();
  await page.getByLabel("选择已结束的实验", { exact: true }).selectOption(run.id);
  await expect(page.getByText(/新审核暂不可用/)).toBeVisible();
  await expect(page.getByLabel("审核候选", { exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "保存审核", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "安全重试审核提交", exact: true }).click();
  await expect(page.getByText("审核已保存。", { exact: true })).toBeVisible();
  expect(bodies).toHaveLength(2); expect(bodies[0]).toEqual(bodies[1]);
  const actual = (await (await request.get(`/api/runs/${run.id}`)).json()) as RunDetail;
  expect(actual.reviews).toHaveLength(1); expect(actual.reviews[0].id).toBe(savedId);
  const target = run.review_targets.find((item) => item.candidate_id === "alpha006")!;
  expect(actual.reviews[0].attempt_id).toBe(target.attempt_id);
  expect(actual.reviews[0].result_digest).toBe(target.result_digest);
  await expect(page.getByText(/新审核暂不可用/)).toBeVisible();
  await expect(page.getByLabel("审核候选", { exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "保存审核", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "安全重试审核提交", exact: true })).toHaveCount(0);
  await page.screenshot({path:testInfo.outputPath("unreviewable-run-replay.png"),fullPage:true});
});
