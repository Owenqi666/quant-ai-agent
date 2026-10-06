import { expect, test } from "@playwright/test";

const key = (workspace: string) => `paper-alpha:selection:v1:${workspace}`;

test("an unavailable legacy selection falls back to a verified research in this workspace", async ({ page, request }) => {
  await request.post("/api/examples/alpha101", { data: {} });
  const health = await (await request.get("/api/health")).json();
  const first = (await (await request.get("/api/catalog/researches?limit=50")).json()).items[0];
  await page.addInitScript(() => localStorage.setItem("paper-alpha-research", "old-workspace-missing-research"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: first.title, exact: true })).toBeVisible();
  await expect(page.getByText(/原先选择的研究不在此工作区/)).toBeVisible();
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(health.workspace_id))).toBe(first.id);
  await expect(page.getByText("正在读取研究版本…", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "无法恢复此工作区的研究选择", exact: true })).toHaveCount(0);
});

test("a saved historical research beyond the first page is restored by identity", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const firstPage = await (await request.get("/api/catalog/researches?limit=50")).json();
  for (let i = firstPage.total_records; i < 51; i += 1) {
    await request.post("/api/researches", { data: { title: `Selection boundary fixture ${i}`, paper_id: original.paper_id, dataset_id: original.dataset_id, task: original.revisions[0].task } });
  }
  const historical = await (await request.post("/api/researches", { data: { title: "Saved selection outside the first page", paper_id: original.paper_id, dataset_id: original.dataset_id, task: original.revisions[0].task } })).json();
  const currentPage = await (await request.get("/api/catalog/researches?limit=50")).json();
  expect(currentPage.items.some((row: { id: string }) => row.id === historical.id)).toBe(false);
  const health = await (await request.get("/api/health")).json();
  await page.addInitScript(({ storageKey, id }) => localStorage.setItem(storageKey, id), { storageKey: key(health.workspace_id), id: historical.id });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: historical.title, exact: true })).toBeVisible();
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(health.workspace_id))).toBe(historical.id);
});

test("a non-404 restore failure retains the intended selection and offers a retry", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const health = await (await request.get("/api/health")).json();
  await page.addInitScript(({ storageKey, id }) => localStorage.setItem(storageKey, id), { storageKey: key(health.workspace_id), id: original.id });
  let unavailable = true;
  await page.route(`**/api/researches/${original.id}`, async (route) => {
    if (unavailable) await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Injected temporary read failure" }) });
    else await route.continue();
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "无法恢复此工作区的研究选择", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toHaveCount(0);
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(health.workspace_id))).toBe(original.id);
  unavailable = false;
  await page.getByRole("button", { name: "重试恢复研究选择", exact: true }).click();
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "无法恢复此工作区的研究选择", exact: true })).toHaveCount(0);
});

test("a simulated workspace identity switch discards a late response from the old selection", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const next = await (await request.post("/api/researches", { data: { title: "Workspace switch target", paper_id: original.paper_id, dataset_id: original.dataset_id, task: original.revisions[0].task } })).json();
  const health = await (await request.get("/api/health")).json();
  const nextWorkspace = `${health.workspace_id}-simulated-restart`;
  await page.addInitScript(({ oldKey, oldId, nextKey, nextId }) => {
    localStorage.setItem(oldKey, oldId); localStorage.setItem(nextKey, nextId);
  }, { oldKey: key(health.workspace_id), oldId: original.id, nextKey: key(nextWorkspace), nextId: next.id });
  let switched = false;
  await page.route("**/api/health", async (route) => {
    const response = await route.fetch();
    if (!switched) { await route.fulfill({ response }); return; }
    // Transport fault injection only: scientific payloads remain real server responses.
    const body = await response.json(); body.workspace_id = nextWorkspace;
    await route.fulfill({ response, json: body });
  });
  let releaseOld!: () => void, oldArrived!: () => void;
  const held = new Promise<void>((resolve) => { releaseOld = resolve; });
  const arrived = new Promise<void>((resolve) => { oldArrived = resolve; });
  await page.route(`**/api/researches/${original.id}`, async (route) => {
    const response = await route.fetch(); oldArrived(); await held;
    await route.fulfill({ response }).catch(() => {}); // Aborted reads are expected after the switch.
  });
  await page.goto("/");
  await arrived;
  switched = true;
  await expect(page.getByRole("heading", { name: next.title, exact: true })).toBeVisible();
  releaseOld();
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toHaveCount(0);
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(nextWorkspace))).toBe(next.id);
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(health.workspace_id))).toBe(original.id);
});

test("a delayed example refresh cannot overwrite a newer workspace revision with the same research UUID", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const health = await (await request.get("/api/health")).json();
  const nextWorkspace = `${health.workspace_id}-same-id-restart`;
  await page.addInitScript(({ oldKey, nextKey, id }) => {
    localStorage.setItem(oldKey, id); localStorage.setItem(nextKey, id);
  }, { oldKey: key(health.workspace_id), nextKey: key(nextWorkspace), id: original.id });
  let switched = false;
  await page.route("**/api/health", async (route) => {
    const response = await route.fetch();
    if (!switched) { await route.fulfill({ response }); return; }
    const body = await response.json(); body.workspace_id = nextWorkspace;
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toBeVisible();
  let releaseOld!: () => void, oldArrived!: () => void;
  const held = new Promise<void>((resolve) => { releaseOld = resolve; });
  const arrived = new Promise<void>((resolve) => { oldArrived = resolve; });
  let captured = false;
  await page.route(`**/api/researches/${original.id}`, async (route) => {
    if (captured) { await route.continue(); return; }
    captured = true;
    const response = await route.fetch(); oldArrived(); await held;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.getByRole("button", { name: "导入示例研究", exact: true }).first().click();
  await arrived;
  const currentRevision = original.revisions.find((row: { id: string }) => row.id === original.latest_revision_id);
  const updatedTask = structuredClone(currentRevision.task);
  updatedTask.budget.max_tool_calls += 1;
  const revisionResponse = await request.post(`/api/researches/${original.id}/revisions`, { data: {
    base_revision_id: original.latest_revision_id, task: updatedTask, note: "Identity-fencing fixture: newer revision with the same research UUID.",
  } });
  expect(revisionResponse.status()).toBe(201);
  const newest = await revisionResponse.json();
  switched = true;
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(newest.id);
  releaseOld();
  await expect(page.getByRole("button", { name: "导入示例研究", exact: true }).first()).toBeEnabled();
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(newest.id);
  expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key(nextWorkspace))).toBe(original.id);
});

test("a temporary health failure keeps the confirmed workspace editor and in-memory draft", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const health = await (await request.get("/api/health")).json();
  await page.addInitScript(({ storageKey, id }) => localStorage.setItem(storageKey, id), { storageKey: key(health.workspace_id), id: original.id });
  let unavailable = false;
  await page.route("**/api/health", async (route) => {
    if (unavailable) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Temporary health probe failure" }) });
    else await route.continue();
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "创建新版本", exact: true }).click();
  await page.getByLabel("修改说明", { exact: true }).fill("Unsubmitted work survives a health probe failure.");
  unavailable = true;
  await expect(page.getByText("服务未连接", { exact: true })).toBeVisible();
  await expect(page.getByLabel("修改说明", { exact: true })).toHaveValue("Unsubmitted work survives a health probe failure.");
  await expect(page.locator(".revision-editor")).toHaveCount(1);
  unavailable = false;
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await expect(page.getByLabel("修改说明", { exact: true })).toHaveValue("Unsubmitted work survives a health probe failure.");
});

test("the workbench still opens when browser selection storage is unavailable", async ({ page, request }) => {
  await request.post("/api/examples/alpha101", { data: {} });
  const first = (await (await request.get("/api/catalog/researches?limit=50")).json()).items[0];
  await page.addInitScript(() => Object.defineProperty(window, "localStorage", { configurable: true, get() { throw new DOMException("Blocked by test policy", "SecurityError"); } }));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: first.title, exact: true })).toBeVisible();
  await expect(page.getByText("服务已连接", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "无法恢复此工作区的研究选择", exact: true })).toHaveCount(0);
});

test("returning to workspace A does not revive a delayed callback from its previous generation", async ({ page, request }) => {
  const imported = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const original = await (await request.get(`/api/researches/${imported.research_id}`)).json();
  const health = await (await request.get("/api/health")).json();
  const workspaceB = `${health.workspace_id}-a-b-a-fixture`;
  let observedWorkspace = health.workspace_id;
  await page.addInitScript(({ keyA, keyB, id }) => {
    localStorage.setItem(keyA, id); localStorage.setItem(keyB, id);
  }, { keyA: key(health.workspace_id), keyB: key(workspaceB), id: original.id });
  await page.route("**/api/health", async (route) => {
    const response = await route.fetch();
    const body = await response.json(); body.workspace_id = observedWorkspace;
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: original.title, exact: true })).toBeVisible();
  let releaseOld!: () => void, oldArrived!: () => void;
  const held = new Promise<void>((resolve) => { releaseOld = resolve; });
  const arrived = new Promise<void>((resolve) => { oldArrived = resolve; });
  let captured = false;
  await page.route(`**/api/researches/${original.id}`, async (route) => {
    if (captured) { await route.continue(); return; }
    captured = true;
    const response = await route.fetch(); oldArrived(); await held;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.getByRole("button", { name: "导入示例研究", exact: true }).first().click();
  await arrived;
  const updatedTask = structuredClone(original.revisions.find((row: { id: string }) => row.id === original.latest_revision_id).task);
  updatedTask.budget.max_tool_calls += 1;
  const newer = await request.post(`/api/researches/${original.id}/revisions`, { data: {
    base_revision_id: original.latest_revision_id, task: updatedTask, note: "A-B-A callback fencing fixture; no scientific result claim.",
  } });
  expect(newer.status()).toBe(201);
  const revisionB = await newer.json();
  observedWorkspace = workspaceB;
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(revisionB.id);
  // A further real revision lets us observe that the return to A has been applied.
  const taskA = structuredClone(updatedTask); taskA.budget.max_tool_calls += 1;
  const responseA = await request.post(`/api/researches/${original.id}/revisions`, { data: {
    base_revision_id: revisionB.id, task: taskA, note: "Return to A after a confirmed intermediate workspace identity.",
  } });
  expect(responseA.status()).toBe(201);
  const revisionA = await responseA.json();
  observedWorkspace = health.workspace_id;
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(revisionA.id);
  releaseOld();
  await expect(page.getByRole("button", { name: "导入示例研究", exact: true }).first()).toBeEnabled();
  await expect(page.getByLabel("研究版本", { exact: true })).toHaveValue(revisionA.id);
});

test("healthy probes preserve experiment polling and workspace changes restart its event cursor", async ({ page, request }) => {
  const example = await (await request.post("/api/examples/alpha101", { data: {} })).json();
  const submitted = await request.post("/api/runs", { data: {
    revision_id: example.revision_id, mode: "normalized_fixed", idempotency_key: "v07-workspace-monitor-fixture",
  } });
  expect(submitted.status()).toBe(201);
  const run = await submitted.json();
  await expect.poll(async () => (await (await request.get(`/api/runs/${run.id}`)).json()).status, { timeout: 30000 }).toBe("completed");
  const health = await (await request.get("/api/health")).json();
  const workspaceB = `${health.workspace_id}-monitor-fixture`;
  let observedWorkspace = health.workspace_id;
  let healthProbes = 0, artifactReads = 0;
  const initialCursorReads: string[] = [];
  await page.addInitScript(({ keyA, keyB, id }) => {
    localStorage.setItem(keyA, id); localStorage.setItem(keyB, id);
  }, { keyA: key(health.workspace_id), keyB: key(workspaceB), id: example.research_id });
  await page.route("**/api/health", async (route) => {
    // Retry only ECONNRESET at the idle keep-alive edge; HTTP errors still fail.
    const response = await route.fetch({ maxRetries: 1 });
    const body = await response.json(); body.workspace_id = observedWorkspace;
    healthProbes += 1;
    await route.fulfill({ response, json: body });
  });
  await page.route(`**/api/runs/${run.id}/artifacts`, async (route) => { artifactReads += 1; await route.continue(); });
  await page.route(`**/api/runs/${run.id}/events/page?*`, async (route) => {
    if (new URL(route.request().url()).searchParams.get("after") === "0") initialCursorReads.push(observedWorkspace);
    await route.continue();
  });
  await page.goto("/");
  await page.getByRole("button", { name: /^实验记录/ }).click();
  await page.getByRole("button", { name: `查看实验 ${run.id.slice(0, 8)}`, exact: true }).click();
  await expect(page.getByText(/^已同步至事件快照 /)).toBeVisible();
  await expect.poll(() => artifactReads).toBe(1);
  expect(initialCursorReads).toEqual([health.workspace_id]);
  const beforeProbes = healthProbes;
  await expect.poll(() => healthProbes).toBeGreaterThan(beforeProbes);
  await expect(page.getByText(/^已同步至事件快照 /)).toBeVisible();
  expect(initialCursorReads).toEqual([health.workspace_id]);
  expect(artifactReads).toBe(1);
  observedWorkspace = workspaceB;
  // The selected experiment is cleared as the newly confirmed workspace restores.
  await expect(page.getByRole("heading", { name: "事件时间线", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: `查看实验 ${run.id.slice(0, 8)}`, exact: true }).click();
  await expect(page.getByText(/^已同步至事件快照 /)).toBeVisible();
  expect(initialCursorReads).toEqual([health.workspace_id, workspaceB]);
  await expect.poll(() => artifactReads).toBe(2);
});
