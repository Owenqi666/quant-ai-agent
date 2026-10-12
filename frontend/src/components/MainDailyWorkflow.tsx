import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import { describeError } from '../domain';
import type { Research, Run } from '../domain';
import type { MainDailySourceHandle, MainDailySourceHandlePage, MainDailyAssessmentSummary,
  MainDailyApprovalSummary, MainDailyReservationSummary, MainDailyNormalizationSummary,
  MainDailyRegistrationSummary, MainDailyMainCapabilities } from '../generated/api-contract';
import { useI18n } from '../i18n';
import { readMainDailyCheckpoint, saveMainDailyCheckpoint, mainDailyStage, mainDailyResearchRequest,
  canStartNewMainDailyTask, readMainDailyHistory, startNewMainDailyTask } from '../mainDailyWorkflow';
import type { MainDailyCheckpoint } from '../mainDailyWorkflow';
import { recoveryKey, readMutation } from '../reviewRecovery';
import { useRecoverableMutation } from '../useRecoverableMutation';
import type { WorkspaceRequestScope } from '../workspaceScope';
import { JsonDetails } from './ui';

type Target = { researchId: string; revisionId: string; runId?: string };
type Props = { workspaceScope: WorkspaceRequestScope; researches: Research[];
  onOpenResearch: (target: Target) => void; onOpenRun: (runId: string) => void };
const PATH = '/real-daily/v1';

export default function MainDailyWorkflow({ workspaceScope, researches, onOpenResearch, onOpenRun }: Props) {
  const { t } = useI18n();
  const workspaceId = workspaceScope.workspaceId;
  const [checkpoint, setCheckpoint] = useState<MainDailyCheckpoint>({ version: 1, workspaceId });
  const [history, setHistory] = useState<MainDailyCheckpoint[]>([]);
  const [originalRun, setOriginalRun] = useState<Run | null>(null);
  const checkpointRef = useRef(checkpoint); checkpointRef.current = checkpoint;
  const [ready, setReady] = useState(false), [error, setError] = useState(''), [busy, setBusy] = useState('');
  const [capabilities, setCapabilities] = useState<MainDailyMainCapabilities | null>(null);
  const [sources, setSources] = useState<MainDailySourceHandle[]>([]);
  const [assessment, setAssessment] = useState<MainDailyAssessmentSummary | null>(null);
  const [approval, setApproval] = useState<MainDailyApprovalSummary | null>(null);
  const [normalization, setNormalization] = useState<MainDailyNormalizationSummary | null>(null);
  const [normalizationMissing, setNormalizationMissing] = useState(false);
  const [registration, setRegistration] = useState<MainDailyRegistrationSummary | null>(null);
  const [templateId, setTemplateId] = useState(''), [templateRevisionId, setTemplateRevisionId] = useState('');
  const [template, setTemplate] = useState<Research | null>(null);
  const [title, setTitle] = useState('Alpha101 #101 OHLC development');
  const [reviewer, setReviewer] = useState(''), [consent, setConsent] = useState(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const key = (operation: string) => recoveryKey(workspaceId, 'main-daily', operation);
  const assessmentMutation = useRecoverableMutation<MainDailyAssessmentSummary>(key('assessment'), PATH + '/assessments');
  const approvalPath = `${PATH}/assessments/${encodeURIComponent(checkpoint.assessmentId || 'missing')}/approve`;
  const approvalMutation = useRecoverableMutation<MainDailyApprovalSummary>(key('approval'), approvalPath);
  const reservePath = `${PATH}/approvals/${encodeURIComponent(checkpoint.approvalId || 'missing')}/reserve`;
  const reserveMutation = useRecoverableMutation<MainDailyReservationSummary>(key('reserve'), reservePath);
  const normalizationMutation = useRecoverableMutation<MainDailyNormalizationSummary>(key('normalize'), PATH + '/normalizations');
  const researchMutation = useRecoverableMutation<Research>(key('research'), '/researches');
  const runMutation = useRecoverableMutation<Run>(key('run'), '/runs');
  const mutations = [assessmentMutation, approvalMutation, reserveMutation, normalizationMutation, researchMutation, runMutation];
  const pending = mutations.some(item => item.pending !== null);
  const blocked = mutations.some(item => item.blocked);
  const mutationKeys = ['assessment', 'approval', 'reserve', 'normalize', 'research', 'run'].map(key);
  const stage = mainDailyStage(checkpoint);
  const handle = sources.find(item => item.id === checkpoint.handleId && item.handle_digest === checkpoint.handleDigest);

  function save(changes: Partial<MainDailyCheckpoint>) {
    const next = { ...checkpointRef.current, ...changes };
    saveMainDailyCheckpoint(localStorage, next);
    checkpointRef.current = next;
    if (active.current) setCheckpoint(next);
    return next;
  }

  useEffect(() => {
    let current = true; const snapshot = workspaceScope.capture();
    setReady(false);
    try { const saved = readMainDailyCheckpoint(localStorage, workspaceId); checkpointRef.current = saved; setCheckpoint(saved);
      setHistory(readMainDailyHistory(localStorage, workspaceId)); }
    catch (cause) { setError(describeError(cause)); return; }
    void api<MainDailyMainCapabilities>(PATH + '/capabilities').then(async cap => {
      const page = cap.authority_initialized ? await api<MainDailySourceHandlePage>(PATH + '/sources') : {items: []};
      if (current && workspaceScope.isCurrent(snapshot)) { setCapabilities(cap); setSources(page.items); setReady(true); }
    })
      .catch(cause => { if (current && workspaceScope.isCurrent(snapshot)) setError(describeError(cause)); });
    return () => { current = false; };
  }, [workspaceId, workspaceScope]);

  useEffect(() => {
    if (!ready) return;
    let current = true; const snapshot = workspaceScope.capture();
    async function read() {
      if (checkpoint.assessmentId) {
        const value = await api<MainDailyAssessmentSummary>(`${PATH}/assessments/${encodeURIComponent(checkpoint.assessmentId)}`);
        if (value.assessment_digest !== checkpoint.assessmentDigest) throw new Error('已保存 assessment 身份不一致。');
        if (current && workspaceScope.isCurrent(snapshot)) setAssessment(value);
      }
      if (checkpoint.approvalId) {
        const value = await api<MainDailyApprovalSummary>(`${PATH}/approvals/${encodeURIComponent(checkpoint.approvalId)}?expected_digest=${checkpoint.approvalDigest}`);
        if (value.approval_digest !== checkpoint.approvalDigest) throw new Error('已保存 approval 身份不一致。');
        if (current && workspaceScope.isCurrent(snapshot)) setApproval(value);
      }
      if (checkpoint.intentId) {
        const value = await api<MainDailyNormalizationSummary>(`${PATH}/normalizations/${encodeURIComponent(checkpoint.intentId)}`);
        if (value.intent_digest !== checkpoint.intentDigest) throw new Error('已保存 normalization 身份不一致。');
        if (current && workspaceScope.isCurrent(snapshot)) setNormalization(value);
      }
      if (checkpoint.registrationId) {
        const value = await api<MainDailyRegistrationSummary>(`${PATH}/registrations/${encodeURIComponent(checkpoint.registrationId)}`);
        if (value.registration_digest !== checkpoint.registrationDigest) throw new Error('已保存 registration 身份不一致。');
        if (current && workspaceScope.isCurrent(snapshot)) setRegistration(value);
      }
      if (checkpoint.runId) {
        const value = await api<Run>(`/runs/${encodeURIComponent(checkpoint.runId)}`);
        if (value.revision_id !== checkpoint.revisionId || value.research_id !== checkpoint.researchId)
          throw new Error('原实验未绑定已保存研究身份。');
        if (current && workspaceScope.isCurrent(snapshot)) setOriginalRun(value);
      }
    }
    void read().catch(cause => { if (current && workspaceScope.isCurrent(snapshot)) setError(describeError(cause)); });
    return () => { current = false; };
  }, [checkpoint, ready, workspaceScope]);

  useEffect(() => {
    if (!templateId) { setTemplate(null); return; }
    let current = true; const snapshot = workspaceScope.capture();
    setTemplate(null); setTemplateRevisionId('');
    void api<Research>(`/researches/${encodeURIComponent(templateId)}`).then(value => {
      if (current && workspaceScope.isCurrent(snapshot)) { setTemplate(value); setTemplateRevisionId(value.latest_revision_id); }
    }).catch(cause => { if (current && workspaceScope.isCurrent(snapshot)) setError(describeError(cause)); });
    return () => { current = false; };
  }, [templateId, workspaceScope]);

  async function perform(name: string, operation: () => Promise<void>) {
    if (busy || !ready || blocked) return;
    const snapshot = workspaceScope.capture(); setBusy(name); setError('');
    try { await operation(); }
    catch (cause) { if (active.current && workspaceScope.isCurrent(snapshot)) setError(describeError(cause)); }
    finally { if (active.current && workspaceScope.isCurrent(snapshot)) setBusy(''); }
  }

  function savedNormalization(value: MainDailyNormalizationSummary, requestKey: string) {
    save({ normalizationKey: requestKey, intentId: value.id, intentDigest: value.intent_digest,
      ...(value.status === 'completed' && value.registration_id && value.registration_digest && value.dataset_id ? {
        registrationId: value.registration_id, registrationDigest: value.registration_digest, datasetId: value.dataset_id } : {}) });
    setNormalization(value);
  }

  async function recoverNormalization() {
    const requestKey = checkpoint.normalizationKey || normalizationMutation.pending?.body.idempotency_key;
    if (!requestKey) throw new Error('没有原请求 key；未生成新的规范化请求。');
    setNormalizationMissing(false);
    let value: MainDailyNormalizationSummary;
    try { value = await api<MainDailyNormalizationSummary>(`${PATH}/normalizations/by-key/${encodeURIComponent(requestKey)}`); }
    catch (cause) {
      if (cause instanceof ApiError && cause.status === 404 && !cause.contractViolation && normalizationMutation.pending && !checkpoint.intentId)
        setNormalizationMissing(true);
      throw cause;
    }
    savedNormalization(value, requestKey);
    // A durable server intent confirms receipt. Its unknown/failed outcome
    // remains terminal for this UI; acknowledging cannot authorize a new owner.
    if (normalizationMutation.pending) normalizationMutation.acknowledge();
  }

  async function submitNormalization(replay = false) {
    const value = await normalizationMutation.execute(replay ? undefined : {handle_id: checkpoint.handleId,
      expected_handle_digest: checkpoint.handleDigest, approval_id: checkpoint.approvalId,
      expected_approval_digest: checkpoint.approvalDigest, reservation_id: checkpoint.reservationId,
      expected_reservation_digest: checkpoint.reservationDigest});
    const original = readMutation(localStorage, key('normalize'), PATH + '/normalizations');
    if (!original) throw new Error('原规范化请求未保留；请检查记录。');
    savedNormalization(value, original.body.idempotency_key); normalizationMutation.acknowledge(); setNormalizationMissing(false);
  }

  const disabled = !!busy || !ready || blocked;
  const currentAllowed = approval?.current_permission === 'active' && approval.local_development_execution_allowed;
  const mayStartNew = canStartNewMainDailyTask(checkpoint, {ready, busy: !!busy, blocked, pending,
    mutationsReady: mutations.every(item => item.ready),
    assessmentStatus: assessment && assessment.id === checkpoint.assessmentId && assessment.assessment_digest === checkpoint.assessmentDigest ? assessment.status : undefined,
    runStatus: originalRun && originalRun.id === checkpoint.runId && originalRun.revision_id === checkpoint.revisionId &&
      originalRun.research_id === checkpoint.researchId ? originalRun.status : undefined});
  function beginNewSource() {
    if (!mayStartNew) return;
    try {
      const saved = startNewMainDailyTask(localStorage, checkpointRef.current, mutationKeys);
      checkpointRef.current = saved.checkpoint; setCheckpoint(saved.checkpoint); setHistory(saved.history);
      setAssessment(null); setApproval(null); setNormalization(null); setRegistration(null); setOriginalRun(null);
      setNormalizationMissing(false); setTemplate(null); setTemplateId(''); setTemplateRevisionId('');
      setReviewer(''); setConsent(false); setError('');
    } catch (cause) { setError(describeError(cause)); }
  }
  const selectedTemplate = template?.revisions?.find(item => item.id === templateRevisionId);
  let templateIssue = '';
  if (template && registration) { try { mainDailyResearchRequest(template, templateRevisionId, registration, title); } catch (cause) { templateIssue = describeError(cause); } }

  return <section className="panel" aria-label={t('四字段日频研究流程', 'Four-field daily research workflow')}>
    <h2>{t('四字段日频研究', 'Four-field daily research')}</h2>
    <p>{t('来源 → 准入 → 开发期前缀 → 证据与候选 → 实验审核。来源声明不代表认证；只使用 open、high、low、close，最终测试值与 volume 不解码。', 'Source → admission → development prefix → evidence and candidate → experiment review. Declarations do not authenticate sources. Only open, high, low and close are decoded; final test values and volume remain untouched.')}</p>
    <p>{t(`当前步骤：${stage}/5`, `Current step: ${stage}/5`)}</p>
    {busy && <p role="status">{t('正在完成当前步骤，请等待原请求确认…', 'Completing this step; waiting for confirmation of the original request…')}</p>}
    {error && <p role="alert">{error}</p>}
    {mutations.map((item, index) => item.error ? <p role="alert" key={index}>{item.error}</p> : null)}
    {!ready && <p role="status">{t('正在恢复本工作区的元数据记录…', 'Restoring this workspace’s metadata records…')}</p>}
    {capabilities?.offline_bootstrap_required && <p role="alert">{t('此工作区尚未完成离线 authority 初始化。请使用受控工程演示或离线准入工具；网页不能初始化执行权限或上传任意原件。', 'Offline authority initialization is required. Use the controlled engineering demo or offline admission tool. The browser cannot bootstrap execution authority or upload arbitrary originals.')}</p>}
    {stage === 1 && <>
      <h3>{t('1 · 选择已登记来源', '1 · Select a registered source')}</h3>
      <label>{t('来源入口', 'Source handle')}<select value={checkpoint.handleId || ''} disabled={disabled || pending} onChange={event => {
        const selected = sources.find(item => item.id === event.target.value);
        if (selected) { try { save({handleId: selected.id, handleDigest: selected.handle_digest}); } catch (cause) { setError(describeError(cause)); } }
      }}><option value="">{t('请选择', 'Choose a source')}</option>{sources.map(item => <option key={item.id} value={item.id}>{item.origin === 'engineering_fixture' ? t('合成工程夹具', 'Synthetic engineering fixture') : t('未认证真实来源声明', 'Unauthenticated real-source declaration')} · {item.id}</option>)}</select></label>
      {!sources.length && ready && <p>{t('没有受控来源入口。先完成离线 metadata capture，再刷新此页面；这里不接收 raw path。', 'No controlled source handles exist. Complete offline metadata capture, then reload this page. Raw paths are not accepted here.')}</p>}
      {handle && <JsonDetails value={handle} label={t('核对声明的窗口、字段与来源状态', 'Inspect declared windows, fields and source status')} />}
      <button className="button primary" disabled={disabled || !handle || !assessmentMutation.ready || assessmentMutation.pending?.state === 'rejected'} onClick={() => void perform('assessment', async () => {
        const value = await assessmentMutation.execute(assessmentMutation.pending ? undefined : {
          handle_id: handle!.id, expected_handle_digest: handle!.handle_digest });
        save({assessmentId: value.id, assessmentDigest: value.assessment_digest}); setAssessment(value); assessmentMutation.acknowledge();
      })}>{t('核对来源与事件声明', 'Assess source and event declarations')}</button>
    </>}
    {stage === 2 && <>
      <h3>{t('2 · 明确开发期执行批准', '2 · Explicitly approve development execution')}</h3>
      {assessment && <JsonDetails value={assessment} label={t('查看准入依据与阻断原因', 'Inspect admission basis and blockers')} />}
      {assessment?.status === 'blocked' ? <p role="alert">{t('来源或事件证据仍有缺口；用户批准和模型不能解除。补齐离线证据后按新来源版本重新准入。', 'Source or event evidence is incomplete. User approval and models cannot clear this blocker. Complete offline evidence and admit a new source version.')}</p> : <>
        <label>{t('审核者', 'Reviewer')}<input value={reviewer} disabled={disabled || !!approvalMutation.pending} onChange={event => setReviewer(event.target.value)} maxLength={200} /></label>
        <label><input type="checkbox" checked={consent} disabled={disabled || !!approvalMutation.pending} onChange={event => setConsent(event.target.checked)} />{t('我明确允许未来一小时内按原字节 SHA 校验整份合成/声明原件，并只解码开发期 OHLC。此批准不认证来源，也不允许最终测试数值。', 'I explicitly allow whole-byte SHA verification for the next hour and development-only OHLC decoding. This grant does not authenticate the source or permit final test values.')}</label>
        <button className="button primary" disabled={disabled || assessment?.status !== 'ready_for_host_review' || !approvalMutation.ready || approvalMutation.pending?.state === 'rejected' || (!approvalMutation.pending && (!consent || !reviewer.trim()))} onClick={() => void perform('approve', async () => {
          const value = await approvalMutation.execute(approvalMutation.pending ? undefined : {
            expected_digest: checkpoint.assessmentDigest, reviewer: reviewer.trim(), expires_at_utc: new Date(Date.now() + 3600000).toISOString(),
            whole_byte_integrity_allowed: true, numeric_access: 'development_ohlc_only', final_numeric_access_allowed: false });
          save({approvalId: value.id, approvalDigest: value.approval_digest}); setApproval(value); approvalMutation.acknowledge();
        })}>{approvalMutation.pending ? t('确认原批准请求', 'Confirm the original grant request') : t('批准一小时开发期执行', 'Approve development execution for one hour')}</button>
      </>}
    </>}
    {stage === 3 && <>
      <h3>{t('3 · 生成开发期前缀', '3 · Generate a development prefix')}</h3>
      {approval && <p>{t(`当前执行权限：${approval.current_permission}`, `Current execution permission: ${approval.current_permission}`)}</p>}
      {!currentAllowed && !normalization && <p role="alert">{t('当前批准不可执行；历史批准不会自动续权。', 'The current grant cannot execute. Historical grants do not renew automatically.')}</p>}
      {normalization && <JsonDetails value={normalization} label={t('查看规范化状态与实际读取计数', 'Inspect normalization status and actual read counters')} />}
      {normalization && normalization.status !== 'completed' && <p role="alert">{t(`原任务状态 ${normalization.status}。本入口保留记录并停止；不会更换 key 或重复读取原件。`, `Original task status: ${normalization.status}. Records are retained and this entry stops; it cannot change keys or reread originals.`)}</p>}
      {!checkpoint.reservationId ? <button className="button primary" disabled={disabled || !currentAllowed || !reserveMutation.ready || reserveMutation.pending?.state === 'rejected'} onClick={() => void perform('reserve', async () => {
        const value = await reserveMutation.execute(reserveMutation.pending ? undefined : { expected_digest: checkpoint.approvalDigest });
        save({reservationId: value.id, reservationDigest: value.reservation_digest}); reserveMutation.acknowledge();
      })}>{t('预留一次规范化任务', 'Reserve one normalization task')}</button> : normalizationMissing && normalizationMutation.pending && !normalization ? <button className="button primary" disabled={disabled || !currentAllowed || normalizationMutation.pending.state === 'rejected'} onClick={() => void perform('replay-normalize', () => submitNormalization(true))}>{t('安全重发原规范化请求', 'Safely resend the original normalization request')}</button> : normalization || normalizationMutation.pending ? <button className="button primary" disabled={disabled} onClick={() => void perform('recover', recoverNormalization)}>{t('读取原任务状态', 'Read the original task status')}</button> : <button className="button primary" disabled={disabled || !currentAllowed || !normalizationMutation.ready} onClick={() => void perform('normalize', () => submitNormalization())}>{t('生成 OHLC 开发期前缀', 'Generate the OHLC development prefix')}</button>}
    </>}
    {stage === 4 && <>
      <h3>{t('4 · 保存有出处的研究与候选', '4 · Save source-linked research and a candidate')}</h3>
      {registration && <JsonDetails value={registration} label={t('查看已注册的前缀与冻结窗口', 'Inspect the registered prefix and frozen windows')} />}
      <label>{t('复用已保存的 Alpha101 研究', 'Reuse a saved Alpha101 study')}<select value={templateId} disabled={disabled || !!researchMutation.pending} onChange={event => setTemplateId(event.target.value)}><option value="">{t('请选择已保存研究', 'Choose a saved study')}</option>{researches.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
      {template && <label>{t('明确选择原版本', 'Select the original revision')}<select value={templateRevisionId} disabled={disabled || !!researchMutation.pending} onChange={event => setTemplateRevisionId(event.target.value)}>{template.revisions?.map(item => <option key={item.id} value={item.id}>v{item.number} · {item.id}</option>)}</select></label>}
      <label>{t('研究标题', 'Research title')}<input value={title} maxLength={200} disabled={disabled || !!researchMutation.pending} onChange={event => setTitle(event.target.value)} /></label>
      {templateIssue && <p role="alert">{templateIssue}</p>}
      {selectedTemplate && <JsonDetails value={{evidence: selectedTemplate.task.evidence, hypotheses: selectedTemplate.task.hypotheses, candidates: selectedTemplate.task.candidates}} label={t('核对所选原版本的证据、假说与公式', 'Inspect selected source evidence, hypothesis and formula')} />}
      <p>{t('保留原文引用、假说和归属；只将 #101 的候选身份规范为 alpha101-101，并使用已注册窗口。保存研究不会提交实验。', 'Source quotes, hypotheses and attribution are retained. Only the #101 candidate identity is normalized to alpha101-101, with registered windows. Saving does not submit an experiment.')}</p>
      <button className="button primary" disabled={disabled || !researchMutation.ready || researchMutation.pending?.state === 'rejected' || (!researchMutation.pending && (!template || !registration || !!templateIssue))} onClick={() => void perform('research', async () => {
        const value = await researchMutation.execute(researchMutation.pending ? undefined : mainDailyResearchRequest(template!, templateRevisionId, registration!, title));
        save({researchId: value.id, revisionId: value.latest_revision_id}); researchMutation.acknowledge();
      })}>{researchMutation.pending ? t('确认原研究创建请求', 'Confirm the original creation request') : t('保存四字段研究版本', 'Save the four-field research revision')}</button>
    </>}
    {stage === 5 && <>
      <h3>{t('5 · 运行与审核', '5 · Run and review')}</h3>
      <p>{t(`冻结版本：${checkpoint.revisionId}。执行固定流程，最多一个候选、一次尝试；数值只来自开发期验证。`, `Frozen revision: ${checkpoint.revisionId}. The fixed workflow runs at most one candidate and one attempt; numerical results use development validation only.`)}</p>
      {checkpoint.runId ? <button className="button primary" disabled={!!busy} onClick={() => onOpenRun(checkpoint.runId!)}>{t('打开原实验与结果审核', 'Open the original experiment and review')}</button> : <button className="button primary" disabled={disabled || !runMutation.ready || runMutation.pending?.state === 'rejected' || (!runMutation.pending && !currentAllowed)} onClick={() => void perform('run', async () => {
        const value = await runMutation.execute(runMutation.pending ? undefined : { revision_id: checkpoint.revisionId, mode: 'fixed' });
        if (value.revision_id !== checkpoint.revisionId || value.research_id !== checkpoint.researchId) throw new Error('返回的实验未绑定原研究版本。');
        save({runId: value.id}); runMutation.acknowledge();
      })}>{runMutation.pending ? t('确认原实验提交请求', 'Confirm the original submission request') : t('提交固定流程实验', 'Submit the fixed experiment')}</button>}
      <button className="button" disabled={!!busy} onClick={() => onOpenResearch({researchId: checkpoint.researchId!, revisionId: checkpoint.revisionId!, ...(checkpoint.runId ? {runId: checkpoint.runId} : {})})}>{t('查看已保存研究版本', 'Inspect the saved research revision')}</button>
    </>}
    {mayStartNew && <div>
      <button className="button" onClick={beginNewSource}>{t('新建来源任务', 'Start a new source task')}</button>
      <p>{t('保留原来源、批准、研究和实验身份，只清空此浏览器的当前工作位置；不会续权、重新规范化或重复运行。', 'Prior source, grant, research and experiment identities are retained. Only this browser’s active checkpoint is cleared; no renewal, normalization or repeated run is requested.')}</p>
    </div>}
    {!!history.length && <details>
      <summary>{t('之前的来源任务', 'Previous source tasks')}</summary>
      <ul>{history.map((item, index) => <li key={`${item.assessmentId || item.handleId}-${index}`}>
        <span>{t(`来源 ${item.handleId || '—'} · 准入 ${item.assessmentId || '—'}`, `Source ${item.handleId || '—'} · Assessment ${item.assessmentId || '—'}`)}</span>
        {item.runId && <button className="button" disabled={!!busy} onClick={() => onOpenRun(item.runId!)}>{t('打开原实验', 'Open original experiment')}</button>}
        {item.researchId && item.revisionId && <button className="button" disabled={!!busy} onClick={() => onOpenResearch({researchId: item.researchId!, revisionId: item.revisionId!, ...(item.runId ? {runId: item.runId} : {})})}>{t('查看原研究', 'Inspect original study')}</button>}
        <JsonDetails value={item} label={t('查看保留的任务身份', 'Inspect retained task identities')} />
      </li>)}</ul>
    </details>}
  </section>;
}
