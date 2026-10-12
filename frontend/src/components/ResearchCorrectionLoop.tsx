import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { describeError, formatDate } from "../domain";
import type { LoopSourceSupport, ProvidersResponse, PublicResearchLoop, PublicResearchLoopPage,
  PublicResearchLoopV2, PublicResearchSession, ResearchV2ProposalDetail } from "../generated/api-contract";
import { useI18n } from "../i18n";
import { useRecoverableMutation, isExplicitMutationRejection } from "../useRecoverableMutation";
import { acknowledgeMutation, readMutation, rejectMutation } from "../reviewRecovery";
import type { PendingMutation } from "../reviewRecovery";
import type { WorkspaceRequestScope } from "../workspaceScope";
import {
  CORRECTION_BASE, CORRECTION_SESSION_BASE, clearCorrectionPosition, correctionCanAdvance, correctionPendingPlan,
  correctionLoopRequest, correctionMutationKey, correctionNextLabel, correctionPositionFromLoop,
  correctionReviewTarget, correctionSessionRequest, correctionStartPlan, loopPolls,
  prepareCorrectionAdvance, readCorrectionPosition, requireCorrectionLoop, requireCorrectionSession,
  saveCorrectionPosition,
} from "../researchCorrectionLoop";
import type { CorrectionPosition, CorrectionScope, CorrectionStartPlan } from "../researchCorrectionLoop";
import { NATIVE_CORRECTION_BASE, acknowledgeNativeCancel, acknowledgeNativeMutation, nativeCanCancel, nativeCancelJournalKey,
  nativeIdentity, nativeJournalKey, nativeLockName, nativeReceiptLinks, pinNativePlan, prepareNativeCancel, prepareNativeMutation,
  readNativeCancelJournal, readNativeJournal, rejectNativeCancel, rejectNativeMutation,
  requireNativeLoop, requireNativeStart, requireNativeSupport, sameNativePlan } from "../nativeCorrectionStart";
import type { NativeCancelJournal, NativeCorrectionStartResponse, NativeCorrectionSupport, NativeJournal, NativePending } from "../nativeCorrectionStart";
import MutationRecovery from "./MutationRecovery";
import { JsonDetails } from "./ui";

type Props = {workspaceScope: WorkspaceRequestScope; researchId: string; baseRevisionId: string;
  providers: ProvidersResponse | null; providerConfigId: string; onOpenJob: (id: string) => void;
  onOpenRun?: (id: string) => void};
export default function ResearchCorrectionLoop(props: Props) {
  const { t } = useI18n();
  return <details className="panel" aria-label={t("有界等价修正流程", "Bounded equivalent correction workflow")}>
    <summary>{t("研究修订 → 候选 → 等价修正 → 验证期实验", "Research revision → Candidate → Equivalent correction → Validation experiment")}</summary>
    <p>{t("此入口使用已维护的研究修订与合成数据。仅做一次等价表示修正，论文含义和收益表现仍需评审；材料抽取及真实数据来源不在此版本内。", "This entry uses a maintained research revision and synthetic data. It allows one equivalent representation correction; paper meaning and predictive quality still require review. Material extraction and real-data sources are outside this version.")}</p>
    {!props.researchId ? <p role="status">{t("先在上方选择已有研究。", "Select an existing research above.")}</p> :
      <CorrectionModeEntry key={`${props.workspaceScope.key}:${props.researchId}:${props.providerConfigId}`} {...props} />}
  </details>;
}
function CorrectionModeEntry(props: Props) {
  const { t } = useI18n();
  const config = props.providers?.providers.find(p => p.config_id === props.providerConfigId);
  const [mode, setMode] = useState(() => {
    if (config?.mode === "codex_cli") return "native";
    try {if (typeof window !== "undefined" && localStorage.getItem(nativeJournalKey({workspaceId: props.workspaceScope.workspaceId,
      researchId: props.researchId, providerConfigId: props.providerConfigId})) !== null) return "native";} catch { /* Native entry reports its own storage failure. */ }
    return "deterministic";
  });
  return <>
    <label>{t("修正入口", "Correction entry")}<select aria-label={t("修正入口", "Correction entry")} value={mode} onChange={e => setMode(e.target.value)}>
      <option value="deterministic">{t("确定性合成示例", "Deterministic synthetic fixture")}</option>
      <option value="native">{t("宿主批准 Native 任务", "Host-approved Native task")}</option>
    </select></label>
    {mode === "native" ? <NativeCorrectionEntry {...props} /> : <CorrectionEntry {...props} />}
  </>;
}

function NativeCorrectionEntry({workspaceScope, researchId, baseRevisionId, providerConfigId, onOpenJob, onOpenRun}: Props) {
  const { t } = useI18n();
  const scope = useMemo<CorrectionScope>(() => ({workspaceId: workspaceScope.workspaceId, researchId, providerConfigId}),
    [workspaceScope.workspaceId, researchId, providerConfigId]);
  const [journal, setJournal] = useState<NativeJournal | null>(null), [storageBlocked, setStorageBlocked] = useState(false);
  const [cancelJournal, setCancelJournal] = useState<NativeCancelJournal | null>(null), [cancelBlocked, setCancelBlocked] = useState(false);
  const [support, setSupport] = useState<NativeCorrectionSupport | null>(null), [loop, setLoop] = useState<PublicResearchLoopV2 | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const mounted = useRef(false), sending = useRef(false), readEpoch = useRef(0);
  const journalRef = useRef<NativeJournal | null>(null);
  const cancelRef = useRef<NativeCancelJournal | null>(null);
  function applyJournal(next: NativeJournal) {journalRef.current = next; setJournal(next);}
  function applyCancelJournal(next: NativeCancelJournal) {cancelRef.current = next; setCancelJournal(next);}
  const position = journal?.position || null, pending = journal?.pending || null;
  const frozenRevision = position?.plan.base_revision_id || baseRevisionId;
  const key = nativeJournalKey(scope);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {
    function readStorage() {
      try {
        const next = readNativeJournal(localStorage, scope), saved = journalRef.current?.position;
        if (saved && (!next.position || !sameNativePlan(saved.plan, next.position.plan) || saved.attached && !next.position.attached))
          throw new Error(t("另一标签页的记录与已冻结Native计划不符；保留原计划，停止操作。", "Another tab's record does not match the frozen Native plan. The original plan is retained and actions are blocked."));
        applyJournal(next); setStorageBlocked(false);
      }
      catch (e) {setStorageBlocked(true); setError(describeError(e));}
    }
    const changed = (event: StorageEvent) => {if (event.key === key || event.key === null) {readStorage(); setRefresh(v => v + 1);}};
    readStorage(); window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [scope, key, t]);
  useEffect(() => {
    if (!position?.attached) {setCancelJournal(null); cancelRef.current = null; setCancelBlocked(false); return;}
    const cancelKey = nativeCancelJournalKey(scope, position);
    function readStop() {
      try {
        const next = readNativeCancelJournal(localStorage, scope, position!), old = cancelRef.current;
        if (old?.pending && (!next.pending || JSON.stringify(old.pending.body) !== JSON.stringify(next.pending.body) || old.acknowledged && !next.acknowledged))
          throw new Error(t("原停止记录已改变或丢失；保留原pending并停止新操作。", "The original stop record changed or was lost. The original pending remains and new actions are blocked."));
        applyCancelJournal(next); setCancelBlocked(false);
      } catch (e) {setCancelBlocked(true); setError(describeError(e));}
    }
    const changed = (event: StorageEvent) => {if (event.key === cancelKey || event.key === null) {readStop(); setRefresh(v => v + 1);}};
    readStop(); window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [scope, position, t]);
  useEffect(() => {
    if (!journal || !frozenRevision || !providerConfigId || storageBlocked) return;
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSupport(null);
    const identity = nativeIdentity(scope, frozenRevision);
    void api<NativeCorrectionSupport>(`${NATIVE_CORRECTION_BASE}/support`, {method: "POST", signal: abort.signal, body: JSON.stringify(identity)})
      .then(response => {
        if (!workspaceScope.isCurrent(captured, abort.signal)) return;
        const value = requireNativeSupport(response, identity);
        const saved = journal.position, current = saved && value.plans.find(p => p.start_id === saved.plan.start_id);
        if (current && !sameNativePlan(current, saved!.plan)) throw new Error(t("宿主计划的源码、配置或profile已漂移；原计划仍保留。", "The host plan source, configuration or profile drifted. The original plan is retained."));
        setSupport(value);
      }).catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [scope, frozenRevision, position?.plan.start_id, refresh, workspaceScope, providerConfigId, !!journal, storageBlocked, t]);
  useEffect(() => {
    if (!position) {setLoop(null); return;}
    const captured = workspaceScope.capture(), abort = new AbortController(), epoch = ++readEpoch.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const response = await api<PublicResearchLoop | PublicResearchLoopV2>(`${CORRECTION_BASE}/${encodeURIComponent(position!.plan.loop_id)}`, {signal: abort.signal});
        if (!workspaceScope.isCurrent(captured, abort.signal) || readEpoch.current !== epoch) return;
        const value = requireNativeLoop(response, position!); setLoop(value);
        if (loopPolls(value)) timer = setTimeout(() => void read(), 2000);
      } catch (e) {
        if (workspaceScope.isCurrent(captured, abort.signal) && readEpoch.current === epoch) {setLoop(null); setError(describeError(e));}
      }
    }
    void read();
    return () => {abort.abort(); if (timer) clearTimeout(timer);};
  }, [position, refresh, workspaceScope]);
  async function locked<T>(work: () => Promise<T>): Promise<T> {
    return navigator.locks ? navigator.locks.request(nativeLockName(scope), work) : work();
  }
  async function choosePlan(startId: string) {
    if (sending.current || !journal || position || storageBlocked || !startId) return;
    const plan = support?.plans.find(p => p.start_id === startId);
    if (!plan) return;
    try {await locked(async () => {const next = pinNativePlan(localStorage, scope, journal, plan); if (mounted.current) applyJournal(next);});}
    catch (e) {setError(describeError(e));}
  }
  async function mutate(operation: NativePending["operation"]) {
    if (operation === "cancel") {await stopNative(); return;}
    if (sending.current || !journal || !position || storageBlocked || cancelBlocked ||
        position.attached && (!cancelJournal || !!cancelJournal.pending)) return;
    sending.current = true; setBusy(true); setError(""); ++readEpoch.current;
    const captured = workspaceScope.capture();
    let staged: NativeJournal | null = null;
    try {
      await locked(async () => {
        staged = prepareNativeMutation(localStorage, scope, journal, operation, loop);
        if (mounted.current && workspaceScope.isCurrent(captured)) applyJournal(staged);
        const item = staged.pending!;
        const response = await api<NativeCorrectionStartResponse | PublicResearchLoopV2>(item.path, {method: "POST", body: JSON.stringify(item.body)});
        if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
        const value = item.operation === "start" ? requireNativeStart(response as NativeCorrectionStartResponse, staged) :
          requireNativeLoop(response as PublicResearchLoopV2, staged.position!);
        const next = acknowledgeNativeMutation(localStorage, scope, staged);
        applyJournal(next); setLoop(value); setRefresh(v => v + 1);
      });
    } catch (e) {
      if (staged && isExplicitMutationRejection(e)) {
        try {rejectNativeMutation(localStorage, scope, staged);} catch { /* Retain the durable original when another tab changed it. */ }
      }
      if (mounted.current && workspaceScope.isCurrent(captured)) {
        try {
          const next = readNativeJournal(localStorage, scope);
          if (!next.position || !sameNativePlan(position.plan, next.position.plan)) throw new Error("Native recovery identity changed");
          applyJournal(next);
        } catch {setStorageBlocked(true);}
        setError(describeError(e)); setRefresh(v => v + 1);
      }
    } finally {sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false);}
  }
  async function stopNative() {
    if (sending.current || !journal || !position?.attached || !loop || !cancelJournal || storageBlocked || cancelBlocked) return;
    sending.current = true; setBusy(true); setError(""); ++readEpoch.current;
    const captured = workspaceScope.capture();
    let staged: NativeCancelJournal | null = null;
    try {
      await locked(async () => {
        staged = prepareNativeCancel(localStorage, scope, journal, cancelJournal, loop);
        if (mounted.current && workspaceScope.isCurrent(captured)) applyCancelJournal(staged);
        const item = staged.pending!;
        const response = await api<PublicResearchLoopV2>(item.path, {method: "POST", body: JSON.stringify(item.body)});
        if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
        const value = requireNativeLoop(response, position);
        const next = acknowledgeNativeCancel(localStorage, scope, position, staged, value);
        applyCancelJournal(next); setLoop(value); setRefresh(v => v + 1);
      });
    } catch (e) {
      if (staged && isExplicitMutationRejection(e)) {
        try {rejectNativeCancel(localStorage, scope, position, staged);} catch { /* Keep both durable original requests. */ }
      }
      if (mounted.current && workspaceScope.isCurrent(captured)) {
        try {applyCancelJournal(readNativeCancelJournal(localStorage, scope, position));} catch {setCancelBlocked(true);}
        setError(describeError(e)); setRefresh(v => v + 1);
      }
    } finally {sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false);}
  }
  const currentPlan = position && support?.plans.find(p => p.start_id === position.plan.start_id);
  const lockedOut = busy || storageBlocked || !journal;
  const progressLocked = lockedOut || cancelBlocked || !!position?.attached && (!cancelJournal || !!cancelJournal.pending);
  const allowed = !!currentPlan?.available && currentPlan.state !== "unknown";
  const target = loop && correctionReviewTarget(loop), receipts = loop ? nativeReceiptLinks(loop) : [];
  const cancellable = !!position?.attached && !!loop && nativeCanCancel(loop, position);
  return <section aria-label={t("宿主批准 Native 修正任务", "Host-approved Native correction task")}
    data-native-start-id={position?.plan.start_id} data-native-loop-id={position?.plan.loop_id} data-native-session-id={position?.plan.session_id}>
    <p>{t("本入口只连接宿主预先批准的 ordinary synthetic Task1 任务：候选1、生成1、符合条件时修正1；共两次 Provider 调用。不开放 E01、C03 或市场数据。", "This entry attaches only a host-approved ordinary synthetic Task1 task: one candidate, one generation and one eligible repair; two provider calls in total. E01, C03 and market data remain outside this scope.")}</p>
    <p>{t("受控工程验收：静态profile、逐次读取边界、推理观测和论文语义分别核验。Native实际读取边界尚未验收，论文语义与预测表现仍未审核。", "Controlled engineering qualification: static profile, per-attempt read boundary, inference observations and paper semantics are assessed separately. The actual Native read boundary is not yet qualified; paper semantics and predictive quality remain unreviewed.")}</p>
    {error && <p role="alert">{error}</p>}
    {storageBlocked && <p role="alert">{t("本机恢复存储无法核实，所有新操作已停止；原计划与待确认请求保留。", "Local recovery storage could not be verified. New actions are blocked; the original plan and unconfirmed request are retained.")}</p>}
    {!providerConfigId && <p role="status">{t("先选择宿主 Provider 配置。", "Select a host provider configuration first.")}</p>}
    {support ? <p role="status" data-native-support-code={support.reason_code}>{support.reason_code} · {support.message}</p> :
      providerConfigId && <p role="status">{t("正在核实原宿主计划；读取失败不会领取替代任务。", "Checking the original host plan; a failed read never attaches a substitute task.")}</p>}
    {support?.reason_code === "READ_BOUNDARY_UNVERIFIED" && <p role="alert">{t("READ_BOUNDARY_UNVERIFIED：真实读取边界检查未通过，不能启动真实 Native 推理。", "READ_BOUNDARY_UNVERIFIED: the actual read boundary check failed. Real Native inference cannot start.")}</p>}
    {!position && support && <label>{t("宿主批准计划", "Host-approved plan")}<select aria-label={t("宿主批准计划", "Host-approved plan")} value="" disabled={lockedOut} onChange={e => void choosePlan(e.target.value)}>
      <option value="">{t("选择并冻结原计划", "Select and pin the original plan")}</option>
      {support.plans.map(p => <option key={p.start_id} value={p.start_id} disabled={!p.available || p.state === "unknown" || p.state === "terminal"}>{p.authorization.kind === "fixture" ? t("离线合成示例", "Offline synthetic fixture") : t("有界真实验收", "Bounded real qualification")} · {p.start_id.slice(-12)} · {p.state} · {p.reason_code}</option>)}
    </select></label>}
    {position && <>
      <p>{t("已冻结原计划：", "Pinned original plan: ")}{position.plan.start_id} · {t("原修订：", "Original revision: ")}{position.plan.base_revision_id}</p>
      <p>{t("原会话 / 循环：", "Original session / loop: ")}{position.plan.session_id} / {position.plan.loop_id}</p>
      <p>{position.plan.authorization.kind === "fixture" ? t("离线synthetic工程示例，无真实模型推理。", "Offline synthetic engineering fixture; no real model inference.") : t("有界真实验收授权；不代表真实模型已验收。", "Bounded real qualification authorization; it does not certify real-model inference.")}</p>
      <p>{t("宿主预算：最多Provider调用 / 修正 / 候选：", "Host budget: maximum provider calls / repairs / candidates: ")}{position.plan.authorization.max_provider_dispatches} / {position.plan.authorization.max_repairs} / {position.plan.authorization.candidate_limit} · {t("截止：", "Deadline: ")}{formatDate(position.plan.authorization.deadline_at)}</p>
      <p>{t("静态profile / 读取边界 / 推理观测 / 语义：", "Static profile / read boundary / inference observation / semantics: ")}{position.plan.evidence.profile_status} / {position.plan.evidence.boundary_status} / {(currentPlan || position.plan).evidence.inference_status} / {position.plan.evidence.semantic_fidelity}</p>
      {currentPlan && <p role="status">{currentPlan.state} · {currentPlan.reason_code} · {currentPlan.message}</p>}
      {pending && <><p role="status">{pending.state === "rejected" ? t("原请求已明确拒绝。请保留记录并由宿主检查，不能改用新计划。", "The original request was explicitly rejected. Preserve its record for host inspection; a new plan cannot replace it.") : t("原请求结果待确认（未知）；先读取原任务，再用同一请求核对。", "The original request outcome is unconfirmed (unknown). Read the original task, then reconcile with the same request.")} · {pending.operation}</p>
        {pending.state === "unconfirmed" && <button type="button" className="button primary" disabled={pending.operation === "cancel" ? lockedOut || cancelBlocked || !cancelJournal || !loop || !!cancelJournal.acknowledged : progressLocked} onClick={() => void mutate(pending.operation)}>{t("核对原Native请求", "Reconcile original Native request")}</button>}</>}
      {!pending && !position.attached && <button type="button" className="button primary" disabled={lockedOut || !allowed || !loop} onClick={() => void mutate("start")}>{t("连接原Native任务", "Attach original Native task")}</button>}
      {position.attached && loop && <>
        <h3>{t("当前阶段", "Current stage")} · {loop.state}</h3><p>{loop.reason || t("沿原任务冻结范围与预算推进。", "Continue within the original frozen scope and budget.")}</p>
        <p data-native-provider-usage={loop.session.usage.provider_dispatches}>{t("已用Provider调用 / 工具 / 实验：", "Provider calls / tools / experiments used: ")}{loop.session.usage.provider_dispatches}/{loop.session.budget.max_provider_dispatches} · {loop.session.usage.tool_actions}/{loop.session.budget.max_tool_actions} · {loop.session.usage.experiments}/{loop.session.budget.max_experiments} · {t("剩余时间：", "Time remaining: ")}{loop.session.remaining_seconds.toFixed(1)}s</p>
        {loopPolls(loop) && <p role="status">{t("等待原Worker；自动刷新只读，不增加调用或预算。", "Waiting for the original worker; polling only reads and never adds calls or budget.")}</p>}
        {!pending && correctionCanAdvance(loop) && <button type="button" className="button primary" data-native-operation="advance" disabled={progressLocked || !allowed} onClick={() => void mutate("advance")}>{correctionNextLabel(loop)}</button>}
        {loop.proposals.map(p => <article key={p.id}><p>{t("原Provider记录：", "Original provider record: ")}{p.operation} · {p.status} · {p.id}</p><OriginalOutput proposalId={p.id} sessionId={loop.session_id} scope={scope} workspaceScope={workspaceScope} status={p.status} /></article>)}
        {receipts.map(r => <article key={r.sequence} data-native-receipt-status={r.status}>
          <h4>{t("Native修正收据：", "Native correction receipt: ")}{r.status}</h4>
          {r.intent && <p>{t("父生成 / 尝试 → 修正子记录：", "Parent generation / attempt → repair child: ")}{r.intent.parent.proposal_id} / {r.intent.parent.attempt_id} → {r.intent.proposal_id || t("未知", "Unknown")}</p>}
          {r.status === "unknown" ? <p role="status">{t("未知：缺少完整绑定的实际收据，不能据intent或started宣称修正成功。", "Unknown: a fully bound actual receipt is missing. Intent or started state cannot prove repair success.")}</p> : <>
            <p>{t("提交公式：", "Submitted formula: ")}<code>{r.receipt?.submitted_expression}</code></p>
            <p>{t("接受公式：", "Accepted formula: ")}{r.receipt?.accepted_expression === null ? t("无可执行输出", "No executable output") : <code>{r.receipt?.accepted_expression}</code>}</p>
            <p>{t("实际修正子记录 / 尝试 / 响应摘要：", "Actual repair child / attempt / response digest: ")}{r.receipt?.provider_association?.proposal_id} / {r.receipt?.provider_association?.attempt_id} / {r.receipt?.provider_association?.response_digest}</p>
          </>}
          <JsonDetails label={t("原公开intent与receipt", "Original public intent and receipt")} value={r} />
        </article>)}
        {target?.runId && !correctionCanAdvance(loop) && !loopPolls(loop) && <><p>{t("实际合成实验：", "Actual synthetic experiment: ")}{target.runId} · {loop.experiment?.state}</p>
          <button type="button" className="button primary" disabled={!!pending || !onOpenRun && !target.jobId} onClick={() => onOpenRun ? onOpenRun(target.runId!) : target.jobId && onOpenJob(target.jobId)}>{t("打开本次输出与审核", "Open actual output and review")}</button></>}
      </>}
    </>}
    <div className="button-row"><button type="button" className="button small" disabled={busy} onClick={() => {setError(""); setRefresh(v => v + 1);}}>{t("读取原Native任务（只读）", "Read original Native task (read only)")}</button></div>
    <details><summary>{t("冻结计划、原记录与停止任务", "Frozen plan, original records and stop task")}</summary>
      {journal && <JsonDetails label={t("Native独立恢复记录（不是执行许可）", "Independent Native recovery record (not execution permission)")} value={journal} />}
      {cancelJournal?.pending && <><p role="status">{cancelJournal.acknowledged ? t("原Session已停止；原步骤pending及未知状态完整保留。", "The original session is stopped. The original step pending and unknown state remain intact.") :
        cancelJournal.pending.state === "rejected" ? t("原停止请求已明确拒绝；停止与步骤记录均保留供宿主检查。", "The original stop request was explicitly rejected. Stop and step records are retained for host inspection.") :
        t("停止结果待确认；原步骤pending保留，仅用同一停止请求核对。", "The stop outcome is unconfirmed. The original step pending remains; reconcile only the same stop request.")}</p>
        {!cancelJournal.acknowledged && cancelJournal.pending.state === "unconfirmed" && <button type="button" className="button small" disabled={lockedOut || cancelBlocked || !loop} onClick={() => void stopNative()}>{t("核对原Native停止请求", "Reconcile original Native stop request")}</button>}
        <JsonDetails label={t("独立停止请求与原键", "Independent stop request and original key")} value={cancelJournal} /></>}
      {cancelBlocked && <p role="alert">{t("停止恢复记录无法核实；原记录保留，停止和推进均暂时阻断。", "The stop recovery record could not be verified. Original records remain; stop and advance actions are blocked.")}</p>}
      {support && <JsonDetails label={t("宿主支持与阻断原因", "Host support and blockers")} value={support} />}
      {loop && <JsonDetails label={t("原Loop完整公开记录", "Original complete public loop record")} value={loop} />}
      {position?.attached && cancellable && !cancelJournal?.pending && <button type="button" className="button small" disabled={lockedOut || cancelBlocked || !cancelJournal} onClick={() => void stopNative()}>{t("停止原Native任务", "Stop original Native task")}</button>}
    </details>
  </section>;
}
function CorrectionEntry({workspaceScope, researchId, baseRevisionId, providers, providerConfigId, onOpenJob, onOpenRun}: Props) {
  const { t } = useI18n();
  const scope = useMemo<CorrectionScope>(() => ({workspaceId: workspaceScope.workspaceId, researchId, providerConfigId}),
    [workspaceScope.workspaceId, researchId, providerConfigId]);
  const [position, setPosition] = useState<CorrectionPosition | null>(null);
  const [positionReady, setPositionReady] = useState(false), [storageBlocked, setStorageBlocked] = useState(false);
  const [support, setSupport] = useState<LoopSourceSupport | null>(null), [session, setSession] = useState<PublicResearchSession | null>(null);
  const [page, setPage] = useState<PublicResearchLoopPage | null>(null), [selectedLoop, setSelectedLoop] = useState<PublicResearchLoopV2 | null>(null);
  const [allowExperiment, setAllowExperiment] = useState(false), [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [childMutationLocked, setChildMutationLocked] = useState(false);
  const mounted = useRef(false), sending = useRef(false);
  const createSession = useRecoverableMutation<PublicResearchSession>(correctionMutationKey(scope, "session-create"), CORRECTION_SESSION_BASE);
  const createLoop = useRecoverableMutation<PublicResearchLoop | PublicResearchLoopV2>(correctionMutationKey(scope, "loop-create", position?.sessionId || "unselected"), CORRECTION_BASE);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {
    try {setPosition(readCorrectionPosition(localStorage, scope)); setPositionReady(true);}
    catch (e) {setError(describeError(e)); setStorageBlocked(true); setPositionReady(true);}
  }, [scope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSupport(null);
    if (baseRevisionId && providerConfigId) void api<LoopSourceSupport>(`${CORRECTION_BASE}/support`, {
      method: "POST", signal: abort.signal,
      body: JSON.stringify({research_id: researchId, base_revision_id: baseRevisionId, provider_config_id: providerConfigId, host_grant_id: null}),
    }).then(value => {if (workspaceScope.isCurrent(captured, abort.signal)) setSupport(value);})
      .catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [scope, baseRevisionId, refresh, workspaceScope, researchId, providerConfigId]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSession(null);
    if (position && !position.loopId) void api<PublicResearchSession>(`${CORRECTION_SESSION_BASE}/${encodeURIComponent(position.sessionId)}`, {signal: abort.signal})
      .then(value => {if (workspaceScope.isCurrent(captured, abort.signal)) setSession(requireCorrectionSession(value, scope, position));})
      .catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [position, scope, refresh, workspaceScope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setPage(null);
    void api<PublicResearchLoopPage>(`${CORRECTION_BASE}?research_id=${encodeURIComponent(researchId)}&limit=10&offset=${offset}`, {signal: abort.signal})
      .then(value => {if (workspaceScope.isCurrent(captured, abort.signal)) setPage(value);})
      .catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [scope, offset, refresh, workspaceScope, researchId]);
  async function start(replay = false) {
    if (sending.current) return;
    sending.current = true; setBusy(true); setError("");
    const captured = workspaceScope.capture();
    try {
      let plan: CorrectionStartPlan;
      if (replay) {
        plan = correctionPendingPlan(createSession.pending, scope);
      } else {
        if (!support || !providers) throw new Error(t("先等待宿主来源检查。", "Wait for the host source check."));
        plan = correctionStartPlan(researchId, baseRevisionId, providerConfigId, support, providers, allowExperiment);
      }
      const value = await createSession.execute(replay ? undefined : correctionSessionRequest(scope, plan), {plan});
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      const next: CorrectionPosition = {...scope, version: 1, sessionId: value.id, loopId: "", plan};
      requireCorrectionSession(value, scope, next);
      createSession.acknowledge(() => saveCorrectionPosition(localStorage, next));
      setPosition(next); setSession(value); setSelectedLoop(null);
    } catch (e) {if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e));}
    finally {sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false);}
  }
  async function build(replay = false) {
    if (sending.current || !position) return;
    sending.current = true; setBusy(true); setError("");
    const captured = workspaceScope.capture();
    try {
      if (!replay && !session) throw new Error(t("会话尚未核实。", "The session has not been verified."));
      const body = replay ? undefined : correctionLoopRequest(session!, scope, position, crypto.randomUUID());
      const response = await createLoop.execute(body, {session_id: position.sessionId, source_digest: position.plan.sourceDigest});
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      const value = requireCorrectionLoop(response, scope, position);
      const next = {...position, loopId: value.id};
      createLoop.acknowledge(() => saveCorrectionPosition(localStorage, next));
      setPosition(next); setSelectedLoop(value); setRefresh(v => v + 1);
    } catch (e) {if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e));}
    finally {sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false);}
  }
  function selectHistory(value: PublicResearchLoopV2) {
    try {
      const next = correctionPositionFromLoop(value, scope);
      if (position) clearCorrectionPosition(localStorage, position);
      saveCorrectionPosition(localStorage, next); setPosition(next); setSelectedLoop(value); setError("");
    } catch (e) {setError(describeError(e));}
  }
  function newTask() {
    if (!position) return;
    try {clearCorrectionPosition(localStorage, position); setPosition(null); setSelectedLoop(null); setSession(null); setRefresh(v => v + 1);}
    catch (e) {setError(describeError(e));}
  }
  const mutationLocked = busy || childMutationLocked || storageBlocked || !positionReady || !createSession.ready || createSession.blocked || !!createSession.pending ||
    !createLoop.ready || createLoop.blocked || !!createLoop.pending;
  const config = providers?.providers.find(item => item.config_id === providerConfigId);
  const startAllowed = !!support?.correction_supported && support.generation_available && config?.mode === "fixture" &&
    config.dispatchable && providers?.allow_fixture && !providers.dispatch_paused;
  const history = page?.items.filter((item): item is PublicResearchLoopV2 => item.schema_version === 2 && item.session.config.provider_config_id === providerConfigId && item.policy.correction_mode === "deterministic") || [];
  const canNewTask = !!position && (selectedLoop && ["awaiting_handoff", "awaiting_input", "stopped", "completed", "cancelled", "exhausted", "interrupted"].includes(selectedLoop.state) ||
    !position.loopId && !!session && (session.remaining_seconds <= 0 || session.current_source.revision_id !== baseRevisionId));
  return <section aria-label={t("确定性修正研究任务", "Deterministic correction research task")}>
    {error && <p role="alert">{error}</p>}
    {!providers?.providers.length && <p role="status">{t("默认没有 Provider。请由宿主生成 synthetic 示例配置，并使用 --provider-config 和 --allow-fixture-provider 启动；浏览器不能开启模型权限。", "No provider is configured by default. Generate the host synthetic fixture configuration and start with --provider-config and --allow-fixture-provider. The browser cannot enable model permission.")} <code>python -B -m paper_alpha.synthetic_revision_fixture configure --out NEW</code></p>}
    {providers?.providers.length && !providerConfigId ? <p>{t("先在上方选择宿主示例 Provider。", "Select a host fixture provider above.")}</p> : null}
    {config?.mode === "codex_cli" && <p role="status">{t("此来源尚无兼容的真实模型生成许可。请使用显式 synthetic 示例；不会退回其他来源或模型。", "This source has no compatible real-model generation grant. Use the explicit synthetic fixture; no alternative source or model is substituted.")}</p>}
    {support && <p role="status">{support.reason_code} · {support.message}</p>}
    <MutationRecovery name={t("修正会话", "correction session")} pending={createSession.pending} error={createSession.error} busy={busy} onRetry={() => void start(true)} onDismiss={createSession.dismissRejected} />
    <MutationRecovery name={t("修正循环", "correction loop")} pending={createLoop.pending} error={createLoop.error} busy={busy} onRetry={() => void build(true)} onDismiss={createLoop.dismissRejected} />
    {!position && <fieldset disabled={mutationLocked}>
      <p>{t("固定范围：一个候选、一次生成、最多一次宿主等价修正。策略由宿主返回，刷新不会追加预算。", "Fixed scope: one candidate, one generation and at most one host equivalent correction. The host supplies the policy; refreshing does not add budget.")}</p>
      <label><input type="checkbox" checked={allowExperiment} onChange={e => setAllowExperiment(e.target.checked)} />{t("明确允许随后创建一次合成数据验证期实验", "Explicitly allow one subsequent synthetic validation experiment")}</label>
      <p>{t("本次会话从创建起最多600秒；生成最多120秒。允许实验时最多3次工具动作、1次实验，实验最多10步/2次技术失败/180秒；不允许实验时最多2次工具动作、0次实验。", "The session lasts up to 600 seconds from creation; generation up to 120 seconds. With an experiment: at most 3 tool actions and 1 experiment, with 10 experiment steps / 2 technical failures / 180 seconds. Without an experiment: at most 2 tool actions and 0 experiments.")}</p>
      <button type="button" className="button primary" disabled={!startAllowed} onClick={() => void start()}>{t("创建有界研究会话", "Create bounded research session")}</button>
    </fieldset>}
    {position && !position.loopId && <>
      <p>{t("已保存真实会话：", "Saved actual session: ")}{position.sessionId} · {t("冻结修订：", "Frozen revision: ")}{position.plan.baseRevisionId}</p>
      {session && <p>{t("状态与剩余时间：", "State and remaining time: ")}{session.effective_state} · {session.remaining_seconds.toFixed(1)}s</p>}
      <button type="button" className="button primary" disabled={mutationLocked || !session || position.plan.baseRevisionId !== baseRevisionId || session.remaining_seconds <= 0} onClick={() => void build()}>{t("创建确定性修正任务", "Create deterministic correction task")}</button>
    </>}
    {position?.loopId && <CorrectionLoopDetail key={`${workspaceScope.key}:${position.loopId}`} position={position} scope={scope}
      workspaceScope={workspaceScope} refresh={refresh} onRead={setSelectedLoop} onMutationLock={setChildMutationLocked} onOpenJob={onOpenJob} onOpenRun={onOpenRun} />}
    <div className="button-row"><button type="button" className="button small" disabled={busy} onClick={() => {setError(""); setRefresh(v => v + 1);}}>{t("刷新原任务（只读）", "Refresh original task (read only)")}</button></div>
    <details><summary>{t("历史任务与冻结配置", "Task history and frozen configuration")}</summary>
      {support && <JsonDetails label={t("宿主返回的来源支持与策略引用", "Host source support and policy references")} value={support} />}
      {position && <JsonDetails label={t("本机恢复位置（不是执行许可）", "Local recovery position (not execution permission)")} value={position} />}
      {page && <p>{t(`本页共${page.items.length}条保存记录；匹配的v2任务${history.length}条。`, `${page.items.length} saved records on this page; ${history.length} matching v2 tasks.`)}</p>}
      {history.map(item => <p key={item.id}><button type="button" className="text-button" disabled={mutationLocked} onClick={() => selectHistory(item)}>{formatDate(item.created_at)} · {item.state} · {item.id.slice(-12)}</button></p>)}
      <div className="button-row"><button type="button" className="button small" disabled={offset === 0 || busy} onClick={() => setOffset(v => Math.max(0, v - 10))}>{t("上一页", "Previous page")}</button><button type="button" className="button small" disabled={!page || offset + 10 >= page.total || offset >= 10000 || busy} onClick={() => setOffset(v => v + 10)}>{t("下一页", "Next page")}</button></div>
      {canNewTask && <><p>{t("另建任务会申请独立的新会话预算；原会话、失败与实验记录全部保留。", "A new task requests a separate session budget. Original sessions, failures and experiment records are retained.")}</p><button type="button" className="button small" disabled={mutationLocked} onClick={newTask}>{t("准备一个新任务", "Prepare a new task")}</button></>}
    </details>
  </section>;
}

function CorrectionLoopDetail({position, scope, workspaceScope, refresh, onRead, onMutationLock, onOpenJob, onOpenRun}: {
  position: CorrectionPosition; scope: CorrectionScope; workspaceScope: WorkspaceRequestScope;
  refresh: number; onRead: (value: PublicResearchLoopV2 | null) => void; onMutationLock: (value: boolean) => void; onOpenJob: (id: string) => void; onOpenRun?: (id: string) => void;
}) {
  const { t } = useI18n();
  const [loop, setLoop] = useState<PublicResearchLoopV2 | null>(null), [error, setError] = useState("");
  const [pending, setPending] = useState<PendingMutation | null>(null), [journalReady, setJournalReady] = useState(false), [journalBlocked, setJournalBlocked] = useState(false);
  const [busy, setBusy] = useState(false), [nonce, setNonce] = useState(0);
  const mounted = useRef(false), sending = useRef(false), readEpoch = useRef(0);
  const path = `${CORRECTION_BASE}/${encodeURIComponent(position.loopId)}/advance`;
  const key = correctionMutationKey(scope, "advance", position.loopId);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {
    try {setPending(readMutation(localStorage, key, path)); setJournalReady(true);}
    catch (e) {setError(describeError(e)); setJournalBlocked(true); setJournalReady(true);}
  }, [key, path]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController(), epoch = ++readEpoch.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const response = await api<PublicResearchLoop | PublicResearchLoopV2>(`${CORRECTION_BASE}/${encodeURIComponent(position.loopId)}`, {signal: abort.signal});
        if (!workspaceScope.isCurrent(captured, abort.signal) || readEpoch.current !== epoch) return;
        const value = requireCorrectionLoop(response, scope, position);
        setLoop(value); onRead(value);
        if (loopPolls(value)) timer = setTimeout(() => void read(), 2000);
      } catch (e) {
        if (workspaceScope.isCurrent(captured, abort.signal) && readEpoch.current === epoch) {setLoop(null); onRead(null); setError(describeError(e));}
      }
    }
    void read();
    return () => {abort.abort(); if (timer) clearTimeout(timer);};
  }, [position, scope, workspaceScope, refresh, nonce, onRead]);
  useEffect(() => {onMutationLock(busy || !!pending || journalBlocked || !journalReady); return () => onMutationLock(false);}, [busy, pending, journalBlocked, journalReady, onMutationLock]);
  async function advance() {
    if (sending.current || (!loop && !pending) || !journalReady || journalBlocked) return;
    sending.current = true; setBusy(true); setError(""); ++readEpoch.current;
    const captured = workspaceScope.capture();
    let item: PendingMutation | null = null;
    try {
      item = prepareCorrectionAdvance(localStorage, key, path, loop, crypto.randomUUID(), position.loopId);
      setPending(item);
      const response = await api<PublicResearchLoop | PublicResearchLoopV2>(path, {method: "POST", body: JSON.stringify(item.body)});
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      const value = requireCorrectionLoop(response, scope, position);
      acknowledgeMutation(localStorage, key, path, item);
      setPending(null); setLoop(value); onRead(value); setNonce(v => v + 1);
    } catch (e) {
      if (item && isExplicitMutationRejection(e)) {
        try {rejectMutation(localStorage, key, path, item);} catch { /* Preserve uncertain original journal. */ }
      }
      if (mounted.current && workspaceScope.isCurrent(captured)) {
        try {setPending(readMutation(localStorage, key, path));} catch {setJournalBlocked(true);}
        setError(describeError(e)); setNonce(v => v + 1);
      }
    } finally {sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false);}
  }
  function dismiss() {
    if (!pending || pending.state !== "rejected") return;
    try {acknowledgeMutation(localStorage, key, path, pending); setPending(null); setNonce(v => v + 1);}
    catch (e) {setError(describeError(e));}
  }
  const target = loop ? correctionReviewTarget(loop) : null;
  const corrections = loop?.actions.flatMap(action => action.correction ? [action.correction] : []) || [];
  return <section aria-label={t("修正循环详情", "Correction loop detail")} data-correction-loop-id={position.loopId}>
    {error && <p role="alert">{error}</p>}
    <MutationRecovery name={t("修正步骤", "correction step")} pending={pending} error="" busy={busy} onRetry={() => void advance()} onDismiss={dismiss} />
    {!loop && <p role="status">{t("正在核验原循环；读取失败不会创建替代任务。", "Verifying the original loop; a failed read does not create a substitute task.")}</p>}
    {loop && <>
      <h3>{t("当前阶段", "Current stage")} · {loop.state}</h3>
      <p>{loop.reason || t("沿用本次来源、策略和预算逐步推进。", "Continue with this source, policy and budget.")}</p>
      <p>{t("原始修订：", "Original revision: ")}{position.plan.baseRevisionId} · {t("策略：", "Policy: ")}{loop.policy.correction_policy_id} · {t("最多一次等价表示修正；不改变字段、窗口取值或论文依据。", "At most one equivalent representation correction; fields, window values and paper evidence are unchanged.")}</p>
      <p>{t("已用生成 / 工具 / 实验 / 技术失败：", "Generation / tools / experiments / technical failures used: ")}
        {loop.session.usage.provider_dispatches}/{loop.session.budget.max_provider_dispatches} · {loop.session.usage.tool_actions}/{loop.session.budget.max_tool_actions} · {loop.session.usage.experiments}/{loop.session.budget.max_experiments} · {loop.session.usage.failures}/{loop.session.budget.max_failures}
        {t("；剩余会话时间：", "; session time remaining: ")}{loop.session.remaining_seconds.toFixed(1)}s</p>
      {loopPolls(loop) && <p role="status">{t("正在等待原Worker；自动刷新只读取，不新增步骤或预算。", "Waiting for the original worker; polling only reads, without adding steps or budget.")}</p>}
      {!pending && correctionCanAdvance(loop) && <button type="button" className="button primary" disabled={busy || !journalReady || journalBlocked} onClick={() => void advance()}>{correctionNextLabel(loop)}</button>}
      {!correctionCanAdvance(loop) && !loopPolls(loop) && !target?.runId && <p>{t("本轮已停止或等待人工补充。缺字段、证据不足和未知错误不会由表示修正规则掩盖。", "This task is stopped or requires user input. Missing fields, insufficient evidence and unknown errors are not masked by representation correction.")}</p>}
      {loop.proposals.map(proposal => <article key={proposal.id}>
        <p>{t("原生成记录：", "Original generation record: ")}{proposal.status} · {proposal.id.slice(-12)}</p>
        {proposal.error && <p>{proposal.error.code} · {proposal.error.message}</p>}
        <OriginalOutput proposalId={proposal.id} sessionId={loop.session_id} scope={scope} workspaceScope={workspaceScope} status={proposal.status} />
      </article>)}
      {corrections.map(correction => <article key={correction.sequence} aria-label={t(`修正第${correction.sequence}步`, `Correction step ${correction.sequence}`)}>
        <h4>{t("宿主修正：", "Host correction: ")}{correction.effect_status}</h4>
        {correction.diagnostics.map((item, index) => <p key={index}>{item.code} · {item.message}</p>)}
        {correction.receipt ? <>
          <p>{t("提交公式：", "Submitted formula: ")}<code>{correction.receipt.submitted_expression}</code></p>
          <p>{t("接受公式：", "Accepted formula: ")}{correction.receipt.accepted_expression === null ? t("未产生可执行输出", "No executable output") : <code>{correction.receipt.accepted_expression}</code>}</p>
          <p>{t("等价性状态：", "Equivalence status: ")}{correction.equivalence_status}{t("；论文语义仍未审核。", "; paper semantics remain unreviewed.")}</p>
          <JsonDetails label={t("策略、错误与原始修正收据摘要", "Policy, diagnostics and original correction receipt digests")} value={correction} />
        </> : <p>{t("尚无完整修正收据。started/unknown不表示成功，也不会自动重跑。", "No complete correction receipt exists. Started/unknown does not mean success and is never automatically rerun.")}</p>}
      </article>)}
      {target?.runId && <><p>{t("已绑定实际实验：", "Actual bound experiment: ")}{target.runId} · {loop.experiment?.state}</p>
        <button type="button" className="button primary" onClick={() => onOpenRun ? onOpenRun(target.runId!) : target.jobId && onOpenJob(target.jobId)} disabled={!onOpenRun && !target.jobId}>{t("打开本次输出与审核", "Open actual output and review")}</button>
        <p>{t("显示的是合成验证期实验；计算完成和表示等价均不代表预测表现获得批准。", "This is a synthetic validation experiment. Computation and representation equivalence do not approve predictive performance.")}</p></>}
      {target?.jobId && <details><summary>{t("原实验与完整循环记录", "Original experiment and complete loop record")}</summary>
        <button type="button" className="button small" onClick={() => onOpenJob(target.jobId!)}>{t("打开原研究执行任务", "Open original research job")}</button>
        <JsonDetails value={loop} />
      </details>}
    </>}
  </section>;
}
function OriginalOutput({proposalId, sessionId, scope, workspaceScope, status}: {proposalId: string; sessionId: string; scope: CorrectionScope; workspaceScope: WorkspaceRequestScope; status: string}) {
  const { t } = useI18n();
  const [value, setValue] = useState<ResearchV2ProposalDetail | null>(null), [error, setError] = useState("");
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController(); setValue(null); setError("");
    void api<ResearchV2ProposalDetail>(`/research-v2/proposals/${encodeURIComponent(proposalId)}`, {signal: abort.signal}).then(response => {
      if (!workspaceScope.isCurrent(captured, abort.signal)) return;
      if (response.id !== proposalId || response.session_id !== sessionId || response.source_kind !== "research_revision" || response.provider_config_id !== scope.providerConfigId)
        throw new Error(t("原输出与当前任务不一致。", "The original output does not match this task."));
      setValue(response);
    }).catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [proposalId, sessionId, scope, workspaceScope, status, t]);
  return <details><summary>{t("查看原生成公式、校验与证据", "Inspect original formula, validation and evidence")}</summary>
    {error && <p role="alert">{error}</p>}
    {value?.attempts.map(attempt => <article key={attempt.id}>
      <p>{attempt.status}{attempt.receipt?.fixture && t(" · synthetic工程示例，无模型推理", " · synthetic engineering fixture, no model inference")}</p>
      {attempt.output?.draft?.candidates.map(candidate => <p key={candidate.id}>{candidate.id} · <code>{candidate.expression}</code> · {candidate.origin}</p>)}
      {attempt.error && <p>{attempt.error.code} · {attempt.error.message}</p>}
      {attempt.validation && <JsonDetails label={t("原完整机器校验", "Original full machine validation")} value={attempt.validation} />}
      {attempt.output && <JsonDetails label={t("原输出与保留论文依据", "Original output and retained paper evidence")} value={attempt.output} />}
      {attempt.receipt && <p>{t("已观测token：", "Observed tokens: ")}{attempt.receipt.usage.input_tokens ?? t("未知", "Unknown")} / {attempt.receipt.usage.output_tokens ?? t("未知", "Unknown")}{t("；收据不能替代人工研究评审。", "; a receipt does not replace research review.")}</p>}
    </article>)}
  </details>;
}
