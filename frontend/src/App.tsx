import CreationDraftInbox from "./components/CreationDraftInbox";
import { LanguageSwitch, useI18n } from "./i18n";
import ResearchArt, { ResearchMark } from "./components/ResearchArt";
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
import { Badge, Empty } from "./components/ui";
import RunDetail from "./components/RunDetail";
import QualityPanel from "./components/QualityPanel";
import CreateResearch from "./components/CreateResearch";
import { usePaper } from "./components/TaskEditor";
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
import ResearchStageContent from "./components/ResearchStageContent";
import type { DailyStep, ResearchStage } from "./components/DailyWorkflow";
import { retainSelected, selectedResearchRun } from "./dailyWorkflow";
import { useWorkPosition } from "./useWorkPosition";
import ReviewFollowup from "./components/ReviewFollowup";
import type { ReviewFollowupHandle } from "./components/ReviewFollowup";
import type { Issue } from "./feedback";
import type { Review } from "./domain";
import MainDailyWorkflow from "./components/MainDailyWorkflow";

type Section = "research" | "experiments" | "quality" | "catalog" | "insights" | "observations" | "protocols" | "monthly" | "author-panels" | "author-studies" | "research-cases" | "research-jobs" | "semantic-materials";

type RequestedResearchRevision = { researchId: string; revisionId?: string };

/** Explicit experiment navigation selects its frozen revision, never a fallback. */
export function resolveResearchRevision(research: Research, requested: RequestedResearchRevision | null): string | null {
  if (!requested || requested.revisionId === undefined || requested.researchId !== research.id) return research.latest_revision_id;
  const target = research.revisions?.find(item => item.id === requested.revisionId && item.research_id === research.id);
  return target?.id ?? null;
}

export default function App() {
  const { t } = useI18n();
  const [reviewDirty, setReviewDirty] = useState(false);
  const [createDirty, setCreateDirty] = useState(false);
  const [revisionDirty, setRevisionDirty] = useState(false);
  useEffect(() => {
    if (!reviewDirty && !createDirty && !revisionDirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [reviewDirty, createDirty, revisionDirty]);
  const [sourceTarget, setSourceTarget] = useState({kind: "", id: ""});
  const sourceNavigationRequest = useRef(0);
  const [section, setSection] = useState<Section>("research");
  const [researchStage, setResearchStage] = useState<ResearchStage>("materials");
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
  const [createSeed, setCreateSeed] = useState<{ task?: Task; paperId?: string; nonce: string; scope: string; resumeRequested?: boolean } | null>(null);
  const [editing, setEditing] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [selectedIssueId, setSelectedIssueId] = useState("");
  const [requestedReview, setRequestedReview] = useState<Review | null>(null);
  const followup = useRef<ReviewFollowupHandle>(null);
  const issueSelection = useRef(selectedIssueId); issueSelection.current=selectedIssueId;
  const positionIntent=useRef(0);
  const { restore: restoreWorkPosition, save: saveWorkPosition, cancel: cancelWorkPosition,
    ready: workPositionReady, status: workPositionStatus, notice: workPositionNotice } = useWorkPosition({workspaceScope,researchId:selectedResearch});
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
  const pendingOpen = useRef<{ researchId: string; revisionId?: string; runId?: string; issueId?: string; regressionCase?: RegressionCase } | null>(null);
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
  useEffect(() => { navigationEpoch.current += 1; }, [selectedResearch, section, researchStage, showCreate, revisionId, editing, workspaceKey, selectedIssueId, requestedReview?.id]);
  const currentRun = selectedResearchRun(selectedResearch, selectedRun, run);
  const researchRuns = retainSelected(runs.filter((r) => r.research_id === selectedResearch), currentRun);
  const { paper: paperDetail, error: paperDetailError } = usePaper(research?.paper_id || "", workspaceKey);
  const currentPaper = paperDetail || papers.find((p) => p.id === research?.paper_id);
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
        if (choice.fellBack) setNotice(t("原先选择的研究不在此工作区，已返回可用研究；其他工作区的记录保持独立。", "The previous study is not in this workspace. An available study is selected; other workspaces remain separate."));
      } catch (error) { if (current()) setSelectionError(describeError(error)); }
    }
    void restoreSelection();
    return () => abort.abort();
  }, [workspaceId, workspaceKey, selectionRetry, refresh, workspaceScope, resetExperiment]);
  useEffect(() => {
    const startedIntent=positionIntent.current;
    setResearch(null); setResearchLoadError("");
    setRevisionId(""); resetExperiment(); setSelectedRun("");
    setSelectedIssueId(""); setRequestedReview(null); setEditing(false); setResearchStage("materials");
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
    ]).then(async ([r, rs, cs, ck]) => {
        if (!current()) return;
        const pending = pendingOpen.current;
        const requestedRevision = resolveResearchRevision(r, pending);
        if (requestedRevision === null) throw new Error(t(
          `目标实验版本 ${pending?.revisionId} 不属于当前研究或尚未读取到；请重试读取，不会自动改选其他版本。`,
          `The experiment revision ${pending?.revisionId} is unavailable for this study. Retry loading; another revision will not be substituted.`));
        persistResearchSelection(browserSelectionStorage(), workspaceId, r.id);
        setResearch(r); setRevisionId(requestedRevision);
        setResearches((old) => old.some((item) => item.id === r.id) ? old : [...old.slice(0, 50), r]);
        setRuns(rs.items); setCases(cs.items); setChecks(ck.items);
        setWindowTotals((old) => ({ ...old, runs: rs.total_records, cases: cs.total_records, checks: ck.total_records }));
        if (pending?.researchId === r.id) {
          takeNavigation();
          if (pending.runId) setSelectedRun(pending.runId);
          if (pending.issueId) setSelectedIssueId(pending.issueId);
          if (pending.regressionCase) setCases((old) => [pending.regressionCase!, ...old.filter((item) => item.id !== pending.regressionCase!.id)].slice(0, 51));
          pendingOpen.current = null;
        } else if (positionIntent.current===startedIntent) {
          await restorePosition(r);
        } else {
          takeNavigation();
        }
      })
      .catch((error) => {
        if (!current()) return;
        if (error instanceof MissingResearchSelection) {
          persistResearchSelection(browserSelectionStorage(), workspaceId, "");
          setNotice(t("所选研究已不可用，正在重新读取此工作区的研究目录。", "The selected study is unavailable. Reloading this workspace catalog."));
          setSelectionRetry((value) => value + 1);
        } else setResearchLoadError(describeError(error));
      });
    return () => abort.abort();
  }, [selectedResearch, selectionWorkspace, workspaceId, workspaceKey, researchRetry, workspaceScope, resetExperiment, restoreWorkPosition, cancelWorkPosition]);

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
    if (!canLeaveReview()) return;
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
        setNotice(t("示例材料已导入；当前选择的研究保持不变。", "Example imported; your current study selection is unchanged."));
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
      setNotice(t("示例已就绪。请先核对论文证据与候选，再提交实验。", "Example ready. Check the paper evidence and candidates before submitting an experiment."));
    });
  }
  function takeNavigation() { positionIntent.current++;pendingOpen.current = null;cancelWorkPosition(); }
  async function restorePosition(value: Research) {
    const restored = await restoreWorkPosition(value);
    if (!restored.position || restored.status === "stale" || researchSelection.current !== value.id) return;
    const position=restored.position;
    setRevisionId(position.revisionId); setSelectedRun(position.runId); setSelectedIssueId(position.issueId || "");
    if (restored.run) setRuns(old=>retainSelected(old,restored.run));
    const stage=position.stage;
    if(stage==="materials"||stage==="evidence"||stage==="candidates")setResearchStage(stage);
    setSection(stage==="review"?"quality":stage==="report"?"insights":stage==="experiments"?"experiments":"research");
  }
  useEffect(()=>{
    if (!workPositionReady || !workspaceReady || research?.id!==selectedResearch || !revisionId || showCreate) return;
    if (!["research","experiments","quality","insights"].includes(section)) return;
    saveWorkPosition({stage:section==="quality"?"review":section==="experiments"?"experiments":section==="insights"?"report":researchStage,
      revisionId,runId:selectedRun,issueId:selectedIssueId});
  },[workPositionReady,workspaceReady,research?.id,selectedResearch,revisionId,showCreate,section,researchStage,selectedRun,selectedIssueId,saveWorkPosition]);
  function chooseRun(id:string, review=false) {
    if(researchSelection.current!==selectedResearch||workspaceScope.key!==workspaceKey||!canLeaveReview())return;
    takeNavigation();setSelectedRun(id);if(review)setSection("quality");
  }
  function editIssue(issue:Issue) {
    if(!canLeaveReview()||!research||issue.research_id!==research.id)return;
    takeNavigation();setSelectedIssueId(issue.id);setRequestedReview(null);
    setRevisionId(research.latest_revision_id);setEditing(true);setShowCreate(false);setSection("research");
    setResearchStage(issue.events.at(-1)?.disposition==="evidence_update"?"evidence":"candidates");
  }
  function followReview(review:Review) {
    if(!canLeaveReview())return;
    takeNavigation();setSelectedIssueId("");setRequestedReview(review);
    window.setTimeout(()=>document.getElementById("review-followup")?.scrollIntoView({block:"start",behavior:"smooth"}),0);
  }
  async function revisionSaved(newRevision:Revision,baseRevisionId:string) {
    if(!research)return;
    const startedNavigation=navigationEpoch.current,workspace=workspaceScope.capture();
      const updated = await api<Research>(`/researches/${research.id}`);
      if (workspaceScope.isCurrent(workspace) && researchSelection.current === research.id && navigationEpoch.current === startedNavigation) {
        setResearch(updated);
        let linkWarning="";
        try { await followup.current?.linkRevision(newRevision); }
        catch(e) { linkWarning=t(" 问题关联待处理："," Issue linking requires attention: ")+describeError(e); }
        if (!workspaceScope.isCurrent(workspace) || researchSelection.current !== research.id || navigationEpoch.current !== startedNavigation) return;
        if (revisionSelection.current === baseRevisionId) {
          setRevisionId(newRevision.id);
          setNotice(t(`版本 ${newRevision.number} 已保存，尚未提交实验。历史结果保持原版本。`, `Revision ${newRevision.number} saved; no experiment submitted. Historical results keep their original revision.`)+linkWarning);
          setEditing(false);
          setSection("experiments");
        }
      }
      if(workspaceScope.isCurrent(workspace))await refresh();
  }
  function canLeaveReview() {
    if (!reviewTimerRunning) return !(reviewDirty || createDirty || revisionDirty) || window.confirm(t("当前有尚未提交的编辑。确定离开当前页面吗？", "You have unsubmitted edits. Leave the current view?"));
    setError(t("请先暂停主动工作计时，再离开审核或开始流程观测；已结束区间会保留在审核草稿中。", "Pause the active-work timer before leaving review or starting an observation. Completed intervals remain in the review draft."));
    return false;
  }
  function navigateSection(next: Section) {
    if ((next !== section || showCreate) && !canLeaveReview()) return;
    takeNavigation();setSection(next); setShowCreate(false);
  }
  function dailyStep(step: DailyStep) {
    const next = step === "review" ? "quality" : step === "report" ? "insights" : step === "experiments" ? "experiments" : "research";
    if ((next !== section || showCreate || (next === "research" && step !== researchStage)) && !canLeaveReview()) return;
    takeNavigation();
    if (step === "materials" || step === "evidence" || step === "candidates") setResearchStage(step);
    setSection(next); setShowCreate(false);
  }
  async function openCaseSource(kind: ResearchCaseSource, id: string) {
    const captured = workspaceScope.capture();
    const navigation = navigationEpoch.current, request = ++sourceNavigationRequest.current;
    const current = () => workspaceScope.isCurrent(captured) && navigationEpoch.current === navigation && sourceNavigationRequest.current === request;
    if (reviewTimerRunning) {setError(t("请先暂停审核计时，再切换结果。", "Pause the review timer before switching results.")); return;}
    if (kind === "daily_run") {
      try {
        const value = await api<Run>(`/runs/${encodeURIComponent(id)}`);
        if (!current()) return;
        const updatedResearch = value.research_id === selectedResearch
          ? await api<Research>(`/researches/${encodeURIComponent(value.research_id)}`)
          : undefined;
        if (current()) openCatalog("runs", {id:value.id,research_id:value.research_id}, updatedResearch);
      } catch(e) {if(current())setError(describeError(e));}
    } else if (kind === "industry_mom_experiment") {
      setSourceTarget({kind,id}); navigateSection("research-jobs");
    } else if (kind === "author_study" || kind === "monthly_experiment") {
      setSourceTarget({kind,id}); navigateSection(kind === "author_study" ? "author-studies" : "monthly");
    } else {
      setError(t("未知研究来源，无法打开准确结果。", "Unknown research source. The corresponding result cannot be opened."));
    }
  }
  function openIndustryCase(id: string) {
    if (reviewTimerRunning) { setError(t("请先暂停审核计时，再切换研究任务。", "Pause the review timer before switching studies.")); return; }
    setSourceTarget({kind: "research_case", id}); navigateSection("research-cases");
  }
  function openCatalog(resource: CatalogResource, row: JsonObject, updatedResearch?: Research) {
    if (!canLeaveReview()) return;
    takeNavigation();setShowCreate(false);
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
      // A job may have committed a new revision while this study stayed selected.
      // Refresh the available versions without changing the editing selection.
      if (updatedResearch?.id === id) setResearch(updatedResearch);
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
        {t("跳转到工作区", "Skip to workspace")}
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#main"
          onClick={() => navigateSection("research")}
        >
          <ResearchMark /> <span>Paper-to-Alpha<small>{t("研究工作空间", "Research workspace")}</small></span>
        </a>
        <div className="workspace-tag">{t("本地研究空间", "Local research space")}</div>
        <nav aria-label={t("主导航", "Main navigation")}><button className={`nav-item ${section === "research" ? "active" : ""}`} onClick={() => navigateSection("research")}>{t("研究工作台", "Research workbench")}</button></nav>
        <details className="auxiliary-navigation"><summary>{t("辅助工具", "Additional tools")}</summary><nav aria-label={t("辅助导航", "Additional navigation")}>
          {(
            [
              { id: "research-jobs", icon: "experiment", name: t("研究执行", "Research execution") },
              { id: "research-cases", icon: "book", name: t("研究任务", "Research cases") },
              { id: "semantic-materials", icon: "book", name: t("语义评测", "Semantic evaluation") },
              { id: "protocols", icon: "book", name: t("研究规则", "Research rules") },
              { id: "monthly", icon: "experiment", name: t("月度实验", "Monthly experiments") },
              { id: "author-panels", icon: "book", name: t("作者数据", "Author data") },
              { id: "author-studies", icon: "book", name: t("研究准入", "Research eligibility") },
              { id: "experiments", icon: "experiment", name: t("实验记录", "Experiments") },
              { id: "quality", icon: "shield", name: t("审核与回归", "Results & review") },
              { id: "insights", icon: "experiment", name: t("研究对比与总结", "Research reports") },
              { id: "observations", icon: "experiment", name: t("流程观测", "Workflow observations") },
              { id: "catalog", icon: "book", name: t("历史目录", "History catalog") },
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
        </nav></details>
        <p className="fine-print">{t("流程观测为可选对照记录，日常研究可直接运行。", "Workflow observation is optional. Daily studies can run directly.")}</p>
        <div className="sidebar-heading">
          <span>{t("研究项目", "Studies")}</span>
          <button
            className="icon-button"
            aria-label={t("新建研究", "New study")}
            disabled={!workspaceReady}
            onClick={() => {
              if (!canLeaveReview()) return;
              takeNavigation();setShowCreate(true);
              setCreateSeed(null);
              setSection("research");
            }}
          >
            {t("新建", "New")}
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
                  if (!canLeaveReview()) return;
                  takeNavigation();setSelectedResearch(r.id);
                  setSection("research");
                  setShowCreate(false);
                }}
              >
                <span className="research-dot" />
                <span>{r.title}</span>
              </button>
            ))
          ) : (
            <p className="sidebar-hint">{t("导入一篇论文，开始你的第一个研究。", "Import a paper to start your first study.")}</p>
          )}
        </div>
        <p className="sidebar-bottom">{t("本地单用户 · Provider 需宿主显式启用", "Local workspace · Provider requires host authorization")}</p>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="muted">{t("工作空间", "Workspace")}</span>
            <span className="breadcrumb-sep">/</span>
            <span>
              {
                {
                  research: t("研究工作台", "Research workbench"),
                  protocols: t("研究规则", "Research rules"),
                  monthly: t("月度实验", "Monthly experiments"),
                  "author-panels": t("作者数据", "Author data"),
                  "author-studies": t("研究准入", "Research eligibility"),
                  "research-cases": t("研究任务", "Research cases"),
                  "research-jobs": t("研究执行", "Research execution"),
                  "semantic-materials": t("语义评测", "Semantic evaluation"),
                  experiments: t("实验记录", "Experiments"),
                  quality: t("审核与回归", "Results & review"),
                  catalog: t("历史目录", "History catalog"),
                  insights: t("研究对比与总结", "Research reports"),
                  observations: t("流程观测", "Workflow observations"),
                }[section]
              }
            </span>
          </div>
          <div className="topbar-status"><LanguageSwitch />
            <span className={`connection-dot ${health ? "online" : ""}`} />
            <span>{health ? t("服务已连接", "Service connected") : t("服务未连接", "Service disconnected")}</span>
            <span className="top-divider" />
            <span
              className={`connection-dot ${health?.worker.online ? "online" : ""}`}
            />
            <span>{health?.worker.online ? t("Worker 在线", "Worker online") : t("Worker 离线", "Worker offline")}</span>
            <button
              className="icon-button"
              aria-label={t("刷新工作台", "Refresh workbench")}
              disabled={!!busy}
              onClick={() =>
                void perform(t("刷新", "Refresh"), async () => {
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
              {t("刷新", "Refresh")}
            </button>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <h1>
                {
                  {
                    research: t("研究工作台", "Research workbench"),
                    protocols: t("研究规则", "Research rules"),
                    monthly: t("月度实验", "Monthly experiments"),
                    "author-panels": t("作者数据", "Author data"),
                    "author-studies": t("研究准入", "Research eligibility"),
                    "research-cases": t("研究任务", "Research cases"),
                  "research-jobs": t("研究执行", "Research execution"),
                  "semantic-materials": t("语义评测", "Semantic evaluation"),
                    experiments: t("实验记录", "Experiments"),
                    quality: t("审核与回归", "Results & review"),
                    catalog: t("历史目录", "History catalog"),
                  insights: t("研究对比与总结", "Research reports"),
                  observations: t("流程观测", "Workflow observations"),
                  }[section]
                }
              </h1>
              <p>
                {
                  {
                    research: t("整理论文依据，定义候选，保留每次研究判断。", "Connect paper evidence, candidate definitions and research decisions."),
                    protocols: "区分论文定义与项目约定，展开时间窗口并检查缺失观察。",
                    monthly: "执行冻结月度规则，检查权重、标签覆盖、代理指标与审核记录。",
                    "author-panels": "导入有出处的作者月度面板，核对原行、时间对齐及不可用原因。",
                    "author-studies": "冻结研究计划，检查连续区间的全原行资格，并保留审核与修订依据。",
                    "research-cases": "关联依据与实际结果，核对阻塞原因，记录有界的下一步建议。",
                    "research-jobs": "执行固定行业 MOM 真实来源案例或既有受控任务，核对每次实际输出与研究范围。",
                    "semantic-materials": "阅读固定论文材料，留下自己的判断和理由，保留全部标注历史。",
                    experiments:
                      t("追踪任务状态、计算结果与失败原因，保留完整执行过程。", "Track execution, computed results and failures with a complete history."),
                    quality:
                      t("冻结输出，记录人工判断，再用明确的预期检查下一版。", "Inspect frozen outputs, record your judgment and check subsequent revisions."),
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
                  if (!canLeaveReview()) return;
                  setShowCreate(true);
                  setCreateSeed(null);
                  setSection("research");
                }}
              >
                {t("新建研究", "New study")}
              </button>
              {task && research && <button className="button" onClick={() => {
                if (!canLeaveReview()) return;
                setCreateSeed({ task: editableTask(task), paperId: research.paper_id, nonce: crypto.randomUUID(), scope: `${research.id}:${revision?.id}` });
                setShowCreate(true);
                setSection("research");
              }}>{t("复制当前任务新建研究", "Copy into a new study")}</button>}
              <button
                className="button primary"
                disabled={!!busy || !workspaceReady}
                onClick={() => void importDemo()}
              >
                {busy === "导入示例" ? t("导入中…", "Importing…") : t("导入示例研究", "Import example study")}
              </button>
            </div>}
          </div>
          {error && (
            <div className="banner error" role="alert">
              <strong>{t("操作未完成", "Operation incomplete")}</strong>
              <span>{error}</span>
              <button
                className="text-button"
                onClick={() => setError("")}
                aria-label={t("关闭错误", "Dismiss error")}
              >
                {t("关闭", "Dismiss")}
              </button>
            </div>
          )}
          {notice && (
            <div className="banner success" role="status">
              <span>{notice}</span>
              <button
                className="text-button"
                onClick={() => setNotice("")}
                aria-label={t("关闭提示", "Dismiss notice")}
              >
                {t("关闭", "Dismiss")}
              </button>
            </div>
          )}
          {section!=="protocols"&&section!=="monthly"&&section!=="author-panels"&&section!=="author-studies"&&section!=="research-cases"&&section!=="research-jobs"&&section!=="semantic-materials"&&<div className="scope-strip">
            <span className="scope-label">{t("当前范围", "Current scope")}</span>
            <span>{t("候选保留人工或模型来源", "Candidates retain manual or model provenance")}</span>
            <i />
            <span>{currentDataset?.data_kind === "synthetic" ? t("合成数据验证", "Synthetic-data validation") : currentDataset?.data_kind || t("数据类型待确认", "Data kind not yet loaded")}</span>
            <i />
            <span>{t("按冻结配置评估", "Evaluate the frozen configuration")}</span>
            <span className="scope-end">{t("工程执行与人工质量审核分开", "Engineering execution and human quality review are separate")}</span>
          </div>}
          {loaded && section !== "catalog" && section!=="protocols" && section!=="monthly" && section!=="author-panels" && section!=="author-studies" && section!=="research-cases" && section!=="research-jobs"&&section!=="semantic-materials" && <details className="flow-record-window"><summary>{t("记录范围与历史目录", "Record scope and history")}</summary><p className="fine-print">
            {t(`工作区只展示有界记录窗口：研究 ${researches.length}/${windowTotals.researches}；当前研究实验 ${researchRuns.length}/${windowTotals.runs}、案例 ${cases.length}/${windowTotals.cases}、检查 ${checks.length}/${windowTotals.checks}。`, `Bounded record window: studies ${researches.length}/${windowTotals.researches}; selected study runs ${researchRuns.length}/${windowTotals.runs}, cases ${cases.length}/${windowTotals.cases}, checks ${checks.length}/${windowTotals.checks}.`)}
            <button className="text-button" onClick={() => { if (canLeaveReview()) { setShowCreate(false); setSection("catalog"); } }}>{t("完整历史与筛选请打开历史目录", "Open the catalog for complete history and filters")}</button>
          </p></details>}
          {section!=="protocols"&&section!=="monthly"&&section!=="author-panels"&&section!=="author-studies"&&section!=="research-cases"&&section!=="research-jobs"&&section!=="semantic-materials"&&researches.length > 1 && (
            <label className="mobile-research-select">
              {t("切换研究", "Switch study")}
              <select
                value={selectedResearch}
                onChange={(e) => { if (canLeaveReview()) setSelectedResearch(e.target.value); }}
              >
                {researches.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.title}
                  </option>
                ))}
              </select>
            </label>
          )}
          {workspaceReady && section === "research" && !showCreate && <details className="panel">
            <summary>{t("四字段日频研究 · 来源到实验", "Four-field daily research · source to experiment")}</summary>
            <MainDailyWorkflow key={`main-daily:${workspaceKey}`} workspaceScope={workspaceScope} researches={researches}
              onOpenResearch={target => {
                if (!canLeaveReview()) return;
                takeNavigation(); pendingOpen.current = target;
                if (researchSelection.current === target.researchId) setResearchRetry(value => value + 1);
                setSelectedResearch(target.researchId); setShowCreate(false); setSection("research"); setResearchStage("candidates");
                void refresh().catch(cause => setError(describeError(cause)));
              }}
              onOpenRun={id => void openCaseSource("daily_run", id)} />
          </details>}
          {workspaceReady && section === "research" && !showCreate && <CreationDraftInbox
            key={workspaceKey} workspaceId={workspaceId} disabled={!!busy}
            onResume={entry => {
              if (!canLeaveReview()) return;
              takeNavigation();
              setCreateSeed({ scope: entry.scope, nonce: crypto.randomUUID(), resumeRequested: true });
              setShowCreate(true); setSection("research");
            }}
            onRecoverRequest={scope => {
              if (!canLeaveReview()) return;
              takeNavigation();
              setCreateSeed({ scope, nonce: crypto.randomUUID() });
              setShowCreate(true); setSection("research");
            }}
          />}
          {workspaceReady && research?.id === selectedResearch && <WorkflowObservations key={`${workspaceKey}:${research.id}`} workspaceId={workspaceId}
            researchId={research.id} revisionId={revision?.id} visible={section === "observations" && !showCreate}
            onOpen={() => navigateSection("observations")} onRecordingChange={onRecordingChange} />}
          {section==="semantic-materials"&&workspaceId ? <SemanticMaterials key={workspaceKey} workspaceScope={workspaceScope}/> : section==="research-jobs"&&workspaceId ? <>
            <IndustryMomExperiments key={`industry:${workspaceKey}:${sourceTarget.kind === "industry_mom_experiment" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "industry_mom_experiment" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenCase={openIndustryCase}/>
            <ResearchJobs key={workspaceKey} workspaceScope={workspaceScope} onOpenRun={id=>void openCaseSource("daily_run",id)} onOpenCases={()=>navigateSection("research-cases")}/><DomainResearchJobs key={`domain:${workspaceKey}`} workspaceScope={workspaceScope} onOpenSource={(kind,id)=>void openCaseSource(kind,id)}/></> : section==="research-cases"&&workspaceId ? <ResearchCases key={`${workspaceKey}:${sourceTarget.kind === "research_case" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "research_case" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenSource={openCaseSource}/> : section==="author-studies"&&workspaceId ? <AuthorStudies key={`${workspaceKey}:${sourceTarget.kind === "author_study" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "author_study" ? sourceTarget.id : ""} workspaceScope={workspaceScope}/> : section==="author-panels"&&workspaceId ? <AuthorPanels key={workspaceKey} workspaceScope={workspaceScope}/> : section==="monthly"&&workspaceId ? <MonthlyExperiments key={`${workspaceKey}:${sourceTarget.kind === "monthly_experiment" ? sourceTarget.id : ""}`} initialId={sourceTarget.kind === "monthly_experiment" ? sourceTarget.id : ""} workspaceScope={workspaceScope} onOpenProtocols={()=>navigateSection("protocols")}/> : section==="protocols"&&workspaceId ? <ResearchProtocols key={workspaceKey} workspaceScope={workspaceScope}/> : selectionError ? (
            <section className="panel" role="alert"><h2>{t("无法恢复此工作区的研究选择", "Unable to restore the workspace selection")}</h2><p>{selectionError}</p><p>{t("当前选择尚未改变。请恢复连接后重试。", "The selection has not changed. Reconnect and retry.")}</p><button className="button" onClick={() => setSelectionRetry((value) => value + 1)}>{t("重试恢复研究选择", "Retry restoring selection")}</button></section>
          ) : !loaded || !workspaceId || selectionWorkspace !== workspaceId ? (
            <div className="loading-state" role="status">
              {t("正在连接本地研究服务并恢复此工作区的研究选择…", "Connecting to the local service and restoring the workspace selection…")}
            </div>
          ) : section === "catalog" ? (
            <CatalogPanel onOpen={openCatalog} />
          ) : showCreate ? (
            <CreateResearch
              key={`${workspaceId}:${createSeed?.nonce || "blank"}`}
              workspaceId={workspaceId}
              onDirtyChange={setCreateDirty}
              draftScope={createSeed?.scope}
              resumeRequested={createSeed?.resumeRequested}
              papers={papers}
              datasets={datasets}
              busy={busy}
              onCancel={() => { if (canLeaveReview()) setShowCreate(false); }}
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
                setNotice(t("研究已保存，尚未提交实验。请核对已保存版本，再明确提交。", "Study saved; no experiment submitted. Check the saved revision, then explicitly submit."));
                setResearches((old) => [created, ...old.filter((item) => item.id !== created.id)]);
                setSelectedResearch(created.id); setShowCreate(false); setCreateSeed(null); setSection("experiments");
                try { await refresh(); }
                catch (e) { if (workspaceScope.isCurrent(scope)) setNotice(t(`研究已创建，列表刷新暂未成功；请刷新查看记录。${describeError(e)}`, `Study created, but refreshing the list failed. Refresh to view it. ${describeError(e)}`)); }
              }}
              onOpenExperiment={target => {
                takeNavigation(); setCreateDirty(false);
                const openedWorkspace = workspaceScope.capture();
                pendingOpen.current = { researchId: target.researchId, revisionId: target.revisionId, runId: target.runId };
                // The study can stay selected while the job creates a different
                // revision; force fresh detail so the pending target is consumed.
                if (researchSelection.current === target.researchId) setResearchRetry(value => value + 1);
                setSelectedResearch(target.researchId); setShowCreate(false); setCreateSeed(null); setSection("experiments");
                setNotice(t("已打开 AI 研究对应的实验；材料、候选与运行来源保持关联。", "Opened the AI study's experiment with material, candidate and execution provenance retained."));
                void refresh().catch(cause => {
                  if (workspaceScope.isCurrent(openedWorkspace) && researchSelection.current === target.researchId) setError(describeError(cause));
                });
              }}
              onError={setError}
              onRefreshDatasets={async () => { await refresh(); }}
              initialTask={createSeed?.task}
              initialPaperId={createSeed?.paperId}
            />
          ) : !selectedResearch ? (
            <div className="panel welcome">
              <Empty title={t("创建首个研究", "Create your first study")}>
                <p>
                  {t("导入内置 Alpha101 示例，体验完整的研究、实验、审核与回归链路。", "Import the Alpha101 example to try the research, experiment and review workflow.")}
                </p>
                <div className="button-row">
                  <button
                    className="button primary"
                    disabled={!!busy}
                    onClick={() => void importDemo()}
                  >
                    {t("导入示例研究", "Import example study")}
                  </button>
                  <button
                    className="button"
                    onClick={() => { takeNavigation();setCreateSeed(null); setShowCreate(true); }}
                  >
                    {t("上传论文并手动定义", "Upload a paper and define a study")}
                  </button>
                </div>
              </Empty>
              <div className="welcome-steps">
                {[
                  t("01  核对原文", "01  Check the paper"),
                  t("02  定义候选", "02  Define candidates"),
                  t("03  运行实验", "03  Run experiments"),
                  t("04  审核与回流", "04  Review and revise"),
                ].map((x) => (
                  <span key={x}>{x}</span>
                ))}
              </div>
            </div>
          ) : !research ? (
            researchLoadError ? <section className="panel" role="alert"><h2>{t("研究版本暂时无法读取", "Unable to load research revisions")}</h2><p>{researchLoadError}</p><button className="button" onClick={() => setResearchRetry((value) => value + 1)}>{t("重试读取所选研究", "Retry loading this study")}</button></section> : <div className="loading-state">{t("正在读取研究版本…", "Loading research revision…")}</div>
          ) : (
            <>
              <div className="research-context">
                <div className="context-heading">
                  <span className="square-icon"></span>
                  <div>
                    <h2>{research.title}</h2>
                    <p>
                      {currentPaper?.title || t("论文", "Paper")} <span>·</span>{" "}
                      {currentDataset?.version || research.dataset_id}
                    </p>
                  </div>
                </div>
                <label className="version-select">
                  <span>{t("研究版本", "Research revision")}</span>
                  <select
                    aria-label={t("研究版本", "Research revision")}
                    value={revision?.id || ""}
                    onChange={(e) => {
                      if (!canLeaveReview()) return;
                      takeNavigation();setRevisionId(e.target.value);
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
              {section !== "observations" && <DailyWorkflow research={research} revision={revision} run={currentRun} editing={editing} stage={section === "quality" ? "review" : section === "experiments" ? "experiments" : section === "insights" ? "report" : researchStage} onStep={dailyStep} researchRuns={researchRuns} onOpenRun={(id)=>chooseRun(id,true)} />}
              {workPositionStatus==="restoring"&&<p role="status">{t("正在恢复上次工作位置…","Restoring your last work position…")}</p>}
              {workPositionNotice&&<section className="info-note" role="status"><p>{workPositionNotice}</p>{workPositionStatus==="unavailable"&&<button type="button" className="button" onClick={()=>void restorePosition(research)}>{t("重试恢复工作位置","Retry restoring work position")}</button>}</section>}
              <div id="review-followup"><ReviewFollowup ref={followup} key={`${workspaceId}:${research.id}`} workspaceId={workspaceId} navigationKey={`${section}:${researchStage}:${revisionId}:${selectedRun}:${editing}`}
                research={research} revision={revision} selectedIssueId={selectedIssueId} requestedReview={requestedReview} run={currentRun} checks={checks} busy={busy}
                onSelectIssue={issue=>{takeNavigation();setSelectedIssueId(issue.id);setRequestedReview(null);}}
                onEdit={editIssue} onOpenRun={id=>chooseRun(id,true)} onClear={()=>{takeNavigation();setSelectedIssueId("");setRequestedReview(null);}} /></div>
              {(section==="quality"||section==="experiments")&&currentRun&&!activeStatuses.has(currentRun.status)&&<section className="info-note" aria-label={t("实验核验状态", "Experiment verification")}>
                <p>{t(`上次完整读取：${currentRun.client_loaded_at?formatDate(currentRun.client_loaded_at):"未记录"}。完整性状态仅代表该次加载时的核验；后台轻量状态检查不会重新核验文件。提交审核、回归和下载仍由后端校验。`, `Last full load: ${currentRun.client_loaded_at?formatDate(currentRun.client_loaded_at):"not recorded"}. Integrity reflects that load. Lightweight polling does not reverify files; review, regression and downloads are checked by the server.`)}</p>
                <button type="button" className="button small" disabled={!!busy} onClick={()=>setRefreshNonce(value=>value+1)}>{t("重新核验当前实验", "Verify this experiment again")}</button>
              </section>}
              {section === "insights" && <ResearchInsights key={`${workspaceKey}:${research.id}`} workspaceId={workspaceId}
                research={research} selectedRun={currentRun} busy={busy} onPerform={perform} />}
              {revision && <div hidden={section !== "research"}>
                <RevisionSubmission key={`${workspaceId}:${research.id}`} workspaceId={workspaceId}
                  research={research} revision={revision} editing={editing} navigationKey={`${section}:${researchStage}`} dataset={currentDataset}
                  onDirtyChange={setRevisionDirty}
                  busy={busy} onSaved={revisionSaved} onCancel={() => { if (canLeaveReview()) setEditing(false); }} />
              </div>}
              {section === "research" && researchStage === "materials" && !editing && <ResearchArt />}
              {section === "research" && paperDetailError && <p className="info-note warning" role="alert">{t("论文页级文本读取失败；可打开 PDF 核对，或刷新页面重试。", "Paper page text could not be loaded. Inspect the PDF or reload to retry.")} {paperDetailError}</p>}
              {section === "research" && revision && !editing && <ResearchStageContent stage={researchStage}
                research={research} revision={revision} paper={currentPaper} dataset={currentDataset}
                onStep={dailyStep} onEdit={() => { takeNavigation();setEditing(true); }} />}
              {section === "experiments" && (
                <>
                  <section className="panel experiment-heading" aria-label={t("实验配置摘要", "Experiment configuration")}><h2>{t("实验", "Experiment")}</h2>
                    <p>{t(`提交 v${revision?.number} 的全部 ${task?.candidates.length ?? 0} 个候选，使用数据版本 ${currentDataset?.version}。指标只来自计算工具。`, `Run all ${task?.candidates.length ?? 0} candidates in v${revision?.number} against data ${currentDataset?.version}. Metrics come only from computation tools.`)}</p>

                    {editing && <button className="button" onClick={() => dailyStep("candidates")}>{t("返回未保存的编辑", "Return to unsaved edits")}</button>}
                  </section>

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
                        onClick={() => dailyStep("candidates")}
                      >
                        {t("修改候选版本", "Revise candidates")}
                      </button>
                    </div>
                    {researchRuns.length ? (
                      <div className="table-scroll">
                        <table>
                          <thead>
                            <tr>
                              <th>{t("实验", "Experiment")}</th>
                              <th>{t("版本 / 执行方式", "Revision / Mode")}</th>
                              <th>{t("状态", "Status")}</th>
                              <th>{t("尝试次数", "Attempts")}</th>
                              <th>{t("创建时间", "Created")}</th>
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
                                    aria-label={t(`查看实验 ${shortId(r.id)}`, `View experiment ${shortId(r.id)}`)}
                                    onClick={() => chooseRun(r.id)}
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
                      <Empty title={t("还没有实验记录", "No experiments yet")}>
                        <p>{t("核对研究定义后，从研究工作台提交一次实验。", "Check the saved research definition, then submit an experiment.")}</p>
                      </Empty>
                    )}
                  </section>
                  {currentRun && <section className="panel" aria-label={t("实验后续操作", "Next actions")}>
                    <p>{t(`后续操作均使用当前实验 ${shortId(currentRun.id)} 的冻结结果。`, `Next actions use the frozen results of experiment ${shortId(currentRun.id)}.`)}</p>
                    <div className="button-row">
                      <button className="button" disabled={activeStatuses.has(currentRun.status) || !currentRun.verification?.verified} onClick={() => navigateSection("quality")}>{t("继续审核当前实验", "Review this experiment")}</button>
                      <button className="button" disabled={activeStatuses.has(currentRun.status)} onClick={() => navigateSection("insights")}>{t("为当前实验准备报告", "Prepare a report")}</button>
                    </div>
                  </section>}
                  {currentRun && (
                    <RunDetail
                      run={currentRun}
                      eventFeed={eventFeed}
                      dataset={currentDataset}
                      artifacts={artifacts}
                      busy={busy}
                      onAction={(action) =>
                        void perform(
                          action === "cancel" ? t("取消实验", "Cancel experiment") : t("重试实验", "Retry experiment"),
                          async () => {
                            await post(`/runs/${currentRun.id}/${action}`, {});
                            await refresh();
                            setRefreshNonce((n) => n + 1);
                          },
                        )
                      }
                      onReview={() => setSection("quality")}
                    />
                  )}
                      {revision && <RunSubmission collapsed={researchRuns.some(item=>item.revision_id===revision.id)} key={`${workspaceId}:${research.id}`} workspaceId={workspaceId}
                        researchId={research.id} revision={revision} dataset={currentDataset} workerOnline={!!health?.worker.online} busy={editing ? t("请先保存或取消版本编辑", "Save or cancel revision edits first") : busy}
                        onSubmitting={() => { setSelectedRun(""); resetExperiment(); }}
                        onQueued={async (saved) => {
                          const scope = workspaceScope.capture();
                          if (researchSelection.current !== saved.research_id) return;
                          setRuns((old) => [saved, ...old.filter((item) => item.id !== saved.id)]);
                          if (!selectedRunRef.current) setSelectedRun(saved.id);
                          setNotice(t("实验已保存到队列，可离开页面后返回查看。", "Experiment queued. You can leave and return to view it."));
                          try { if (issueSelection.current===selectedIssueId) await followup.current?.linkRun(saved); await refresh(); }
                          catch (e) { if (workspaceScope.isCurrent(scope)) setNotice(t(`实验已保存到队列，列表刷新暂未成功；请刷新查看记录。${describeError(e)}`, `Experiment queued, but refreshing the list failed. Refresh to view it. ${describeError(e)}`)); }
                        }} />}
                  {selectedRun && !run && (
                    <div className="loading-state">{t("正在读取实验结果…", "Loading experiment results…")}</div>
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
                  run={currentRun}
                  selectedRun={selectedRun}
                  artifacts={artifacts}
                  cases={cases}
                  checks={checks}
                  busy={busy}
                  recording={recording}
                  onReviewDirtyChange={setReviewDirty}
                  onReviewTimerRunningChange={onReviewTimerRunningChange}
                  onReport={() => navigateSection("insights")}
                  onSelectRun={(id) => chooseRun(id)}
                  onFollowReview={followReview}
                  onPerform={perform}
                  refresh={async () => {
                    await refresh();
                    setRefreshNonce((n) => n + 1);
                  }}
                  onCaseCreated={(created) => setCases((old) => [created, ...old.filter((item) => item.id !== created.id)].slice(0, 51))}
                />
                <details className="panel issue-feedback-tools"><summary>{t("可选：问题反馈与追踪", "Optional: issue feedback and tracking")}</summary><FeedbackPanel key={`${workspaceId}:${research.id}`} workspaceId={workspaceId} research={research} reviews={currentRun?.reviews || []} runs={runs} checks={checks} busy={busy} initialIssueId={selectedIssueId}
                  onContinueRevision={editIssue} onIssueSelected={issue=>{takeNavigation();setSelectedIssueId(issue.id);}}
                  onPerform={perform} refresh={async () => { await refresh(); setRefreshNonce((n) => n + 1); }} /></details>
                </>
              )}
            </>
          )}
          <footer className="page-footer">
            <span>Paper / Alpha</span>
            <span>{t("本地研究工作台 · 所有指标由计算引擎产生", "Local research workbench · Metrics produced by the computation engine")}</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
