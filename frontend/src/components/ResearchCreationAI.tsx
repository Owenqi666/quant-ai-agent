import { useCallback, useEffect, useRef, useState } from "react";
import type { AcceptedMaterialDraft, HostCapability, PaperSelection, PublicExperiment, PublicMaterialDetail, PublicMaterialEngineering, PublicMaterialVersion, PublicResearchSession, ResearchSourcePreview, ResearchV2ProposalDetail, ResearchV2ProposalSummary } from "../generated/api-contract";
import type { Dataset, JsonObject, Paper } from "../domain";
import type { CreationStage } from "../creationDrafts";
import { api, ApiError, post } from "../api";
import { describeError, shortId } from "../domain";
import { useI18n } from "../i18n";
import { experimentHandoffStatus } from "../experimentHandoffStatus";
import { acknowledgeMutation, rejectMutation, stageMutation } from "../reviewRecovery";
import type { PendingMutation } from "../reviewRecovery";
import { notifyDraftChanged } from "../drafts";
import { applyEngineeringConfiguration, creationFlowMutationKey, creationInputKey, creationMutationBody, generationReconciliationToken, emptyCreationFlow, nextExperimentAction, persistCreationFlow, proposalExecutionKind, readCreationFlowMutation } from "../researchCreationFlow";
import type { ResearchCreationFlowState } from "../researchCreationFlow";
import MaterialAdoptionPanel from "./MaterialAdoptionPanel";
import CreationProposalDiagnostics from "./CreationProposalDiagnostics";
import MutationRecovery from "./MutationRecovery";
import { Field, JsonDetails } from "./ui";
import type { EngineeringDisposition } from "../materialEngineering";

export type CreationCapability = HostCapability;
export interface CreationExperimentTarget { researchId: string; revisionId: string; runId?: string; jobId: string; experimentId: string }
interface Props {
  workspaceId: string; draftKey: string; paper: Paper | null; dataset?: Dataset; title: string;
  evaluation: JsonObject; stage: CreationStage; configurationConfirmed: boolean; methodPolicy: JsonObject;
  value?: ResearchCreationFlowState; onChange: (next: ResearchCreationFlowState) => void;
  onStageChange: (stage: CreationStage) => void; onOpenExperiment: (target: CreationExperimentTarget) => void;
  onBusyChange?: (busy: boolean) => void;
  onApplyAuthorizedConfiguration?: (policy: JsonObject) => void;
}
type CreationSourcePreview = ResearchSourcePreview & { paper_selection?: PaperSelection | null };
const activeProposal = (proposal: ResearchV2ProposalDetail | null) => !!proposal && ["queued", "running", "validating", "retry_wait", "cancelling"].includes(proposal.status);
const empty = emptyCreationFlow();
const sessionBudget = { max_provider_dispatches: 1, max_tool_actions: 8, max_experiments: 1, max_failures: 2, max_seconds: 3600 };
const operationTitle = (operation: string) => operation === "extract_material" ? "论文材料抽取" : "候选生成";

export function GenerationSourceNotice({ hasGeneration, configurationConfirmed, generationCurrent }: { hasGeneration: boolean; configurationConfirmed: boolean; generationCurrent: boolean }) {
  const { t } = useI18n();
  if (!hasGeneration) return null;
  if (!configurationConfirmed) return <p role="status">{t("请重新核对并确认当前配置，再判断旧候选是否仍适用；重新确认不调用模型。", "Recheck and confirm the current configuration before deciding whether the previous candidate still applies; reconfirming makes no model call.")}</p>;
  return !generationCurrent ? <p role="alert">{t("材料、数据或配置已变化，旧候选保留但不能交接。请显式生成当前来源的候选。", "Material, data or settings changed. Old candidates remain recorded but cannot be handed off; explicitly generate for the current source.")}</p> : null;
}

export default function ResearchCreationAI(props: Props) {
  const { t } = useI18n();
  const { workspaceId, draftKey, paper, dataset, title, evaluation, stage, configurationConfirmed, methodPolicy } = props;
  const flow = props.value ?? empty;
  const effectiveMethodPolicy = flow.engineeringMethodPolicy ?? methodPolicy;
  const engineeringOnly = effectiveMethodPolicy.kind === "daily_formula_engineering_v1";
  const flowRef = useRef(flow); flowRef.current = flow;
  const mutationKey = creationFlowMutationKey(workspaceId, draftKey);
  const [capabilities, setCapabilities] = useState<CreationCapability[]>([]);
  const [preview, setPreview] = useState<{ key: string; value: CreationSourcePreview } | null>(null);
  const [extract, setExtract] = useState<ResearchV2ProposalDetail | null>(null), [generate, setGenerate] = useState<ResearchV2ProposalDetail | null>(null);
  const [material, setMaterial] = useState<PublicMaterialDetail | null>(null), [version, setVersion] = useState<PublicMaterialVersion | null>(null);
  const [experiment, setExperiment] = useState<PublicExperiment | null>(null);
  const [pending, setPending] = useState<PendingMutation | null>(null), [blocked, setBlocked] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [ready, setReady] = useState(false);
  const [nonce, setNonce] = useState(0), [reviewedCandidate, setReviewedCandidate] = useState("");
  const lock = useRef(false), mounted = useRef(false), readEpoch = useRef(0);
  // Confirmation policy is re-read after refresh. The frozen mutation already owns it;
  // excluding that transient cache lets the exact original request be acknowledged.
  const inputKey = creationInputKey({ workspaceId, draftKey, paperId: paper?.id, paperDigest: paper?.sha256, datasetId: dataset?.id, dataDigest: dataset?.sha256, title, evaluation, pages: flow.selectedPages, focus: flow.researchFocus ?? "", provider: flow.providerConfigId, grant: flow.hostGrantId });
  const liveInput = useRef(inputKey); liveInput.current = inputKey;
  const operationInput = useRef(inputKey);
  const extractKey = creationInputKey({ paperId: paper?.id, paperDigest: paper?.sha256, pages: flow.selectedPages, focus: flow.researchFocus ?? "", provider: flow.providerConfigId, grant: flow.hostGrantId });
  const generateKey = creationInputKey({ materialId: flow.materialId, versionId: flow.materialVersionId, datasetId: dataset?.id, dataDigest: dataset?.sha256, title, evaluation, methodPolicy: effectiveMethodPolicy, provider: flow.providerConfigId, grant: flow.hostGrantId });
  const extractCurrent = flow.extraction?.inputKey === extractKey;
  const generationCurrent = !!flow.generation && flow.generation.inputKey === generateKey && extractCurrent;
  const currentExtract = extract?.id === flow.extraction?.proposalId ? extract : null;
  const currentGenerate = generate?.id === flow.generation?.proposalId ? generate : null;
  const currentMaterial = material?.id === flow.materialId ? material : null;
  const currentVersion = version?.id === flow.materialVersionId ? version : null;
  const currentExperiment = experiment?.id === flow.experimentId ? experiment : null;
  const handoffStatus = currentExperiment ? experimentHandoffStatus(currentExperiment) : null;
  const selectedCapability = capabilities.find(item => item.provider_config_id === flow.providerConfigId && item.host_grant_id === flow.hostGrantId && item.operation === (stage === "materials" || stage === "evidence" ? "extract_material" : "generate_candidates"));
  const loading = busy || !ready || blocked || !!pending;
  useEffect(() => { mounted.current = true; try { setPending(readCreationFlowMutation(localStorage, mutationKey)); } catch (cause) { setError(describeError(cause)); setBlocked(true); } setReady(true); return () => { mounted.current = false; }; }, [mutationKey]);
  useEffect(() => { props.onBusyChange?.(loading); return () => props.onBusyChange?.(false); }, [loading, props.onBusyChange]);
  const refresh = useCallback(async () => {
    const epoch = ++readEpoch.current, snapshot = liveInput.current, refs = flowRef.current;
    const current = () => mounted.current && epoch === readEpoch.current && snapshot === liveInput.current;
    const requests: Promise<unknown>[] = [api<{ items: CreationCapability[] }>("/research-v2/capabilities").then(result => { if (current()) setCapabilities(result.items); })];
    if (refs.extraction?.proposalId) requests.push(api<ResearchV2ProposalDetail>(`/research-v2/proposals/${refs.extraction.proposalId}`).then(result => { if (current()) setExtract(result); }));
    if (refs.generation?.proposalId) requests.push(api<ResearchV2ProposalDetail>(`/research-v2/proposals/${refs.generation.proposalId}`).then(result => { if (current()) setGenerate(result); }));
    if (refs.materialId) requests.push(api<PublicMaterialDetail>(`/research-v2/materials/${refs.materialId}`).then(result => { if (current()) setMaterial(result); }));
    if (refs.materialId && refs.materialVersionId) requests.push(api<PublicMaterialVersion>(`/research-v2/materials/${refs.materialId}/versions/${refs.materialVersionId}`).then(result => { if (current()) setVersion(result); }));
    if (refs.experimentId) requests.push(api<PublicExperiment>(`/research-v2/experiments/${refs.experimentId}`).then(result => { if (current()) setExperiment(result); }));
    const results = await Promise.allSettled(requests);
    const failed = results.find(result => result.status === "rejected");
    if (current() && failed?.status === "rejected") setError(describeError(failed.reason));
  }, []);
  useEffect(() => { void refresh(); }, [inputKey, flow.extraction?.proposalId, flow.generation?.proposalId, flow.materialId, flow.materialVersionId, flow.experimentId, nonce, refresh]);
  useEffect(() => {
    if (!activeProposal(currentExtract) && !activeProposal(currentGenerate) && !handoffStatus?.poll) return;
    let stopped = false;
    let timer: number | undefined;
    // One read at a time; slow responses do not starve all readback by repeatedly
    // superseding its epoch. Unmount/source changes invalidate the old loop.
    const schedule = () => { timer = window.setTimeout(async () => {
      await refresh();
      if (!stopped) schedule();
    }, 2000); };
    schedule();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [inputKey, currentExtract?.id, currentExtract?.status, currentGenerate?.id, currentGenerate?.status,
    currentExperiment?.id, currentExperiment?.job?.state, currentExperiment?.reservation.status,
    currentExperiment?.reservation.outcome?.status, handoffStatus?.poll, refresh]);
  function save(next: ResearchCreationFlowState, expected = flowRef.current) {
    if (creationInputKey([next.selectedPages, next.researchFocus, next.providerConfigId, next.hostGrantId]) !==
      creationInputKey([expected.selectedPages, expected.researchFocus, expected.providerConfigId, expected.hostGrantId])) next = { ...next, engineeringMethodPolicy: undefined };
    persistCreationFlow(localStorage, draftKey, next, expected);
    flowRef.current = next; props.onChange(next); notifyDraftChanged(draftKey);
  }
  async function perform(action: () => Promise<void>) {
    if (lock.current || blocked || !ready) return false;
    let succeeded = false;
    lock.current = true; operationInput.current = liveInput.current; setBusy(true); setError("");
    try { await action(); succeeded = true; }
    catch (cause) { if (mounted.current) setError(describeError(cause)); }
    finally { lock.current = false; if (mounted.current) { setBusy(false); setNonce(value => value + 1); } }
    return succeeded;
  }
  function resultFlow(action: string, result: unknown, context: Record<string, unknown>): ResearchCreationFlowState {
    const before = context.flow as ResearchCreationFlowState;
    if (action === "session_extract" || action === "session_generate") {
      const session = result as PublicResearchSession;
      const reference = { sessionId: session.id, sourceDigest: String(context.sourceDigest), inputKey: String(context.sourceKey) };
      return action === "session_extract" ? { ...before, extraction: reference, materialId: undefined, materialVersionId: undefined, materialDigest: undefined, engineeringMethodPolicy: undefined, generation: undefined, experimentId: undefined } : { ...before, generation: reference, experimentId: undefined };
    }
    if (action === "proposal_extract" || action === "proposal_generate") {
      const proposal = result as ResearchV2ProposalSummary, key = action === "proposal_extract" ? "extraction" : "generation";
      if (!before[key] || proposal.session_id !== before[key]?.sessionId) throw new Error("提案会话不匹配，保留原恢复请求。");
      return { ...before, [key]: { ...before[key], proposalId: proposal.id } };
    }
    if (action === "import_material") return { ...before, materialId: (result as PublicMaterialDetail).id };
    if (action === "adopt_material") { const adopted = result as PublicMaterialVersion; return { ...before, materialId: adopted.material_id, materialVersionId: adopted.id, materialDigest: adopted.digest, engineeringMethodPolicy: undefined, generation: undefined, experimentId: undefined }; }
    if (action === "configure_engineering") return applyEngineeringConfiguration(before, result as PublicMaterialEngineering);
    if (action === "create_experiment" || action === "advance_experiment") return { ...before, experimentId: (result as PublicExperiment).id };
    if (action === "cancel_session" || action === "reconcile_session") return before;
    throw new Error("无法识别 AI 恢复操作，原记录已保留。");
  }
  async function send(action?: string, path?: string, body?: Record<string, unknown>, extra: Record<string, unknown> = {}) {
    if (!mounted.current || operationInput.current !== liveInput.current) throw new Error("当前来源已变化，未发送新请求；原记录保持不变。");
    let receipt = readCreationFlowMutation(localStorage, mutationKey);
    if (!receipt) {
      if (!action || !path || !body) throw new Error("没有待确认 AI 请求。");
      receipt = stageMutation(localStorage, mutationKey, path, body, crypto.randomUUID(), { action, inputKey: liveInput.current, flow: structuredClone(flowRef.current), ...extra });
    } else if (action) throw new Error("已有待确认 AI 请求，请先安全重试原请求。");
    setPending(receipt);
    if (receipt.state === "rejected") throw new Error("原请求已明确拒绝，请解除后修改。");
    let result: unknown;
    try { result = await post<unknown>(receipt.path, creationMutationBody(receipt)); }
    catch (cause) {
      if (cause instanceof ApiError && !cause.contractViolation && cause.status >= 400 && cause.status < 500 && cause.status !== 409) {
        const rejected = rejectMutation(localStorage, mutationKey, receipt.path, receipt);
        if (mounted.current && rejected) setPending(rejected);
      }
      throw cause;
    }
    if (!mounted.current || receipt.context?.inputKey !== liveInput.current) throw new Error("来源已变化；原请求结果已保留，未覆盖当前编辑。请恢复原输入后确认。");
    const next = resultFlow(String(receipt.context.action), result, receipt.context);
    save(next, receipt.context.flow as ResearchCreationFlowState);
    acknowledgeMutation(localStorage, mutationKey, receipt.path, receipt);
    setPending(null);
    return result;
  }
  const paperSource = () => ({ kind: "paper_selection", paper_id: paper?.id, selected_pages: flowRef.current.selectedPages });
  const acceptedSource = () => ({ kind: "accepted_material", material_id: flowRef.current.materialId, version_id: flowRef.current.materialVersionId, dataset_id: dataset?.id, evaluation, method_policy: effectiveMethodPolicy, destination: { kind: "new_research", research_id: null, base_revision_id: null }, title: title.trim() });
  async function previewSource(generating: boolean) {
    if (!paper || !flow.selectedPages.length || !selectedCapability?.available) throw new Error("请选择论文页与当前可用 Provider。");
    if (generating && (!configurationConfirmed || !flow.materialVersionId || !extractCurrent)) throw new Error("请先采纳当前材料，并确认完整数据及时间配置。");
    const key = generating ? generateKey : extractKey, snapshot = liveInput.current;
    const result = await post<CreationSourcePreview>("/research-v2/source-preview", { source: generating ? acceptedSource() : paperSource(), provider_config_id: flow.providerConfigId, ...(flow.hostGrantId ? { host_grant_id: flow.hostGrantId } : {}) });
    if (mounted.current && snapshot === liveInput.current) setPreview({ key, value: result });
  }
  async function start(generating: boolean) {
    const key = generating ? generateKey : extractKey, branch = generating ? "generation" : "extraction", op = generating ? "generate_candidates" : "extract_material";
    if (!preview || preview.key !== key || !selectedCapability?.available || (generating && !configurationConfirmed)) throw new Error("来源或权限已变化，请重新预览并确认。");
    const source = generating ? acceptedSource() : paperSource();
    const existing = flowRef.current[branch];
    if (existing?.proposalId && existing.inputKey === key) throw new Error("本来源已有生成请求；请读取现有结果，不重复调用。");
    if (!existing || existing.inputKey !== key) await send(generating ? "session_generate" : "session_extract", "/research-v2/sessions", {
      source, provider_config_id: flow.providerConfigId, ...(flow.hostGrantId ? { host_grant_id: flow.hostGrantId } : {}),
      expected_source_digest: preview.value.source.source_digest, expected_provider_config_digest: preview.value.config.provider_config_digest,
      purpose: operationTitle(op), budget: { ...sessionBudget, max_seconds: selectedCapability.deadline_at ? Math.max(1, Math.min(sessionBudget.max_seconds, Math.floor((Date.parse(selectedCapability.deadline_at) - Date.now()) / 1000) - 5)) : sessionBudget.max_seconds, max_experiments: generating ? 1 : 0 },
    }, { sourceKey: key, sourceDigest: preview.value.source.source_digest });
    const reference = flowRef.current[branch]!;
    const session = await api<PublicResearchSession>(`/research-v2/sessions/${reference.sessionId}`);
    await send(generating ? "proposal_generate" : "proposal_extract", "/research-v2/proposals", {
      schema_version: 2, contract_id: "research_provider_v2", operation: op, session_id: session.id, expected_session_digest: session.state_digest,
      source, expected_source_digest: reference.sourceDigest, provider_config_id: flow.providerConfigId,
      instruction: generating ? "Generate one faithful candidate from the accepted material. Preserve unresolved rules and abstain instead of inventing data or method choices." : "Extract at most two source-grounded hypotheses from the selected pages. Mark missing mechanisms and rules unresolved; do not invent information. The following is the user research focus, not a change to the output contract: " + (flow.researchFocus?.trim() || "No narrower focus specified; select a small coherent set."),
      candidate_limit: 1, budget: { max_provider_invocations: 1, max_seconds: Math.min(240, selectedCapability.max_proposal_seconds ?? 60) },
    });
    props.onStageChange(generating ? "candidates" : "evidence");
  }
  async function adopt(draft: AcceptedMaterialDraft) {
    if (!currentMaterial || !extractCurrent) throw new Error("请先登记当前提案的材料。");
    await send("adopt_material", `/research-v2/materials/${currentMaterial.id}/adopt`, { expected_digest: currentMaterial.current_digest, edited_material: draft });
    props.onStageChange("candidates");
  }
  async function configureEngineering(unresolved_dispositions: EngineeringDisposition[]) {
    if (!currentMaterial || !extractCurrent) throw new Error("请先登记当前提案的材料。");
    await send("configure_engineering", `/research-v2/materials/${currentMaterial.id}/engineering`, { expected_digest: currentMaterial.current_digest, unresolved_dispositions });
    props.onStageChange("candidates");
  }
  async function createExperiment() {
    if (!generationCurrent || !configurationConfirmed || !currentGenerate?.accepted_response_digest || currentGenerate.accepted_response?.outcome !== "draft" || reviewedCandidate !== currentGenerate.accepted_response_digest) throw new Error("请核对当前来源、配置与候选后再交接。");
    let session = await api<PublicResearchSession>(`/research-v2/sessions/${flow.generation!.sessionId}`);
    const token = generationReconciliationToken(session, currentGenerate.id);
    if (token) {
      await send("reconcile_session", `/research-v2/sessions/${session.id}/reconcile`, { token });
      session = await api<PublicResearchSession>(`/research-v2/sessions/${session.id}`);
      if (generationReconciliationToken(session, currentGenerate.id)) throw new Error("候选执行结果仍未决；已保留原状态，未交接实验。");
    }
    const created = await send("create_experiment", "/research-v2/experiments", { session_id: session.id, expected_session_digest: session.state_digest, proposal_id: currentGenerate.id, accepted_response_digest: currentGenerate.accepted_response_digest, budget: { max_steps: 8, max_failures: 2, max_seconds: 600 }, note: engineeringOnly ? "Formula engineering acceptance; actor=automation; human_approval=false. Economic mechanism and research validity remain unresolved." : "Explicit handoff from the material-to-alpha workbench. Semantic fidelity remains unverified." });
    await submitExistingExperiment(created as PublicExperiment);
  }
  async function submitExistingExperiment(initial: PublicExperiment) {
    let current = initial;
    for (let step = 0; step < 3; step++) {
      const action = nextExperimentAction(current);
      if (!action || !["validate", "commit", "submit"].includes(action)) break;
      const previousState = current.job?.state;
      current = await send("advance_experiment", `/research-v2/experiments/${current.id}/advance`, { action }) as PublicExperiment;
      if (current.job?.state === previousState) break;
    }
    if (mounted.current) setExperiment(current);
  }
  async function cancelSession(id: string) {
    const session = await api<PublicResearchSession>(`/research-v2/sessions/${id}`);
    await send("cancel_session", `/research-v2/sessions/${id}/cancel`, { expected_digest: session.state_digest });
  }
  const extractionOptions = capabilities.filter(item => item.operation === "extract_material");
  const readonlyProposal = (proposal: ResearchV2ProposalDetail | null) => {
    if (!proposal) return null;
    const kind = proposalExecutionKind(proposal);
    return <div className="info-note"><p>{t("状态", "Status")}: {proposal.status} · {t("保存的 Provider 执行次数", "Recorded provider invocations")}: {proposal.provider_invocations} · {kind === "fixture" ? t("固定演示输出", "Fixture demonstration") : kind === "native_provider_invocation" ? t("已保存 Native 调用收据", "Native invocation receipt saved") : t("Native 执行尚无可观察收据", "Native execution receipt not observed")}</p>{kind !== "fixture" && <p>{t("进程调用记录与真实模型验收分开；需结合官方传输身份和验收证据。模型输出仍是草案，人工质量待审。", "Invocation records and real-model acceptance are separate; consult official transport identity and acceptance evidence. Model outputs remain drafts pending human quality review.")}</p>}{proposal.error && <p role="alert">{proposal.error.code}: {proposal.error.message}</p>}<CreationProposalDiagnostics proposal={proposal} /><JsonDetails label={t("提案、回执与失败记录", "Proposal, receipts and failures")} value={proposal} /></div>;
  };
  const canGenerate = generationCurrent && currentGenerate?.status === "ready" && currentGenerate.accepted_response?.outcome === "draft";
  return <section aria-label={t("AI 辅助研究创建", "AI-assisted study creation")}>
    <p className="info-note">{t("AI 输出是待核对的研究提案。查看或刷新不调用模型；抽取、采纳、生成和实验交接分别由你明确触发。", "AI outputs are proposals for review. Viewing and refreshing never call the model. Extraction, adoption, generation and experiment handoff require explicit actions.")}</p>
    <MutationRecovery name="AI 草案请求" pending={pending} error={error} busy={busy} onRetry={() => void perform(async () => { await send(); })} onDismiss={() => { try { if (pending?.state === "rejected") { acknowledgeMutation(localStorage, mutationKey, pending.path, pending); setPending(null); setError(""); } } catch (cause) { setError(describeError(cause)); } }} />
    <button type="button" className="button small" disabled={busy} onClick={() => { setError(""); setNonce(value => value + 1); }}>{t("刷新已有状态", "Refresh existing state")}</button>
    {(stage === "materials" || stage === "evidence") && <>
      <fieldset disabled={loading}>
        <Field label={t("论文研究 Provider", "Paper research provider")}><select value={creationInputKey([flow.providerConfigId, flow.hostGrantId])} onChange={event => { const [providerConfigId, hostGrantId] = JSON.parse(event.target.value); try { save({ ...flow, providerConfigId, hostGrantId }); setPreview(null); } catch (cause) { setError(describeError(cause)); } }}><option value={creationInputKey(["", null])}>{t("选择 Provider", "Select provider")}</option>{extractionOptions.map(item => <option key={creationInputKey([item.provider_config_id, item.host_grant_id])} value={creationInputKey([item.provider_config_id, item.host_grant_id])}>{item.provider_config_id} · {item.provider_mode === "fixture" ? t("演示", "Demo") : item.authorization_kind === "bounded_qualification" ? t("受限 Native 验收", "Bounded Native qualification") : item.authorization_kind === "qualified" ? t("Native 已有宿主资格证据", "Native with host qualification evidence") : t("Native 尚未授权", "Native not authorized")} · {item.available ? t("可用", "Available") : item.reason_code}</option>)}</select></Field>
        {selectedCapability && <p>{selectedCapability.message} {t("剩余授权调用", "Remaining authorized dispatches")}: {selectedCapability.remaining_dispatches ?? t("未提供", "Not provided")}</p>}
        {!extractionOptions.length && <p role="status">{t("尚无可读取的研究 Provider 能力；可继续手动研究。", "No research provider capability is available; manual research remains available.")}</p>}
        <Field label={t("本次研究关注点（可选）", "Research focus (optional)")}><textarea maxLength={2000} value={flow.researchFocus ?? ""} placeholder={t("例如：仅研究 Alpha101 的原始公式与适用条件", "For example: the original Alpha101 formula and conditions")} onChange={event => { try { save({ ...flow, researchFocus: event.target.value }); setPreview(null); } catch (cause) { setError(describeError(cause)); } }} /></Field>
        <Field label={t("选定物理页（最多20页）", "Physical pages (maximum 20)")}><div>{paper?.pages?.map(item => <label key={item.page} style={{ display: "inline-block", marginRight: "1rem" }}><input type="checkbox" checked={flow.selectedPages.includes(item.page)} disabled={!flow.selectedPages.includes(item.page) && flow.selectedPages.length >= 20} onChange={event => { try { save({ ...flow, selectedPages: (event.target.checked ? [...flow.selectedPages, item.page] : flow.selectedPages.filter(page => page !== item.page)).sort((a, b) => a - b) }); setPreview(null); } catch (cause) { setError(describeError(cause)); } }} />{item.page}</label>)}</div></Field>
        {selectedCapability?.scope?.paper_selections.filter(selection => selection.paper_id === paper?.id).map(selection => <button className="button small" type="button" key={selection.selected_pages.join(",")} onClick={() => { try { save({ ...flow, selectedPages: selection.selected_pages }); setPreview(null); } catch (cause) { setError(describeError(cause)); } }}>{t(`采用授权页范围 ${selection.selected_pages.join(", ")}`, `Use authorized pages ${selection.selected_pages.join(", ")}`)}</button>)}
        {!!flow.selectedPages.length && <details><summary>{t("查看选页提取文本", "Inspect selected extracted text")}</summary><p>{t("以下为物理页提取文本；PDF 公式排版仍应核对原件，系统使用规范化引用跨度。", "This is extracted text by physical page. Verify equation layout against the original PDF; citations use normalized spans.")}</p>{paper?.pages?.filter(item => flow.selectedPages.includes(item.page)).map(item => <article key={item.page}><h4>{t(`第 ${item.page} 页`, `Page ${item.page}`)}</h4><pre style={{ whiteSpace: "pre-wrap" }}>{item.text}</pre></article>)}</details>}
        <div className="button-row"><button type="button" className="button" disabled={!selectedCapability?.available || !flow.selectedPages.length} onClick={() => void perform(() => previewSource(false))}>{t("预览抽取来源", "Preview extraction source")}</button><button type="button" className="button primary" disabled={!selectedCapability?.available || preview?.key !== extractKey || (extractCurrent && !!flow.extraction?.proposalId)} onClick={() => void perform(() => start(false))}>{t("开始一次材料抽取", "Extract material once")}</button></div>
      </fieldset>
      {flow.extraction && !extractCurrent && <p role="alert">{t("原抽取来源与当前选择不一致，旧结果保留但不能采纳。请明确预览新来源。", "The original extraction source differs from the selection. Old results remain available but cannot be adopted; explicitly preview the new source.")}</p>}
      {readonlyProposal(currentExtract)}
      {activeProposal(currentExtract) && flow.extraction && <button type="button" className="button" disabled={loading} onClick={() => void perform(() => cancelSession(flow.extraction!.sessionId))}>{t("取消当前抽取", "Cancel extraction")}</button>}
      {extractCurrent && currentExtract?.status === "ready" && currentExtract.accepted_response?.outcome === "material" && !flow.materialId && <button type="button" className="button primary" disabled={loading} onClick={() => void perform(async () => { await send("import_material", `/research-v2/proposals/${currentExtract.id}/material`, {}); })}>{t("打开材料提案进行核对", "Open material for review")}</button>}
      {extractCurrent && currentMaterial && <MaterialAdoptionPanel workspaceId={workspaceId} key={`${currentMaterial.id}:${currentMaterial.current_digest}`} material={currentMaterial} datasetId={dataset?.id} busy={loading} onAdopt={draft => perform(() => adopt(draft))} onEngineering={items => perform(() => configureEngineering(items))} />}
      {currentVersion && <p>{t("已保存材料版本", "Saved material version")}: {shortId(currentVersion.id)} · {t("语义仍待评定", "Semantic fidelity remains unverified")}</p>}
    </>}
    {(stage === "candidates" || stage === "experiment") && <>
      {engineeringOnly && <p role="status">{t("当前范围：仅公式工程验证。源论文科学问题仍未决，人工质量状态 pending_human；保存配置与核对候选不形成研究认可。", "Current scope: formula engineering verification only. Source science remains unresolved and human quality status is pending_human; saving settings or checking candidates supplies no research approval.")}</p>}
      {!flow.materialVersionId && <p role="status">{t("先在证据阶段核对并采纳材料，再生成候选。", "Review and adopt material before generating candidates.")}</p>}
      {selectedCapability?.scope?.generation_policy && <details><summary>{t("本次宿主授权的生成配置", "Host-authorized generation configuration")}</summary><JsonDetails value={selectedCapability.scope.generation_policy} label={t("固定输入范围", "Fixed input scope")} />{props.onApplyAuthorizedConfiguration && <button type="button" className="button" disabled={loading} onClick={() => props.onApplyAuthorizedConfiguration?.(selectedCapability.scope!.generation_policy!)}>{t("应用授权配置并重新确认", "Apply authorized settings and reconfirm")}</button>}</details>}
      <p>{t("候选生成前需确认数据、切分与方法；修改后旧候选来源将失效。", "Confirm data, time windows and method before generation. Changes invalidate the previous candidate source.")}</p>
      <div className="button-row"><button type="button" className="button" disabled={loading || !configurationConfirmed || !flow.materialVersionId || !extractCurrent || !selectedCapability?.available} onClick={() => void perform(() => previewSource(true))}>{t("预览候选生成来源", "Preview candidate source")}</button><button type="button" className="button primary" disabled={loading || !configurationConfirmed || preview?.key !== generateKey || !selectedCapability?.available || (generationCurrent && !!flow.generation?.proposalId)} onClick={() => void perform(() => start(true))}>{t("生成一个候选", "Generate one candidate")}</button></div>
      {selectedCapability && !selectedCapability.available && <p role="alert">{selectedCapability.reason_code}: {selectedCapability.message}</p>}
      <GenerationSourceNotice hasGeneration={!!flow.generation} configurationConfirmed={configurationConfirmed} generationCurrent={generationCurrent} />
      {readonlyProposal(currentGenerate)}
      {activeProposal(currentGenerate) && flow.generation && <button type="button" className="button" disabled={loading} onClick={() => void perform(() => cancelSession(flow.generation!.sessionId))}>{t("取消当前候选生成", "Cancel candidate generation")}</button>}
      {currentGenerate?.accepted_response?.draft?.candidates.map(candidate => <article key={candidate.id}><h4>{candidate.id}</h4><pre>{candidate.expression}</pre><p>{candidate.origin} · {t("假说", "Hypothesis")}: {candidate.hypothesis_id}</p><p>{candidate.changes.join("; ")}</p></article>)}
      {canGenerate && <label><input type="checkbox" disabled={loading} checked={reviewedCandidate === currentGenerate?.accepted_response_digest} onChange={event => setReviewedCandidate(event.target.checked ? currentGenerate!.accepted_response_digest! : "")} />{engineeringOnly ? t("已核对候选与冻结工程配置（不构成人工研究质量认可）", "Candidate and frozen engineering settings checked (no human research-quality approval)") : t("已核对当前候选、来源材料与冻结配置", "I checked this candidate, its material source and frozen settings")}</label>}
      {canGenerate && stage === "candidates" && <button type="button" className="button" disabled={loading || reviewedCandidate !== currentGenerate?.accepted_response_digest} onClick={() => props.onStageChange("experiment")}>{t("进入实验交接", "Continue to experiment handoff")}</button>}
      {stage === "experiment" && !flow.experimentId && <button type="button" className="button primary" disabled={loading || !canGenerate || !configurationConfirmed || reviewedCandidate !== currentGenerate?.accepted_response_digest} onClick={() => void perform(createExperiment)}>{t("保存此 AI 研究并提交实验", "Save this AI study and submit the experiment")}</button>}
      {currentExperiment && <section aria-label={t("AI 实验交接记录", "AI experiment handoff")}><p>{t("交接已保存；各执行步骤使用原实验身份，刷新不会提交新运行。", "Handoff is saved. Each execution step keeps this experiment identity; refreshing never submits a new run.")}</p><p>{currentExperiment.id} · {currentExperiment.job?.state}</p>{handoffStatus?.kind === "manual" && currentExperiment.job && ["created", "validated", "committed"].includes(currentExperiment.job.state) && <button type="button" className="button primary" disabled={loading} onClick={() => void perform(async () => { const current = await api<PublicExperiment>(`/research-v2/experiments/${currentExperiment.id}`); await submitExistingExperiment(current); })}>{t("继续提交原实验", "Continue submitting the original experiment")}</button>}{handoffStatus?.kind === "background" && <p role="status">{t("计算或后台收尾中；关闭本页面后，后台仍在原预算内继续。刷新仅读取已有实验。", "Calculation or finalization is in progress. The backend continues within the original budget after this page closes. Refresh only reads the existing experiment.")}</p>}{handoffStatus?.kind === "completed" && <p role="status">{t("实验执行与后台收尾均已完成，结果可进入审核。研究语义仍待评定。", "Experiment execution and finalization are complete. Results are ready for review; semantic fidelity remains unverified.")}</p>}{handoffStatus?.kind === "stopped" && <p role="alert">{t("原实验未完整完成：会话或执行已停止或失败。保留实际结果与停止原因，未自动重新提交。", "The original experiment did not fully complete: its session or execution stopped or failed. Its result and stop reason remain recorded; it was not automatically resubmitted.")}</p>}{handoffStatus?.kind === "unknown" && <p role="alert">{t("原实验结果仍未决，不能视为完整完成；保留原记录，不自动重建运行。", "The original experiment outcome is uncertain and cannot be treated as fully complete. Its record remains; no new run is created automatically.")}</p>}{handoffStatus && <p>{t("后台结算", "Finalization")}: {currentExperiment.reservation.status}{handoffStatus.reasons.length ? ` · ${handoffStatus.reasons.join(" · ")}` : ""}</p>}<button type="button" className="button" disabled={busy} onClick={() => { setError(""); setNonce(value => value + 1); }}>{t("读取已有实验状态", "Read existing experiment state")}</button>{currentExperiment.job?.run_id && currentExperiment.bootstrap && <button type="button" className="button" disabled={loading} onClick={() => props.onOpenExperiment({ researchId: currentExperiment.bootstrap!.research_id, revisionId: currentExperiment.job!.revision_id || currentExperiment.bootstrap!.revision_id, runId: currentExperiment.job!.run_id!, jobId: currentExperiment.job!.id, experimentId: currentExperiment.id })}>{t("打开实际实验与审核", "Open actual experiment and review")}</button>}<JsonDetails label={t("实验、研究和运行身份", "Experiment, study and run identities")} value={currentExperiment} /></section>}
    </>}
    {preview?.value.paper_selection && <details><summary>{t("实际冻结并发送的规范化片段", "Actual frozen normalized source chunks")}</summary><p>{t("来源摘要", "Source digest")}: {preview.value.paper_selection.selection_digest}</p>{preview.value.public_projection_redacted && <p>{t("公开展示已脱敏，请同时核对原 PDF；摘要仍绑定原始输入。", "The public display is redacted. Check the original PDF; the digest still binds the original input.")}</p>}{preview.value.paper_selection.chunks.map(chunk => <article key={chunk.id}><h4>{t(`物理第 ${chunk.page} 页`, `Physical page ${chunk.page}`)} · {chunk.id}</h4><pre style={{ whiteSpace: "pre-wrap" }}>{chunk.text}</pre></article>)}</details>}
    {preview && <p className="fine-print">{t("来源预览摘要", "Source preview digest")}: {shortId(preview.value.source.source_digest)} · {t("预览未调用模型", "Preview made no model call")}</p>}
  </section>;
}
