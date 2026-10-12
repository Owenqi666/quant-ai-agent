import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "../api";
import { describeError, formatDate } from "../domain";
import type { Research } from "../domain";
import type { ProposalContextResponse, ProposalDetail, ProposalPage, ProposalResearchReview, ProposalSummary, ProvidersResponse } from "../generated/api-contract";
import { buildProposalCreate, buildProposalHandoff, observedMetric, proposalCanCancel, proposalCanHandoff, proposalIsActive, proposalStatusLabel, reviewMatchesProposal } from "../providerWorkflow";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import type { WorkspaceRequestScope } from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import ProviderRuntimeStatusView from "./ProviderRuntimeStatusView";
import ResearchCorrectionLoop from "./ResearchCorrectionLoop";
import { Field, JsonDetails } from "./ui";
import { useI18n } from "../i18n";
export default function ProviderTasks({
  workspaceScope,
  catalog,
  researchId,
  research,
  onResearchChange,
  onRefreshResearch,
  onOpenJob,
  onOpenRun,
  onPreviousResearch,
  onNextResearch,
  canPreviousResearch,
  canNextResearch
}: {
  workspaceScope: WorkspaceRequestScope;
  catalog: {
    id: string;
    title: string;
  }[];
  researchId: string;
  research: Research | null;
  onResearchChange: (id: string) => void;
  onRefreshResearch: () => void;
  onOpenJob: (id: string) => void;
  onOpenRun?: (id: string) => void;
  onPreviousResearch: () => void;
  onNextResearch: () => void;
  canPreviousResearch: boolean;
  canNextResearch: boolean;
}) {
  const {
    t
  } = useI18n();
  const [providers, setProviders] = useState<ProvidersResponse | null>(null),
    [configId, setConfigId] = useState("");
  const [context, setContext] = useState<ProposalContextResponse | null>(null),
    [page, setPage] = useState<ProposalPage | null>(null);
  const [selected, setSelected] = useState(""),
    [offset, setOffset] = useState(0),
    [refresh, setRefresh] = useState(0);
  const [instruction, setInstruction] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const mounted = useRef(false),
    sending = useRef(false);
  const selectedResearch = useRef(researchId);
  selectedResearch.current = researchId;
  const observedStatuses = useRef(new Map<string, {
      status: ProposalSummary["status"];
      sequence: number;
    }>()),
    statusSequence = useRef(0);
  const onReadProposal = useCallback((value: ProposalDetail) => {
    observedStatuses.current.set(value.id, {
      status: value.status,
      sequence: ++statusSequence.current
    });
    setPage(old => old && old.items.some(item => item.id === value.id && item.status !== value.status) ? {
      ...old,
      items: old.items.map(item => item.id === value.id ? {
        ...item,
        status: value.status
      } : item)
    } : old);
  }, []);
  const mutation = useRecoverableMutation<ProposalSummary>(recoveryKey(workspaceScope.workspaceId, "proposal-create", researchId), "/proposals");
  const revisionId = research?.latest_revision_id;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const capture = workspaceScope.capture(),
      abort = new AbortController();
    setProviders(null);
    void api<ProvidersResponse>("/providers", {
      signal: abort.signal
    }).then(v => {
      if (workspaceScope.isCurrent(capture, abort.signal)) {
        setProviders(v);
        setConfigId(old => v.providers.some(c => c.config_id === old) ? old : v.providers.find(c => c.dispatchable)?.config_id || "");
      }
    }).catch(e => {
      if (workspaceScope.isCurrent(capture, abort.signal)) setError(describeError(e));
    });
    return () => abort.abort();
  }, [refresh, workspaceScope]);
  useEffect(() => {
    const capture = workspaceScope.capture(),
      abort = new AbortController();
    setContext(null);
    if (researchId && research?.id === researchId && revisionId) void api<ProposalContextResponse>(`/researches/${researchId}/proposal-context?base_revision_id=${revisionId}`, {
      signal: abort.signal
    }).then(v => {
      if (workspaceScope.isCurrent(capture, abort.signal)) setContext(v);
    }).catch(e => {
      if (workspaceScope.isCurrent(capture, abort.signal)) setError(describeError(e));
    });
    return () => abort.abort();
  }, [researchId, research?.id, revisionId, refresh, workspaceScope]);
  useEffect(() => {
    setSelected("");
    setOffset(0);
    setInstruction("");
    setError("");
  }, [researchId]);
  useEffect(() => {
    const capture = workspaceScope.capture(),
      abort = new AbortController(),
      startedSequence = statusSequence.current;
    setPage(null);
    const filter = researchId ? `&research_id=${researchId}` : "";
    void api<ProposalPage>(`/proposals?limit=10&offset=${offset}${filter}`, {
      signal: abort.signal
    }).then(v => {
      if (workspaceScope.isCurrent(capture, abort.signal)) {
        setPage({
          ...v,
          items: v.items.map(item => {
            const observed = observedStatuses.current.get(item.id);
            return observed && observed.sequence > startedSequence ? {
              ...item,
              status: observed.status
            } : item;
          })
        });
        setSelected(old => old || v.items[0]?.id || "");
      }
    }).catch(e => {
      if (workspaceScope.isCurrent(capture, abort.signal)) setError(describeError(e));
    });
    return () => abort.abort();
  }, [researchId, offset, refresh, workspaceScope]);
  async function submit(replay = false) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    const capture = workspaceScope.capture(),
      requestedResearch = researchId;
    try {
      const body = replay ? undefined : buildProposalCreate(researchId, revisionId || "", context!, providers!, configId, instruction);
      const v = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(capture) || selectedResearch.current !== requestedResearch) return;
      setSelected(v.id);
      setRefresh(n => n + 1);
      mutation.acknowledge();
    } catch (e) {
      if (mounted.current && workspaceScope.isCurrent(capture) && selectedResearch.current === requestedResearch) setError(describeError(e));
    } finally {
      sending.current = false;
      if (mounted.current && workspaceScope.isCurrent(capture)) setBusy(false);
    }
  }
  const config = providers?.providers.find(c => c.config_id === configId);
  const locked = busy || !mutation.ready || mutation.blocked || !!mutation.pending;
  return <>
    <section className="panel" aria-label={t("AI 草案生成入口", "AI draft generation")}><h2>{t("论文证据 → AI 草案 → 校验 → 显式交接实验", "Paper evidence → AI draft → Validation → Explicit experiment handoff")}</h2>
      <p>{t("选择已有研究及宿主启用的 Provider，每次最多生成一个候选。生成和机器校验通过仍需研究审核；交接只创建实验任务，不表示人工批准。", "Select an existing research and a host-enabled provider. Each request generates at most one candidate. Generation and machine checks still require research review; handoff creates an experiment task without human approval.")}</p>
      <p>{t("生成预算：登记一次 Provider 执行，最多 240 秒（含现场边界检查与模型执行）；交接实验预算：10 步、3 次技术失败、从创建起最多 300 秒。刷新不重置预算。", "Generation budget: one registered provider invocation, up to 240 seconds including boundary checks and model execution. Handoff budget: 10 steps, 3 technical failures, and 300 seconds from creation. Refreshing does not reset the budget.")}</p>
      {error && <p role="alert">{error}</p>}
      {providers && <p>{t("宿主真实派发许可：", "Host permission for real dispatch: ")}{providers.real_dispatch_enabled ? t("已启用", "Enabled") : t("未启用", "Disabled")}{t("；当前派发：", "; current dispatch: ")}{providers.dispatch_paused ? t("已暂停", "Paused") : t("未暂停", "Not paused")}{t("。许可不代表本次推理或边界已经验证。", " Permission does not prove that inference or its boundary has been verified.")}</p>}
      <ProviderRuntimeStatusView workspaceScope={workspaceScope} refresh={refresh} />
      {providers?.providers.filter(c => !c.dispatchable && c.error).map(c => <p key={c.config_id}>{c.config_id}：{c.error!.code} · {c.error!.message}</p>)}
      <MutationRecovery name={t("AI 草案请求", "AI draft request")} pending={mutation.pending} error={mutation.error} busy={busy} onRetry={() => void submit(true)} onDismiss={mutation.dismissRejected} />
      <fieldset disabled={locked}>
        <Field label={t("草案研究", "Draft research")}><select value={researchId} onChange={e => onResearchChange(e.target.value)}><option value="">{t("选择已有研究", "Select an existing research")}</option>{research && research.id === researchId && !catalog.some(r => r.id === researchId) && <option value={research.id}>{research.title}</option>}{catalog.map(r => <option key={r.id} value={r.id}>{r.title}</option>)}</select></Field>
        <div className="button-row"><button className="button small" disabled={!canPreviousResearch} onClick={onPreviousResearch}>{t("上一页研究", "Previous research page")}</button><button className="button small" disabled={!canNextResearch} onClick={onNextResearch}>{t("下一页研究", "Next research page")}</button></div>
        <Field label="Provider"><select value={configId} onChange={e => setConfigId(e.target.value)}><option value="">{t("选择宿主 Provider", "Select a host provider")}</option>{providers?.providers.map(c => <option key={c.config_id} value={c.config_id} disabled={!c.dispatchable}>{c.config_id} · {c.model || t("未配置模型", "Model not configured")} · {c.mode}{c.dispatchable ? "" : t("（不可派发）", " (not dispatchable)")}</option>)}</select></Field>
        {config?.error && <p role="status">{config.error.code}：{config.error.message}</p>}
        {!providers?.providers.length && <p>{t("尚无宿主配置。浏览器不能设置模型、执行器或启用真实调用。", "No host configuration is available. The browser cannot configure models, executors or enable real calls.")}</p>}
        <Field label={t("研究要求（可选）", "Research requirements (optional)")}><textarea maxLength={4000} value={instruction} onChange={e => setInstruction(e.target.value)} placeholder={t("只根据已有证据实现候选，明确区分原文结论与推测", "Implement candidates from existing evidence only; distinguish source findings from conjecture.")} /></Field>
        {context && <p>{t("使用冻结修订", "Using frozen revision ")}{context.context.base_revision_id}{t("；模型上下文仅包含证据、维护假说、字段算子与开发配置。完整本地因子计算可能读取测试字节，指标只评 validation。", "; model context contains only evidence, maintained hypotheses, field/operator definitions and development configuration. Full local factor computation may read test bytes; metrics use validation only.")}</p>}
        <button className="button primary" disabled={!context || !providers || !config?.dispatchable || providers.dispatch_paused} onClick={() => void submit()}>{t("提交一次草案生成", "Submit one draft generation")}</button>
      </fieldset>
      <div className="button-row"><button className="button" disabled={busy} onClick={() => {
          onRefreshResearch();
          setRefresh(n => n + 1);
        }}>{t("刷新研究、Provider 与草案任务", "Refresh research, providers and draft tasks")}</button></div>
      <details><summary>{t("证据与能力上下文", "Evidence and capability context")}</summary>{context && <JsonDetails value={context} />}</details>
      <h3>{t("草案任务", "Draft tasks")}</h3>
      <p>{page ? t(`本页 ${page.items.length} / 共 ${page.total} 项`, `This page: ${page.items.length} / ${page.total} tasks`) : t("正在读取任务", "Loading tasks")}</p>
      {page?.items.map(item => <p key={item.id}><button className="text-button" aria-label={t(`查看 AI 草案 ${item.id}`, `View AI draft ${item.id}`)} onClick={() => setSelected(item.id)}>{formatDate(item.created_at)} · {proposalStatusLabel(item.status)} · {item.requested_model || item.provider_config_id}</button></p>)}
      <div className="button-row"><button className="button small" disabled={offset === 0} onClick={() => setOffset(n => Math.max(0, n - 10))}>{t("上一页草案", "Previous draft page")}</button><button className="button small" disabled={!page || offset + 10 >= page.total} onClick={() => setOffset(n => n + 10)}>{t("下一页草案", "Next draft page")}</button></div>
    </section>
    <ResearchCorrectionLoop workspaceScope={workspaceScope} researchId={researchId}
      baseRevisionId={research?.id === researchId ? revisionId || "" : ""} providers={providers}
      providerConfigId={configId} onOpenJob={onOpenJob} onOpenRun={onOpenRun} />
    {selected && <ProposalView key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope} onOpenJob={onOpenJob} onReadProposal={onReadProposal} />} 
  </>;
}
function ProposalView({
  id,
  workspaceScope,
  onOpenJob,
  onReadProposal
}: {
  id: string;
  workspaceScope: WorkspaceRequestScope;
  onOpenJob: (id: string) => void;
  onReadProposal: (value: ProposalDetail) => void;
}) {
  const {
    t
  } = useI18n();
  const [proposal, setProposal] = useState<ProposalDetail | null>(null),
    [review, setReview] = useState<ProposalResearchReview | null>(null);
  const [error, setError] = useState(""),
    [reviewError, setReviewError] = useState(""),
    [busy, setBusy] = useState(false),
    [nonce, setNonce] = useState(0);
  const mounted = useRef(false),
    sending = useRef(false);
  const readEpoch = useRef(0);
  const mutation = useRecoverableMutation<ProposalDetail>(recoveryKey(workspaceScope.workspaceId, "proposal-handoff", id), `/proposals/${id}/handoff`);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const capture = workspaceScope.capture(),
      abort = new AbortController(),
      epoch = ++readEpoch.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      if (epoch !== readEpoch.current) return;
      try {
        const v = await api<ProposalDetail>(`/proposals/${id}`, {
          signal: abort.signal
        });
        if (!workspaceScope.isCurrent(capture, abort.signal) || epoch !== readEpoch.current) return;
        setProposal(v);
        onReadProposal(v);
        if (proposalIsActive(v.status)) timer = setTimeout(() => void poll(), 2000);
      } catch (e) {
        if (workspaceScope.isCurrent(capture, abort.signal) && epoch === readEpoch.current) {
          setProposal(null);
          setReview(null);
          setError(describeError(e));
        }
      }
    }
    void poll();
    return () => {
      abort.abort();
      if (timer) clearTimeout(timer);
    };
  }, [id, nonce, workspaceScope, onReadProposal]);
  useEffect(() => {
    if (!proposal) return;
    const capture = workspaceScope.capture(),
      abort = new AbortController(),
      epoch = readEpoch.current;
    setReview(null);
    setReviewError("");
    // Query the consolidated, verified output after a state transition or an
    // explicit refresh, rather than recomputing its oracle on every status poll.
    void api<ProposalResearchReview>(`/proposals/${id}/review`, {
      signal: abort.signal
    }).then(v => {
      if (workspaceScope.isCurrent(capture, abort.signal) && epoch === readEpoch.current) {
        if (!reviewMatchesProposal(v, proposal)) {
          setReviewError(t("审核材料与当前草案状态不一致，请刷新。", "Review material does not match the current draft state. Refresh to continue."));
          return;
        }
        setReview(v);
      }
    }).catch(e => {
      if (workspaceScope.isCurrent(capture, abort.signal) && epoch === readEpoch.current) setReviewError(describeError(e));
    });
    return () => abort.abort();
  }, [id, proposal?.status, proposal?.accepted_response_digest, proposal?.research_job_id, nonce, workspaceScope]);
  async function handoff(replay = false) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    const capture = workspaceScope.capture();
    readEpoch.current += 1;
    setReview(null);
    try {
      const v = await mutation.execute(replay ? undefined : buildProposalHandoff(proposal!));
      if (!mounted.current || !workspaceScope.isCurrent(capture)) return;
      setProposal(v);
      onReadProposal(v);
      mutation.acknowledge();
      if (v.research_job_id) onOpenJob(v.research_job_id);
    } catch (e) {
      if (mounted.current && workspaceScope.isCurrent(capture)) setError(describeError(e));
    } finally {
      sending.current = false;
      if (mounted.current && workspaceScope.isCurrent(capture)) {
        setBusy(false);
        setNonce(n => n + 1);
      }
    }
  }
  async function cancel() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    const capture = workspaceScope.capture();
    readEpoch.current += 1;
    setReview(null);
    try {
      const v = await post<ProposalDetail>(`/proposals/${id}/cancel`, {});
      if (mounted.current && workspaceScope.isCurrent(capture)) {
        setProposal(v);
        onReadProposal(v);
      }
    } catch (e) {
      if (mounted.current && workspaceScope.isCurrent(capture)) setError(describeError(e));
    } finally {
      sending.current = false;
      if (mounted.current && workspaceScope.isCurrent(capture)) {
        setBusy(false);
        setNonce(n => n + 1);
      }
    }
  }
  function refreshView() {
    readEpoch.current += 1;
    setProposal(null);
    setReview(null);
    setError("");
    setReviewError("");
    setNonce(n => n + 1);
  }
  if (!proposal) return <section className="panel" role={error ? "alert" : "status"}>{error || t("正在读取草案状态", "Loading draft state")}{error && <button className="button" onClick={refreshView}>{t("重新读取草案状态", "Reload draft state")}</button>}</section>;
  const currentReview = review && reviewMatchesProposal(review, proposal) ? review : null;
  return <section className="panel" aria-label={t("AI 草案实际输出", "Actual AI draft output")}><h2>{proposalStatusLabel(proposal.status)}</h2>
    <p>{t("请求", "Request ")}{proposal.id}{t("；已登记执行", "; registered invocations: ")}{proposal.provider_invocations}{t("次；模型", "; model: ")}{proposal.requested_model || t("未知", "Unknown")}{t("。人工研究审核：待填。", " Human research review: unassessed.")}</p>
    {proposal.error && <p role="alert">{proposal.error.code}：{proposal.error.message}</p>}{error && <p role="alert">{error}</p>}
    <MutationRecovery name={t("实验交接", "Experiment handoff")} pending={mutation.pending} error={mutation.error} busy={busy} onRetry={() => void handoff(true)} onDismiss={mutation.dismissRejected} />
    <div className="button-row">
      <button className="button" disabled={busy} onClick={refreshView}>{t("重新核验草案与实验结果", "Recheck draft and experiment results")}</button>
      {proposalCanCancel(proposal.status) && <button className="button" disabled={busy} onClick={() => void cancel()}>{t("停止生成", "Stop generation")}</button>}
      {proposalCanHandoff(proposal) && <button className="button primary" disabled={busy || !currentReview || !!mutation.pending || mutation.blocked || !mutation.ready} onClick={() => void handoff()}>{t("将本次草案交接为实验任务", "Hand this draft to an experiment task")}</button>}
      {proposal.research_job_id && <button className="button" onClick={() => onOpenJob(proposal.research_job_id!)}>{t("打开下方已有实验执行", "Open the existing experiment execution below")}</button>}
    </div>
    <p>{t("交接后请在下方原实验任务中逐步校验、保存和运行。交接不代表论文忠实度获得人工批准。", "After handoff, validate, save and run the existing experiment task below. Handoff does not confer human approval of paper fidelity.")}</p>
    {reviewError && <p role="alert">{t("审核材料未能通过核验：", "Review material could not be verified: ")}{reviewError}</p>}
    {!currentReview && !reviewError && <p role="status">{t("正在读取本次审核材料", "Loading review material for this draft")}</p>}
    {currentReview && <>
      <a className="text-button" href={currentReview.paper.pdf_url} target="_blank" rel="noreferrer">{t("打开原论文 PDF：", "Open original paper PDF: ")}{currentReview.paper.title}</a>
      {currentReview.response?.model_output.abstention && <p role="status">{t("模型弃权：", "Model abstained: ")}{currentReview.response.model_output.abstention.reason_code} · {currentReview.response.model_output.abstention.reason}</p>}
      {!currentReview.candidates.length && <p>{t("当前没有候选实现；未补造表达式或指标。", "There is no candidate implementation. Expressions or metrics have not been invented.")}</p>}
      {currentReview.candidates.map(candidate => <article key={candidate.id}><h3>{t("候选", "Candidate ")}{candidate.id}</h3><pre>{candidate.expression}</pre>
        <p>{t("实现归因：", "Implementation attribution: ")}{candidate.origin}{t("；声明归因：", "; claim attribution: ")}{candidate.hypothesis?.attribution}{t("；机制归因：", "; mechanism attribution: ")}{candidate.hypothesis?.mechanism_attribution}。</p>
        <p>{candidate.hypothesis?.claim}</p><p>{candidate.hypothesis?.economic_mechanism}</p>
        <JsonDetails label={t("实际候选机器检查", "Actual candidate machine checks")} value={candidate.machine_checks} />
      </article>)}
      {(currentReview.output_evidence.length ? currentReview.output_evidence : currentReview.input_evidence).map(evidence => <blockquote key={evidence.id}>
        <p>{t("PDF 第", "PDF physical page ")}{evidence.page}{t("页 ·", " · ")}{evidence.id}{t("· 引文字面核对：", " · literal quotation check: ")}{evidence.citation_verified ? t("匹配", "Matched") : t("不匹配", "Not matched")}{t("（语义待审）", " (semantics unassessed)")}</p><p>{evidence.quote}</p>
      </blockquote>)}
      {currentReview.computation?.candidates.map(candidate => <article key={candidate.id}><h3>{candidate.id}{t("的实际计算结果", " actual computed result")}</h3>
        <p>{t("完整性：", "Integrity: ")}{currentReview.computation!.integrity}{t("；独立数值核对：", "; independent numerical check: ")}{candidate.independent_verification.outcome}{t("；评估区间：", "; evaluation split: ")}{candidate.result?.split || t("未评估", "Not evaluated")}。</p>
        <p>{candidate.result?.reason}</p>
        {candidate.result?.metrics ? <table><thead><tr><th>{t("实际指标", "Observed metric")}</th><th>{t("原值", "Literal value")}</th></tr></thead><tbody>{Object.entries(candidate.result.metrics).map(([name, value]) => <tr key={name}><td>{name}</td><td>{observedMetric(value)}</td></tr>)}</tbody></table> : <p>{t("没有可核对的指标，缺失值保留。", "No metrics are available for verification; missing values are retained.")}</p>}
        <JsonDetails label={t("独立校验及计算范围", "Independent verification and computation scope")} value={candidate.independent_verification} />
      </article>)}
      {!currentReview.computation?.candidates.length && <p>{currentReview.computation?.missing_reason || t("尚无实际计算结果，未设置默认零指标。", "There is no computed result; missing metrics have not been replaced by zero.")}</p>}
      <p>{t("synthetic 软件验证；收益为未扣成本的工具值，不代表市场表现。完整本地因子计算可能读取测试字节，模型上下文和指标范围另行记录。", "Synthetic software validation. Returns are tool outputs before costs, not market performance. Full local factor computation may read test bytes; model-context and metric scopes are recorded separately.")}</p>
      <JsonDetails label={t("完整公开审核材料及输入/结果身份", "Complete public review material and input/result identity")} value={currentReview} />
    </>}
    <JsonDetails label={t("调用回执、失败记录与原始产物入口", "Invocation receipts, failures and original artifact links")} value={proposal} />
  </section>;
}
