import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "./api";
import { browserSelectionStorage, isMissingResearch, MissingResearchSelection, persistResearchSelection, resolveResearchSelection, savedResearchSelection } from "./researchSelection";
import {
  activeStatuses,
  describeError,
  editableTask,
  formatDate,
  modeName,
  shortId,
} from "./domain";
import type {
  Dataset,
  Health,
  JsonObject,
  Paper,
  RegressionCase,
  RegressionCheck,
  Research,
  Revision,
  Run,
  Task,
} from "./domain";
import { Badge, Empty, JsonDetails } from "./components/ui";
import RunDetail from "./components/RunDetail";
import QualityPanel from "./components/QualityPanel";
import CreateResearch from "./components/CreateResearch";
import RevisionSubmission from "./components/RevisionSubmission";
import { useWorkspaceHealth } from "./useWorkspaceHealth";
import { useExperimentMonitor } from "./useExperimentMonitor";
import { catalogPath } from "./feedback";
import type { CatalogPage, CatalogResource } from "./feedback";
import CatalogPanel from "./components/CatalogPanel";
import FeedbackPanel from "./components/FeedbackPanel";
import RunSubmission from "./components/RunSubmission";
import ResearchInsights from "./components/ResearchInsights";
import ResearchProtocols from "./components/ResearchProtocols";
import MonthlyExperiments from "./components/MonthlyExperiments";
import AuthorPanels from "./components/AuthorPanels";
import AuthorStudies from "./components/AuthorStudies";
import ResearchCases from "./components/ResearchCases";
import ResearchJobs from "./components/ResearchJobs";
import DomainResearchJobs from "./components/DomainResearchJobs";
import IndustryMomExperiments from "./components/IndustryMomExperiments";
import type {ResearchCaseSource} from "./researchCases";
import SemanticMaterials from "./components/SemanticMaterials";
import WorkflowObservations from "./components/WorkflowObservations";
import DailyWorkflow from "./components/DailyWorkflow";
import type { DailyStep } from "./components/DailyWorkflow";
import { retainSelected, selectedResearchRun } from "./dailyWorkflow";

type Section = "research" | "experiments" | "quality" | "catalog" | "insights" | "observations" | "protocols" | "monthly" | "author-panels" | "author-studies" | "research-cases" | "research-jobs" | "semantic-materials";

export default function App() {
  const [sourceTarget, setSourceTarget] = useState({kind: "", id: ""});
  const sourceNavigationRequest = useRef(0);
  const [section, setSection] = useState<Section>("research");
  const [researches, setResearches] = useState<Research[]>([]);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [cases, setCases] = useState<RegressionCase[]>([]);
  const [checks, setChecks] = useState<RegressionCheck[]>([]);
  const { health, workspaceId, workspaceScope, beginHealthRequest, acceptHealth } = useWorkspaceHealth();
  const workspaceKey = workspaceScope.key;
  const [selectedResearch, setSelectedResearch] = useState("");
  const [selectionWorkspace, setSelectionWorkspace] = useState("");
  const [selectionError, setSelectionError] = useState("");
  const [selectionRetry, setSelectionRetry] = useState(0);
  const [researchLoadError, setResearchLoadError] = useState("");
  const [researchRetry, setResearchRetry] = useState(0);
  const workspaceReady = !!workspaceId && selectionWorkspace === workspaceId;
  const [research, setResearch] = useState<Research | null>(null);
  const [revisionId, setRevisionId] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [createSeed, setCreateSeed] = useState<{ task: Task; paperId: string; nonce: string; scope: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [selectedIssueId, setSelectedIssueId] = useState("");
  const dailyScope = `${workspaceKey}:${selectedResearch}`;
  const dailyScopeRef = useRef(dailyScope);
  dailyScopeRef.current = dailyScope;
  const [observationRecording, setObservationRecording] = useState({ scope: "", value: false });
  const [reviewTimer, setReviewTimer] = useState({ scope: "", value: false });
  const recording = observationRecording.scope === dailyScope && observationRecording.value;
  const reviewTimerRunning = reviewTimer.scope === dailyScope && reviewTimer.value;
  const onRecordingChange = useCallback((value: boolean) => {
    if (dailyScopeRef.current === dailyScope) setObservationRecording({ scope: dailyScope, value });
  }, [dailyScope]);
  const onReviewTimerRunningChange = useCallback((value: boolean) => {
    if (dailyScopeRef.current === dailyScope) setReviewTimer({ scope: dailyScope, value });
  }, [dailyScope]);
  const selectedRunRef = useRef(selectedRun);
  selectedRunRef.current = selectedRun;
  const [windowTotals, setWindowTotals] = useState({ researches: 0, runs: 0, cases: 0, checks: 0 });
  const researchSelection = useRef(selectedResearch);
  researchSelection.current = selectedResearch;
  const pendingOpen = useRef<{ researchId: string; runId?: string; issueId?: string; regressionCase?: RegressionCase } | null>(null);
  const updateMonitoredRun = useCallback((result: Run) => {
    setRuns((old) => old.some((item) => item.id === result.id) ? old.map((item) => item.id === result.id ? result : item) : [result, ...old].slice(0, 51));
  }, []);
  const { run, artifacts, eventFeed, reset: resetExperiment } = useExperimentMonitor({
    runId: workspaceReady ? selectedRun : "", workspaceScope, refreshNonce, onRun: updateMonitoredRun, onError: setError,
  });
  const revision =
    research?.revisions?.find((r) => r.id === revisionId) ||
    research?.revisions?.find((r) => r.id === research.latest_revision_id) ||
    research?.revisions?.at(-1);
  const task = revision?.task;
  const revisionSelection = useRef(revision?.id);
  revisionSelection.current = revision?.id;
  const navigationEpoch = useRef(0);
  const operationEpoch=useRef(0);
  useEffect(() => { navigationEpoch.current += 1; }, [selectedResearch, section, showCreate, revisionId, editing, workspaceKey]);
  const currentRun = selectedResearchRun(selectedResearch, selectedRun, run);
  const researchRuns = retainSelected(runs.filter((r) => r.research_id === selectedResearch), currentRun);
  const currentPaper = papers.find((p) => p.id === research?.paper_id);
  const currentDataset = datasets.find((d) => d.id === research?.dataset_id);

  const refresh = useCallback(async () => {
    const scopeSnapshot = workspaceScope.capture();
    const healthSequence = beginHealthRequest();
    const scope = researchSelection.current;
    const filter = scope ? { research_id: scope } : {};
    const [r, p, d, rs, cs, ck, h] = await Promise.all([
      api<CatalogPage<Research>>(catalogPath("researches")),
      api<Paper[]>("/papers"),
      api<Dataset[]>("/datasets"),
      api<CatalogPage<Run>>(catalogPath("runs", filter)),
      api<CatalogPage<RegressionCase>>(catalogPath("regression-cases", filter)),
      api<CatalogPage<RegressionCheck>>(catalogPath("regression-checks", filter)),
      api<Health>("/health"),
    ]);
    if (!workspaceScope.isCurrent(scopeSnapshot)) return r.items;
    setResearches((old) => {
      const selected = old.find((item) => item.id === researchSelection.current);
      return selected && !r.items.some((item) => item.id === selected.id) ? [...r.items, selected] : r.items;
    });
    setPapers(p);
    setDatasets(d);
    if (scope === researchSelection.current) {
      setRuns((old) => retainSelected(rs.items, old.find((item) => item.id === selectedRunRef.current && item.research_id === scope)));
      setCases(cs.items);
      setChecks(ck.items);
      setWindowTotals({ researches: r.total_records, runs: rs.total_records, cases: cs.total_records, checks: ck.total_records });
    }
    acceptHealth(healthSequence, h);
    setLoaded(true);
    return r.items;
  }, [workspaceScope, beginHealthRequest, acceptHealth]);
  useEffect(() => {
    let active = true;
    refresh()
      .catch((e) => {
        if (active) {
          setError(describeError(e));
          setLoaded(true);
        }
      });
    return () => {
      active = false;
    };
  }, [refresh]);
  useEffect(() => {
    if (!workspaceId) return;
    const abort = new AbortController();
    const scopeSnapshot = workspaceScope.capture();
    const current = () => workspaceScope.isCurrent(scopeSnapshot, abort.signal);
    setSelectionWorkspace(""); setSelectionError(""); setResearchLoadError("");
    researchSelection.current = "";
    setSelectedResearch(""); setResearch(null); setRevisionId("");
    setSelectedRun(""); resetExperiment(); setSelectedIssueId("");
    setResearches([]); setPapers([]); setDatasets([]); setRuns([]); setCases([]); setChecks([]);
    setShowCreate(false); setEditing(false); setCreateSeed(null);
    operationEpoch.current+=1;setBusy("");setError("");setNotice("");
    pendingOpen.current = null;
    async function restoreSelection() {
      try {
        const firstPage = await refresh();
        if (!current()) return;
        const choice = await resolveResearchSelection(savedResearchSelection(browserSelectionStorage(), workspaceId), firstPage,
          (id) => api<Research>(`/researches/${encodeURIComponent(id)}`, { signal: abort.signal }));
        if (!current()) return;
        const id = choice.research?.id || "";
        persistResearchSelection(browserSelectionStorage(), workspaceId, id);
        setSelectedResearch(id); setSelectionWorkspace(workspaceId);
        if (choice.fellBack) setNotice("原先选择的研究不在此工作区，已返回可用研究；其他工作区的记录保持独立。");
      } catch (error) { if (current()) setSelectionError(describeError(error)); }
    }
    void restoreSelection();
    return () => abort.abort();
  }, [workspaceId, workspaceKey, selectionRetry, refresh, workspaceScope, resetExperiment]);
  useEffect(() => {
    setResearch(null); setResearchLoadError("");
    setRevisionId(""); resetExperiment(); setSelectedRun("");
    setSelectedIssueId(""); setEditing(false);
    if (!selectedResearch || selectionWorkspace !== workspaceId) return;
    const abort = new AbortController();
    const scopeSnapshot = workspaceScope.capture();
    const current = () => workspaceScope.isCurrent(scopeSnapshot, abort.signal);
    const detail = api<Research>(`/researches/${encodeURIComponent(selectedResearch)}`, { signal: abort.signal }).catch((error) => {
      if (isMissingResearch(error)) throw new MissingResearchSelection();
      throw error;
    });
    Promise.all([
      detail,
      api<CatalogPage<Run>>(catalogPath("runs", { research_id: selectedResearch }), { signal: abort.signal }),
      api<CatalogPage<RegressionCase>>(catalogPath("regression-cases", { research_id: selectedResearch }), { signal: abort.signal }),
      api<CatalogPage<RegressionCheck>>(catalogPath("regression-checks", { research_id: selectedResearch }), { signal: abort.signal }),
    ]).then(([r, rs, cs, ck]) => {
        if (!current()) return;
        persistResearchSelection(browserSelectionStorage(), workspaceId, r.id);
        setResearch(r); setRevisionId(r.latest_revision_id);
        setResearches((old) => old.some((item) => item.id === r.id) ? old : [...old.slice(0, 50), r]);
        setRuns(rs.items); setCases(cs.items); setChecks(ck.items);
        setWindowTotals((old) => ({ ...old, runs: rs.total_records, cases: cs.total_records, checks: ck.total_records }));
        const pending = pendingOpen.current;
        if (pending?.researchId === r.id) {
          if (pending.runId) setSelectedRun(pending.runId);
          if (pending.issueId) setSelectedIssueId(pending.issueId);
          if (pending.regressionCase) setCases((old) => [pending.regressionCase!, ...old.filter((item) => item.id !== pending.regressionCase!.id)].slice(0, 51));
          pendingOpen.current = null;
        }
      })
      .catch((error) => {
        if (!current()) return;
        if (error instanceof MissingResearchSelection) {
          persistResearchSelection(browserSelectionStorage(), workspaceId, "");
          setNotice("所选研究已不可用，正在重新读取此工作区的研究目录。");
          setSelectionRetry((value) => value + 1);
        } else setResearchLoadError(describeError(error));
      });
    return () => abort.abort();
  }, [selectedResearch, selectionWorkspace, workspaceId, workspaceKey, researchRetry, workspaceScope, resetExperiment]);

  async function perform(label: string, action: () => Promise<void>) {
    if (busy) return;
    const workspace=workspaceScope.capture(),epoch=++operationEpoch.current;
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      if(workspaceScope.isCurrent(workspace)&&epoch===operationEpoch.current)setError(describeError(e));
    } finally {
      if(workspaceScope.isCurrent(workspace)&&epoch===operationEpoch.current)setBusy("");
    }
  }
  async function importDemo() {
    await perform("导入示例", async () => {
      // Navigate when the user starts the action, not after slow HTTP requests:
      // a later explicit navigation must not be undone by this callback.
      const previousResearch = researchSelection.current;
      const previousWorkspace = workspaceScope.capture();
      setSection("research");
      setShowCreate(false);
      const result = await post<{ research_id: string; revision_id: string }>(
        "/examples/alpha101",
        {},
      );
      await refresh();
      if (!workspaceScope.isCurrent(previousWorkspace)) return;
      if (researchSelection.current !== previousResearch && researchSelection.current !== result.research_id) {
        setNotice("示例材料已导入；当前选择的研究保持不变。");
        return;
      }
      setSelectedResearch(result.research_id);
      if (researchSelection.current === result.research_id) {
        const updated = await api<Research>(`/researches/${result.research_id}`);
        if (researchSelection.current === result.research_id && workspaceScope.isCurrent(previousWorkspace)) {
          setResearch(updated);
          setRevisionId(result.revision_id);
        }
      }
      setNotice("示例已就绪。请先核对论文证据与候选，再提交实验。");
    });
  }
  async function revisionSaved(newRevision:Revision,baseRevisionId:string) {
    if(!research)return;
    const startedNavigation=navigationEpoch.current,workspace=workspaceScope.capture();
      const updated = await api<Research>(`/researches/${research.id}`);
      if (workspaceScope.isCurrent(workspace) && researchSelection.current === research.id && navigationEpoch.current === startedNavigation) {
        setResearch(updated);
        if (revisionSelection.current === baseRevisionId) {
          setRevisionId(newRevision.id);
          setEditing(false);
        }
      }
      if(workspaceScope.isCurrent(workspace))await refresh();
  }
  function navigateSection(next: Section) {
    if (reviewTimerRunning && next !== "quality") {
      setError("请先暂停主动工作计时，再离开审核或开始流程观测；已结束区间会保留在审核草稿中。");
      return;
    }
    setSection(next); setShowCreate(false);
  }
  function dailyStep(step: DailyStep) {
    const next = step === "review" ? "quality" : step === "report" ? "insights" : "research";
    navigateSection(next);
    if (!reviewTimerRunning && (step === "run" || step === "confirm")) {
      window.requestAnimationFrame(() => document.getElementById(step === "run" ? "daily-run-submission" : "daily-candidates")?.scrollIntoView({ block: "start" }));
    }
  }
  async function openCaseSource(kind: ResearchCaseSource, id: string) {
    const captured = workspaceScope.capture();
    const navigation = navigationEpoch.current, request = ++sourceNavigationRequest.current;
    const current = () => workspaceScope.isCurrent(captured) && navigationEpoch.current === navigation && sourceNavigationRequest.current === request;
    if (reviewTimerRunning) {setError("请先暂停审核计时，再切换结果。"); return;}
    if (kind === "daily_run") {
      try {
        const value = await api<Run>(`/runs/${encodeURIComponent(id)}`);
        if (current()) openCatalog("runs", {id:value.id,research_id:value.research_id});
      } catch(e) {if(current())setError(describeError(e));}
    } else if (kind === "industry_mom_experiment") {
      setSourceTarget({kind,id}); navigateSection("research-jobs");
    } else if (kind === "author_study" || kind === "monthly_experiment") {
      setSourceTarget({kind,id}); navigateSection(kind === "author_study" ? "author-studies" : "monthly");
    } else {
      setError("未知研究来源，无法打开准确结果。");
    }
  }
  function openIndustryCase(id: string) {
    if (reviewTimerRunning) { setError("请先暂停审核计时，再切换研究任务。"); return; }
    setSourceTarget({kind: "research_case", id}); navigateSection("research-cases");
  }
  function openRun(id: string) {
    setSelectedRun(id);
    setSection("experiments");
  }
  function openCatalog(resource: CatalogResource, row: JsonObject) {
    setShowCreate(false);
    const id = String(resource === "researches" ? row.id : row.research_id);
    const pending = { researchId: id,
      runId: resource === "runs" ? String(row.id) : undefined,
      issueId: resource === "issues" ? String(row.id) : undefined,
      regressionCase: resource === "regression-cases" ? row as unknown as RegressionCase : undefined };
    setSection(resource === "researches" ? "research" : resource === "runs" ? "experiments" : "quality");
    if (id !== selectedResearch) {
      pendingOpen.current = pending;
      setSelectedResearch(id);
    } else {
      if (pending.runId) setSelectedRun(pending.runId);
      if (pending.issueId) setSelectedIssueId(pending.issueId);
      if (pending.regressionCase) setCases((old) => [pending.regressionCase!, ...old.filter((item) => item.id !== pending.regressionCase!.id)].slice(0, 51));
    }
  }
  const terminalRuns = researchRuns.filter(
    (r) => !activeStatuses.has(r.status),
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        跳转到工作区
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#main"
          onClick={() => navigateSection("research")}
        >
          Paper-to-Alpha
        </a>
        <div className="workspace-tag">本地研究空间</div>
        <nav aria-label="主导航">
          {(
            [
              { id: "research", icon: "book", name: "研究工作台" },
              { id: "research-jobs", icon: "experiment", name: "研究执行" },
              { id: "research-cases", icon: "book", name: "研究任务" },
              { id: "semantic-materials", icon: "book", name: "语义评测" },
              { id: "protocols", icon: "book", name: "研究规则" },
              { id: "monthly", icon: "experiment", name: "月度实验" },
              { id: "author-panels", icon: "book", name: "作者数据" },
              { id: "author-studies", icon: "book", name: "研究准入" },
              { id: "experiments", icon: "experiment", name: "实验记录" },
              { id: "quality", icon: "shield", name: "审核与回归" },
              { id: "insights", icon: "experiment", name: "研究对比与总结" },
              { id: "observations", icon: "experiment", name: "流程观测" },
              { id: "catalog", icon: "book", name: "历史目录" },
            ] as const
          ).map((item) => (
            <button
              key={item.id}
              className={`nav-item ${section === item.id ? "active" : ""}`}
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => navigateSection(item.id)}
            >
              {item.name}
              {item.id === "experiments" && (
                <span className="nav-count">{researchRuns.length}</span>
              )}
            </button>
          ))}
        </nav>
        <p className="fine-print">流程观测为可选对照记录，日常研究可直接运行。</p>
        <div className="sidebar-heading">
          <span>研究项目</span>
          <button
            className="icon-button"
            aria-label="新建研究"
            disabled={!workspaceReady}
            onClick={() => {
              setShowCreate(true);
              setCreateSeed(null);
              setSection("research");
            }}
          >
            新建
          </button>
        </div>
        <div className="research-list">
          {researches.length ? (
            researches.map((r) => (
              <button
                key={r.id}
                className={`research-link ${r.id === selectedResearch ? "selected" : ""}`}
                disabled={!workspaceReady}
                onClick={() => {
                  setSelectedResearch(r.id);
                  setSection("research");
                  setShowCreate(false);
                }}
              >
                <span className="research-dot" />
                <span>{r.title}</span>
              </button>
            ))
          ) : (
            <p className="sidebar-hint">导入一篇论文，开始你的第一个研究。</p>
          )}
        </div>
        <p className="sidebar-bottom">本地单用户 · AI 接口预留</p>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="muted">工作空间</span>
            <span className="breadcrumb-sep">/</span>
            <span>
              {
                {
                  research: "研究工作台",
                  protocols: "研究规则",
                  monthly: "月度实验",
                  "author-panels": "作者数据",
                  "author-studies": "研究准入",
                  "research-cases": "研究任务",
                  "research-jobs": "研究执行",
                  "semantic-materials": "语义评测",
                  experiments: "实验记录",
                  quality: "审核与回归",
                  catalog: "历史目录",
                  insights: "研究对比与总结",
                  observations: "流程观测",
                }[section]
              }
            </span>
          </div>
          <div className="topbar-status">
            <span className={`connection-dot ${health ? "online" : ""}`} />
            <span>{health ? "服务已连接" : "服务未连接"}</span>
            <span className="top-divider" />
            <span
              className={`connection-dot ${health?.worker.online ? "online" : ""}`}
            />
            <span>{health?.worker.online ? "Worker 在线" : "Worker 离线"}</span>
            <button
              className="icon-button"
              aria-label="刷新工作台"
              disabled={!!busy}
              onClick={() =>
                void perform("刷新", async () => {
                  const scopeSnapshot = workspaceScope.capture();
                  await refresh();
                  if (selectedResearch) {
                    const updated = await api<Research>(`/researches/${selectedResearch}`);
                    if (workspaceScope.isCurrent(scopeSnapshot) && researchSelection.current === selectedResearch) setResearch(updated);
                  }
                  setRefreshNonce((n) => n + 1);
                })
              }
            >
              刷新
            </button>
          </div>
        </header>
        <main id="main">
          <div className="page-heading">
            <div>
              <h1>
                {
                  {
                    research: "研究工作台",
                    protocols: "研究规则",
                    monthly: "月度实验",
                    "author-panels": "作者数据",
                    "author-studies": "研究准入",
                    "research-cases": "研究任务",
                  "research-jobs": "研究执行",
                  "semantic-materials": "语义评测",
                    experiments: "实验记录",
                    quality: "审核与回归",
                    catalog: "历史目录",
                  insights: "研究对比与总结",
                  observations: "流程观测",
                  }[section]
                }
              </h1>
              <p>
                {
                  {
                    research: "整理论文依据，定义候选，保留每次研究判断。",
                    protocols: "区分论文定义与项目约定，展开时间窗口并检查缺失观察。",
                    monthly: "执行冻结月度规则，检查权重、标签覆盖、代理指标与审核记录。",
                    "author-panels": "导入有出处的作者月度面板，核对原行、时间对齐及不可用原因。",
                    "author-studies": "冻结研究计划，检查连续区间的全原行资格，并保留审核与修订依据。",
                    "research-cases": "关联依据与实际结果，核对阻塞原因，记录有界的下一步建议。",
                    "research-jobs": "执行固定行业 MOM 真实来源案例或既有受控任务，核对每次实际输出与研究范围。",
                    "semantic-materials": "阅读固定论文材料，留下自己的判断和理由，保留全部标注历史。",
                    experiments:
                      "追踪任务状态、计算结果与失败原因，保留完整执行过程。",
                    quality:
                      "冻结输出，记录人工判断，再用明确的预期检查下一版。",
                    catalog: "按条件分页读取研究、实验、审核、回归和问题记录。",
                    insights: "核对实验条件、比较实际结果，并保存可检查的研究总结。",
                    observations: "记录六阶段实际观察，核对计时覆盖和同条件的人工流程对照。",
                  }[section]
                }
              </p>
            </div>
            {section!=="protocols"&&section!=="monthly"&&section!=="author-panels"&&section!=="author-studies"&&section!=="research-cases"&&section!=="research-jobs"&&section!=="semantic-materials"&&<div className="button-row">
              <button
                className="button"
                disabled={!workspaceReady}
                onClick={() => {
                  setShowCreate(true);
                  setCreateSeed(null);
                  setSection("research");
                }}
              >
                新建研究
              </button>
              {task && research && <button className="button" onClick={() => {
                setCreateSeed({ task: editableTask(task), paperId: research.paper_id, nonce: crypto.randomUUID(), scope: `${research.id}:${revision?.id}` });
                setShowCreate(true);
                setSection("research");
              }}>复制当前任务新建研究</button>}
              <button
                className="button primary"
                disabled={!!busy || !workspaceReady}
                onClick={() => void importDemo()}
              >
                {busy === "导入示例" ? "导入中…" : "导入示例研究"}
              </button>
            </div>}
          </div>
          {error && (
            <div className="banner error" role="alert">
              <strong>操作未完成</strong>
              <span>{error}</span>
              <button
                className="text-button"
                onClick={() => setError("")}
                aria-label="关闭错误"
              >
                关闭
              </button>
            </div>
          )}
          {notice && (
            <div className="banner success" role="status">
              <span>{notice}</span>
              <button
                className="text-button"
                onClick={() => setNotice("")}
                aria-label="关闭提示"
              >
                关闭
              </button>
            </div>
          )}
          {section!=="protocols"&&section!=="monthly"&&section!=="author-panels"&&section!=="author-studies"&&section!=="research-cases"&&section!=="research-jobs"&&section!=="semantic-materials"&&<div className="scope-strip">
            <span className="scope-label">当前范围</span>
            <span>人工定义候选</span>
            <i />
            <span>合成数据验证</span>
            <i />
            <span>最终测试区间隔离</span>
            <span className="scope-end">AI 未启用</span>
          </div>}
          {loaded && section !== "catalog" && section!=="protocols" && section!=="monthly" && section!=="author-panels" && section!=="author-studies" && section!=="research-cases" && section!=="research-jobs"&&section!=="semantic-materials" && <p className="fine-print">
            工作区只展示有界记录窗口：研究 {researches.length}/{windowTotals.researches}；当前研究实验 {researchRuns.length}/{windowTotals.runs}、案例 {cases.length}/{windowTotals.cases}、检查 {checks.length}/{windowTotals.checks}。
            <button className="text-button" onClick={() => { setShowCreate(false); setSection("catalog"); }}>完整历史与筛选请打开历史目录</button>
          </p>}
          {section!=="protocols"&&section!=="monthly"&&section!=="author-panels"&&section!=="author-studies"&&section!=="research-cases"&&section!=="research-jobs"&&section!=="semantic-materials"&&researches.length > 1 && (
            <label className="mobile-research-select">
              切换研究
              <select
                value={selectedResearch}
                onChange={(e) => setSelectedResearch(e.target.value)}
              >
                {researches.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.title}
                  </option>
                ))}
              </select>
            </label>
          )}
          {workspaceReady && research?.id === selectedResearch && <WorkflowObservations key={`${workspaceKey}:${research.id}`} workspaceId={workspaceId}
            researchId={research.id} revisionId={revision?.id} visible={section === "observations" && !showCreate}
            onOpen={() => navigateSection("observations")} onRecordingChange={onRecordingChange} />}
          {section==="semantic-materials"&&workspaceId ? <SemanticMaterials key={workspaceKey} workspaceScope={workspaceScope}/> : section==="research-jobs"&&workspaceId ? <>
            <IndustryMomExperiments key={`industry:${workspaceKey}:${sourceTarget.kind === "industry_mom_experiment" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "industry_mom_experiment" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenCase={openIndustryCase}/>
            <ResearchJobs key={workspaceKey} workspaceScope={workspaceScope} onOpenRun={id=>void openCaseSource("daily_run",id)} onOpenCases={()=>navigateSection("research-cases")}/><DomainResearchJobs key={`domain:${workspaceKey}`} workspaceScope={workspaceScope} onOpenSource={(kind,id)=>void openCaseSource(kind,id)}/></> : section==="research-cases"&&workspaceId ? <ResearchCases key={`${workspaceKey}:${sourceTarget.kind === "research_case" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "research_case" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenSource={openCaseSource}/> : section==="author-studies"&&workspaceId ? <AuthorStudies key={`${workspaceKey}:${sourceTarget.kind === "author_study" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "author_study" ? sourceTarget.id : ""} workspaceScope={workspaceScope}/> : section==="author-panels"&&workspaceId ? <AuthorPanels key={workspaceKey} workspaceScope={workspaceScope}/> : section==="monthly"&&workspaceId ? <MonthlyExperiments key={`${workspaceKey}:${sourceTarget.kind === "monthly_experiment" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "monthly_experiment" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenProtocols={()=>navigateSection("protocols")}/> : section==="protocols"&&workspaceId ? <ResearchProtocols key={workspaceKey} workspaceScope={workspaceScope}/> : selectionError ? (
            <section className="panel" role="alert"><h2>无法恢复此工作区的研究选择</h2><p>{selectionError}</p><p>当前选择尚未改变。请恢复连接后重试。</p><button className="button" onClick={() => setSelectionRetry((value) => value + 1)}>重试恢复研究选择</button></section>
          ) : !loaded || !workspaceId || selectionWorkspace !== workspaceId ? (
            <div className="loading-state" role="status">
              正在连接本地研究服务并恢复此工作区的研究选择…
            </div>
          ) : section === "catalog" ? (
            <CatalogPanel onOpen={openCatalog} />
          ) : showCreate ? (
            <CreateResearch
              key={`${workspaceId}:${createSeed?.nonce || "blank"}`}
              workspaceId={workspaceId}
              draftScope={createSeed?.scope}
              papers={papers}
              datasets={datasets}
              busy={busy}
              onCancel={() => setShowCreate(false)}
              onUpload={async (form) => {
                const p = await api<Paper>("/papers", {
                  method: "POST",
                  body: form,
                });
                await refresh();
                return p;
              }}
              onCreated={async (created) => {
                const scope = workspaceScope.capture();
                setNotice("研究已创建。证据匹配将在实验预检时核验。");
                setResearches((old) => [created, ...old.filter((item) => item.id !== created.id)]);
                setSelectedResearch(created.id); setShowCreate(false); setCreateSeed(null);
                try { await refresh(); }
                catch (e) { if (workspaceScope.isCurrent(scope)) setNotice(`研究已创建，列表刷新暂未成功；请刷新查看记录。${describeError(e)}`); }
              }}
              onError={setError}
              onRefreshDatasets={async () => { await refresh(); }}
              initialTask={createSeed?.task}
              initialPaperId={createSeed?.paperId}
            />
          ) : !selectedResearch ? (
            <div className="panel welcome">
              <Empty title="创建首个研究">
                <p>
                  导入内置 Alpha101 示例，体验完整的研究、实验、审核与回归链路。
                </p>
                <div className="button-row">
                  <button
                    className="button primary"
                    disabled={!!busy}
                    onClick={() => void importDemo()}
                  >
                    导入示例研究
                  </button>
                  <button
                    className="button"
                    onClick={() => { setCreateSeed(null); setShowCreate(true); }}
                  >
                    上传论文并手动定义
                  </button>
                </div>
              </Empty>
              <div className="welcome-steps">
                {[
                  "01  核对原文",
                  "02  定义候选",
                  "03  运行实验",
                  "04  审核与回流",
                ].map((x) => (
                  <span key={x}>{x}</span>
                ))}
              </div>
            </div>
          ) : !research ? (
            researchLoadError ? <section className="panel" role="alert"><h2>研究版本暂时无法读取</h2><p>{researchLoadError}</p><button className="button" onClick={() => setResearchRetry((value) => value + 1)}>重试读取所选研究</button></section> : <div className="loading-state">正在读取研究版本…</div>
          ) : (
            <>
              <div className="research-context">
                <div className="context-heading">
                  <span className="square-icon"></span>
                  <div>
                    <h2>{research.title}</h2>
                    <p>
                      {currentPaper?.title || "论文"} <span>·</span>{" "}
                      {currentDataset?.version || research.dataset_id}
                    </p>
                  </div>
                </div>
                <label className="version-select">
                  <span>研究版本</span>
                  <select
                    aria-label="研究版本"
                    value={revision?.id || ""}
                    onChange={(e) => {
                      setRevisionId(e.target.value);
                      setEditing(false);
                    }}
                  >
                    {research.revisions?.map((r) => (
                      <option key={r.id} value={r.id}>
                        v{r.number} · {formatDate(r.created_at)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {section !== "observations" && <DailyWorkflow research={research} revision={revision} run={currentRun} onStep={dailyStep} />}
              {(section==="quality"||section==="experiments")&&currentRun&&!activeStatuses.has(currentRun.status)&&<section className="info-note" aria-label="实验核验状态">
                <p>上次完整读取：{currentRun.client_loaded_at?formatDate(currentRun.client_loaded_at):"未记录"}。完整性状态仅代表该次加载时的核验；后台轻量状态检查不会重新核验文件。提交审核、回归和下载仍由后端校验。</p>
                <button type="button" className="button small" disabled={!!busy} onClick={()=>setRefreshNonce(value=>value+1)}>重新核验当前实验</button>
              </section>}
              {section === "insights" && <ResearchInsights key={`${workspaceKey}:${research.id}`} workspaceId={workspaceId}
                research={research} selectedRun={currentRun} busy={busy} onPerform={perform} />}
              {section === "research" && task && (
                <>
                  {research.preflight && revision?.id === research.latest_revision_id && <section className="panel">
                    <h3>当前修订的数据适配预检</h3>
                    <p className="fine-print">数据版本 {research.preflight.dataset_version}；原始表达式保留，建议不会自动应用。</p>
                    <div className="table-scroll"><table><thead><tr><th>候选</th><th>预检状态</th><th>规则码</th><th>预热 / 缺失字段</th></tr></thead><tbody>
                      {research.preflight.candidates.map((candidate) => <tr key={candidate.candidate_id}>
                        <td>{candidate.candidate_id}</td><td>{candidate.status === "ready" ? "可提交" : candidate.status === "blocked" ? "候选将被阻断" : "需检查"}</td>
                        <td>{candidate.code}</td><td>{candidate.warmup_rows ?? "—"} / {candidate.missing_fields?.join(", ") || "无"}</td>
                      </tr>)}
                    </tbody></table></div>
                    <JsonDetails value={research.preflight} label="查看预检范围与逐项依据" />
                  </section>}
                  <div className="research-grid">
                    <section id="daily-candidates" className="panel candidate-panel">
                      <div className="panel-heading">
                        <div>
                          <h2>
                            候选与论文依据{" "}
                            <span className="count-pill">
                              {task.candidates.length}
                            </span>
                          </h2>
                        </div>
                        <button
                          className="button small"
                          disabled={!!busy}
                          onClick={() => {
                            setEditing(!editing);
                          }}
                        >
                          {editing ? "收起编辑器" : "创建新版本"}
                        </button>
                      </div>
                      {revision && <RevisionSubmission
                        key={`${workspaceId}:${research.id}`}
                        workspaceId={workspaceId} research={research} revision={revision} editing={editing}
                        dataset={currentDataset} busy={busy} onSaved={revisionSaved} onCancel={() => setEditing(false)}
                      />}
                      <div className="candidate-list">
                        {task.candidates.map((c, index) => {
                          const h = task.hypotheses.find(
                            (h) => h.id === c.hypothesis_id,
                          );
                          return (
                            <article className="candidate-card" key={c.id}>
                              <div className="candidate-top">
                                <div>
                                  <span className="candidate-index">
                                    {String(index + 1).padStart(2, "0")}
                                  </span>
                                  <h3>{c.id}</h3>
                                </div>
                                <span
                                  className={`origin-tag ${c.origin === "paper_original" ? "original" : ""}`}
                                >
                                  {{
                                    paper_original: "论文原式",
                                    user_modification: "用户修改",
                                    model_conjecture: "模型推测",
                                  }[c.origin] || c.origin}
                                </span>
                              </div>
                              <code className="expression">{c.expression}</code>
                              {h && (
                                <>
                                  <p className="hypothesis-claim">{h.claim}</p>
                                  <div className="field-chips">
                                    <span>所需字段</span>
                                    {h.required_fields.map((f) => (
                                      <span
                                        className={`field-chip ${currentDataset && !currentDataset.fields.includes(f) ? "missing" : ""}`}
                                        key={f}
                                      >
                                        {f}
                                        {currentDataset &&
                                          !currentDataset.fields.includes(f) &&
                                          " · 缺失"}
                                      </span>
                                    ))}
                                  </div>
                                  <div className="evidence-list">
                                    {h.evidence_ids.map((id) => {
                                      const e = task.evidence.find(
                                        (e) => e.id === id,
                                      );
                                      return e ? (
                                        <div className="evidence" key={id}>
                                          <div>
                                            <span className="evidence-bar" />
                                            <span className="evidence-label">
                                              原文依据
                                            </span>
                                            <a
                                              href={`/api/papers/${research.paper_id}/pdf#page=${e.page}`}
                                              target="_blank"
                                              rel="noreferrer"
                                            >
                                              第 {e.page} 页 ↗
                                            </a>
                                          </div>
                                          <blockquote>{e.quote}</blockquote>
                                        </div>
                                      ) : (
                                        <div
                                          className="info-note warning"
                                          key={id}
                                        >
                                          缺少证据 {id}
                                        </div>
                                      );
                                    })}
                                  </div>
                                  <details className="hypothesis-details">
                                    <summary>研究解释与适用条件</summary>
                                    <p>
                                      <span className="origin-tag">
                                        {h.mechanism_attribution ===
                                        "model_conjecture"
                                          ? "解释为推测 · 待人工核验"
                                          : h.mechanism_attribution}
                                      </span>
                                    </p>
                                    <p>{h.economic_mechanism}</p>
                                    <ul>
                                      {h.assumptions.map((a, i) => (
                                        <li key={i}>{a}</li>
                                      ))}
                                    </ul>
                                  </details>
                                </>
                              )}
                              {c.changes.length > 0 && (
                                <p className="change-note">
                                  变更说明：{c.changes.join("；")}
                                </p>
                              )}
                            </article>
                          );
                        })}
                        {!task.candidates.length && (
                          <Empty title="还没有候选">
                            通过版本编辑器填写证据、假设和候选。
                          </Empty>
                        )}
                      </div>
                    </section>
                    <aside className="right-rail">
                      {revision && <div id="daily-run-submission"><RunSubmission key={`${workspaceId}:${research.id}`} workspaceId={workspaceId}
                        researchId={research.id} revision={revision} workerOnline={!!health?.worker.online} busy={busy}
                        onQueued={async (saved) => {
                          const scope = workspaceScope.capture();
                          if (researchSelection.current !== saved.research_id) return;
                          setRuns((old) => [saved, ...old.filter((item) => item.id !== saved.id)]);
                          setSelectedRun(saved.id); setSection("experiments");
                          setNotice("实验已保存到队列，可离开页面后返回查看。");
                          try { await refresh(); }
                          catch (e) { if (workspaceScope.isCurrent(scope)) setNotice(`实验已保存到队列，列表刷新暂未成功；请刷新查看记录。${describeError(e)}`); }
                        }} /></div>}
                      <section className="panel source-panel">
                        <div className="panel-heading">
                          <h2>研究材料</h2>
                        </div>
                        <a
                          className="source-link"
                          href={`/api/papers/${research.paper_id}/pdf`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <span>
                            {currentPaper?.title || "查看论文 PDF"}
                            <small>原始 PDF · 查看全文 ↗</small>
                          </span>
                        </a>
                        <div className="divider" />

                        <h3>{currentDataset?.version || "版本化数据集"}</h3>
                        <p className="muted">合成 OHLCV · 用于软件验证</p>
                        <div className="field-chips">
                          {currentDataset?.fields.map((f) => (
                            <span key={f} className="field-chip">
                              {f}
                            </span>
                          ))}
                        </div>
                        <p className="fine-print">
                          计算指标不代表真实市场收益或论文复现结果。
                        </p>
                        <JsonDetails
                          value={task.evaluation}
                          label="查看时间划分与评估配置"
                        />
                        {currentDataset && (
                          <JsonDetails
                            value={{
                              sha256: currentDataset.sha256,
                              metadata: currentDataset.metadata,
                            }}
                            label="查看数据版本与来源"
                          />
                        )}
                      </section>
                      <section className="panel latest-panel">
                        <div className="panel-heading">
                          <h2>最近实验</h2>
                          <span className="count-pill">
                            {researchRuns.length}
                          </span>
                        </div>
                        {researchRuns.slice(0, 3).map((r) => (
                          <button
                            className="recent-run"
                            key={r.id}
                            onClick={() => openRun(r.id)}
                          >
                            <span>
                              <code>{shortId(r.id)}</code>
                              <small>{modeName(r.mode)}</small>
                            </span>
                            <Badge status={r.status} />
                          </button>
                        ))}
                        {!researchRuns.length && (
                          <p className="muted">
                            首次提交后，结果会出现在这里。
                          </p>
                        )}
                      </section>
                    </aside>
                  </div>
                </>
              )}
              {section === "experiments" && (
                <>
                  <section className="panel run-browser">
                    <div className="panel-heading">
                      <div>
                        <h2>
                          运行记录{" "}
                          <span className="count-pill">
                            {researchRuns.length}
                          </span>
                        </h2>
                      </div>
                      <button
                        className="button small"
                        onClick={() => setSection("research")}
                      >
                        配置新实验
                      </button>
                    </div>
                    {researchRuns.length ? (
                      <div className="table-scroll">
                        <table>
                          <thead>
                            <tr>
                              <th>实验</th>
                              <th>版本 / 执行方式</th>
                              <th>状态</th>
                              <th>尝试次数</th>
                              <th>创建时间</th>
                            </tr>
                          </thead>
                          <tbody>
                            {researchRuns.map((r) => (
                              <tr
                                key={r.id}
                                className={
                                  r.id === selectedRun ? "selected-row" : ""
                                }
                              >
                                <td>
                                  <button
                                    className="run-link"
                                    aria-label={`查看实验 ${shortId(r.id)}`}
                                    onClick={() => setSelectedRun(r.id)}
                                  >
                                    {shortId(r.id)} <span>↗</span>
                                  </button>
                                </td>
                                <td>
                                  v
                                  {research.revisions?.find(
                                    (v) => v.id === r.revision_id,
                                  )?.number || "?"}{" "}
                                  <span className="muted">/</span>{" "}
                                  {modeName(r.mode)}
                                </td>
                                <td>
                                  <Badge status={r.status} />
                                </td>
                                <td>{r.attempt_count}</td>
                                <td>{formatDate(r.created_at)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <Empty title="还没有实验记录">
                        <p>核对研究定义后，从研究工作台提交一次实验。</p>
                      </Empty>
                    )}
                  </section>
                  {currentRun && <section className="panel" aria-label="实验后续操作">
                    <p>后续操作均使用当前实验 {shortId(currentRun.id)} 的冻结结果。</p>
                    <div className="button-row">
                      <button className="button" disabled={activeStatuses.has(currentRun.status) || !currentRun.verification?.verified} onClick={() => navigateSection("quality")}>继续审核当前实验</button>
                      <button className="button" disabled={activeStatuses.has(currentRun.status)} onClick={() => navigateSection("insights")}>为当前实验准备报告</button>
                    </div>
                  </section>}
                  {run && (
                    <RunDetail
                      run={run}
                      eventFeed={eventFeed}
                      dataset={currentDataset}
                      artifacts={artifacts}
                      busy={busy}
                      onAction={(action) =>
                        void perform(
                          action === "cancel" ? "取消实验" : "重试实验",
                          async () => {
                            await post(`/runs/${run.id}/${action}`, {});
                            await refresh();
                            setRefreshNonce((n) => n + 1);
                          },
                        )
                      }
                      onReview={() => setSection("quality")}
                    />
                  )}
                  {selectedRun && !run && (
                    <div className="loading-state">正在读取实验结果…</div>
                  )}
                </>
              )}
              {section === "quality" && (
                <>
                <QualityPanel
                  key={`${workspaceId}:${research.id}`}
                  workspaceId={workspaceId}
                  research={research}
                  runs={terminalRuns}
                  run={run}
                  selectedRun={selectedRun}
                  artifacts={artifacts}
                  cases={cases}
                  checks={checks}
                  busy={busy}
                  recording={recording}
                  onReviewTimerRunningChange={onReviewTimerRunningChange}
                  onReport={() => navigateSection("insights")}
                  onSelectRun={setSelectedRun}
                  onPerform={perform}
                  refresh={async () => {
                    await refresh();
                    setRefreshNonce((n) => n + 1);
                  }}
                  onCaseCreated={(created) => setCases((old) => [created, ...old.filter((item) => item.id !== created.id)].slice(0, 51))}
                />
                <FeedbackPanel key={`${workspaceId}:${research.id}`} workspaceId={workspaceId} research={research} reviews={run?.reviews || []} runs={runs} checks={checks} busy={busy} initialIssueId={selectedIssueId}
                  onPerform={perform} refresh={async () => { await refresh(); setRefreshNonce((n) => n + 1); }} />
                </>
              )}
            </>
          )}
          <footer className="page-footer">
            <span>Paper / Alpha</span>
            <span>本地研究工作台 · 所有指标由计算引擎产生</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
