import { expect, test } from "@playwright/test";
import { resolve } from "node:path";

test("delayed paper upload, offline health and revision completion preserve newer user edits and navigation", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const example = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const create = async (title: string) => (await (await request.post("/api/researches", { data: { title, paper_id: example.paper_id, dataset_id: example.dataset_id, task: example.revisions[0].task } })).json());
  const a = await create("异步保存研究 A"), b = await create("异步保存研究 B");
  await page.goto("/");
  await page.getByRole("button", { name: "新建研究", exact: true }).first().click();
  let releaseUpload!: () => void, uploadArrived!: () => void;
  const release = new Promise<void>((resolve) => { releaseUpload = resolve; });
  const arrived = new Promise<void>((resolve) => { uploadArrived = resolve; });
  await page.route("**/api/papers", async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    const response = await route.fetch(); uploadArrived(); await release; await route.fulfill({ response });
  });
  await page.getByLabel("论文文件", { exact: true }).setInputFiles(resolve("../examples/alpha101/paper.pdf"));
  await page.getByRole("button", { name: "上传论文", exact: true }).click();
  await arrived;
  await page.getByLabel("研究标题", { exact: true }).fill("上传期间的最新编辑");
  await page.getByLabel("最多工具调用次数", { exact: true }).fill("37");
  releaseUpload();
  await expect(page.getByRole("button", { name: "上传论文", exact: true })).toBeVisible();
  await expect(page.getByLabel("研究标题", { exact: true })).toHaveValue("上传期间的最新编辑");
  await expect(page.getByLabel("最多工具调用次数", { exact: true })).toHaveValue("37");
  await page.unroute("**/api/papers");
  await page.route("**/api/health", (route) => route.abort("failed"));
  await expect(page.getByText("服务未连接", { exact: true })).toBeVisible();
  await expect(page.getByLabel("研究标题", { exact: true })).toHaveValue("上传期间的最新编辑");
  await page.getByLabel("研究标题", { exact: true }).fill("离线期间继续编辑");
  await page.unroute("**/api/health");
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await expect(page.getByLabel("研究标题", { exact: true })).toHaveValue("离线期间继续编辑");
  await expect(page.getByRole("button", { name: "恢复此草稿", exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: a.title, exact: true }).click();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await page.getByLabel("最多工具调用次数", { exact: true }).fill("30");
  await page.getByLabel("修改说明", { exact: true }).fill("延迟真实响应保护验收，原公式保持一致。");
  let releaseRevision!: () => void, revisionArrived!: () => void;
  const revisionRelease = new Promise<void>((resolve) => { releaseRevision = resolve; });
  const revisionArrival = new Promise<void>((resolve) => { revisionArrived = resolve; });
  await page.route(`**/api/researches/${a.id}/revisions`, async (route) => {
    const response = await route.fetch(); revisionArrived(); await revisionRelease; await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存新版本", exact: true }).click();
  await revisionArrival;
  await page.getByRole("button", { name: b.title, exact: true }).click();
  await expect(page.getByRole("heading", { name: b.title, exact: true })).toBeVisible();
  // Returning to the same research must not make the old navigation valid again.
  await page.getByRole("button", { name: a.title, exact: true }).click();
  await expect(page.getByLabel("研究版本", { exact: true }).getByRole("option")).toHaveCount(2);
  await page.getByLabel("研究版本", { exact: true }).selectOption(a.latest_revision_id);
  releaseRevision();
  await page.getByRole("button", { name: "安全重试修订提交", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本 2 已保存");
  await expect(page.getByRole("heading", { name: a.title, exact: true })).toBeVisible();
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(a.latest_revision_id);
  const saved = await (await request.get(`/api/researches/${a.id}`)).json();
  expect(saved.revisions).toHaveLength(2);
  expect(saved.revisions.at(-1).task.budget.max_tool_calls).toBe(30);
});
