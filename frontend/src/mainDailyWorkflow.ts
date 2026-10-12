import type { Research, Revision } from './domain';
import type { MainDailyRegistrationSummary } from './generated/api-contract';
import { writeDurably } from './reviewRecovery';

export interface MainDailyCheckpoint {
  version: 1; workspaceId: string;
  handleId?: string; handleDigest?: string; assessmentId?: string; assessmentDigest?: string;
  approvalId?: string; approvalDigest?: string; reservationId?: string; reservationDigest?: string;
  normalizationKey?: string; intentId?: string; intentDigest?: string;
  registrationId?: string; registrationDigest?: string; datasetId?: string;
  researchId?: string; revisionId?: string; runId?: string;
}
const keys = ['version', 'workspaceId', 'handleId', 'handleDigest', 'assessmentId', 'assessmentDigest',
  'approvalId', 'approvalDigest', 'reservationId', 'reservationDigest', 'normalizationKey', 'intentId', 'intentDigest',
  'registrationId', 'registrationDigest', 'datasetId', 'researchId', 'revisionId', 'runId'];
export const mainDailyCheckpointKey = (workspaceId: string) => `paper-alpha:main-daily:v1:${JSON.stringify(workspaceId)}`;
export const mainDailyHistoryKey = (workspaceId: string) => `paper-alpha:main-daily-history:v1:${JSON.stringify(workspaceId)}`;

export function readMainDailyCheckpoint(storage: Pick<Storage, 'getItem'>, workspaceId: string): MainDailyCheckpoint {
  const raw = storage.getItem(mainDailyCheckpointKey(workspaceId));
  if (raw === null) return { version: 1, workspaceId };
  if (raw.length > 8192) throw new Error('日频工作位置超过本机记录上限；请保留记录并检查。');
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('日频工作位置格式错误。');
  const item = value as Record<string, unknown>;
  if (item.version !== 1 || item.workspaceId !== workspaceId || Object.keys(item).some(key => !keys.includes(key)))
    throw new Error('日频工作位置不属于此工作区；没有恢复或提交任务。');
  for (const [key, text] of Object.entries(item)) {
    if (key === 'version') continue;
    if (typeof text !== 'string' || !text || text.length > 128 || /[\x00-\x1f\x7f]/.test(text))
      throw new Error('日频工作位置含无效身份。');
    if (key.endsWith('Id') && !/^[A-Za-z0-9_-]+$/.test(text)) throw new Error('日频工作位置含无效身份。');
    if (key.endsWith('Digest') && !/^[0-9a-f]{64}$/.test(text)) throw new Error('日频工作位置摘要无效。');
  }
  for (const [id, pin] of [['handleId', 'handleDigest'], ['assessmentId', 'assessmentDigest'],
    ['approvalId', 'approvalDigest'], ['reservationId', 'reservationDigest'], ['intentId', 'intentDigest'],
    ['registrationId', 'registrationDigest'], ['researchId', 'revisionId']]) {
    if (!!item[id] !== !!item[pin]) throw new Error('日频工作位置身份不完整。');
  }
  if (item.assessmentId && !item.handleId || item.approvalId && !item.assessmentId || item.reservationId && !item.approvalId ||
      item.intentId && (!item.reservationId || !item.normalizationKey) || item.registrationId && !item.datasetId ||
      item.researchId && !item.registrationId || item.runId && !item.revisionId)
    throw new Error('日频工作位置阶段关联不完整。');
  return item as unknown as MainDailyCheckpoint;
}

export function saveMainDailyCheckpoint(storage: Pick<Storage, 'getItem' | 'setItem'>, checkpoint: MainDailyCheckpoint): void {
  const raw = JSON.stringify(checkpoint);
  readMainDailyCheckpoint({ getItem: () => raw }, checkpoint.workspaceId);
  writeDurably(storage, mainDailyCheckpointKey(checkpoint.workspaceId), checkpoint);
}

export function mainDailyStage(checkpoint: MainDailyCheckpoint): 1 | 2 | 3 | 4 | 5 {
  return checkpoint.researchId ? 5 : checkpoint.registrationId ? 4 : checkpoint.approvalId ? 3 : checkpoint.assessmentId ? 2 : 1;
}

export function canStartNewMainDailyTask(checkpoint: MainDailyCheckpoint, options: {
  ready: boolean; busy: boolean; blocked: boolean; pending: boolean; mutationsReady: boolean;
  assessmentStatus?: string; runStatus?: string;
}) {
  if (!options.ready || options.busy || options.blocked || options.pending || !options.mutationsReady) return false;
  return mainDailyStage(checkpoint) === 2 && options.assessmentStatus === 'blocked' ||
    mainDailyStage(checkpoint) === 5 && !!checkpoint.runId &&
      ['completed', 'failed', 'cancelled', 'interrupted'].includes(options.runStatus || '');
}

export function readMainDailyHistory(storage: Pick<Storage, 'getItem'>, workspaceId: string): MainDailyCheckpoint[] {
  const raw = storage.getItem(mainDailyHistoryKey(workspaceId));
  if (raw === null) return [];
  if (raw.length > 65536) throw new Error('之前的日频任务记录超过本机上限；请保留记录并检查。');
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('之前的日频任务记录格式错误。');
  const item = value as Record<string, unknown>;
  if (item.version !== 1 || item.workspaceId !== workspaceId ||
    Object.keys(item).some(key => !['version', 'workspaceId', 'checkpoints'].includes(key)) ||
    !Array.isArray(item.checkpoints) || item.checkpoints.length > 50) throw new Error('之前的日频任务作用域或容量错误。');
  return item.checkpoints.map(checkpoint => readMainDailyCheckpoint({ getItem: () => JSON.stringify(checkpoint) }, workspaceId));
}

export function startNewMainDailyTask(storage: Pick<Storage, 'getItem' | 'setItem'>, checkpoint: MainDailyCheckpoint,
  mutationKeys: readonly string[]): { checkpoint: MainDailyCheckpoint; history: MainDailyCheckpoint[] } {
  const scope = checkpoint.workspaceId;
  const expected = JSON.stringify(checkpoint);
  function unchanged() {
    if (mutationKeys.some(key => storage.getItem(key) !== null)) throw new Error('仍有待确认请求；请先确认原请求，不会清除或替换 key。');
    if (JSON.stringify(readMainDailyCheckpoint(storage, scope)) !== expected) throw new Error('当前任务已改变，请重新加载；没有清除其他任务。');
  }
  unchanged();
  const history = readMainDailyHistory(storage, scope);
  if (!history.some(item => JSON.stringify(item) === expected)) history.push(JSON.parse(expected) as MainDailyCheckpoint);
  const archived = { version: 1, workspaceId: scope, checkpoints: history };
  readMainDailyHistory({ getItem: () => JSON.stringify(archived) }, scope);
  // Preserve every prior ID before clearing only this browser's active pointer.
  writeDurably(storage, mainDailyHistoryKey(scope), archived);
  unchanged();
  const next: MainDailyCheckpoint = { version: 1, workspaceId: scope };
  saveMainDailyCheckpoint(storage, next);
  return { checkpoint: next, history };
}

export function alpha101Candidate(revision: Revision) {
  const formulas = new Set(['((close-open)/((high-low)+.001))', '((close-open)/((high-low)+0.001))',
    '(close-open)/(high-low+.001)', '(close-open)/(high-low+0.001)']);
  const matches = revision.task.candidates.filter(item => ['alpha101', 'alpha101-101'].includes(item.id) &&
    formulas.has(item.expression.replace(/\s/g, '')));
  if (matches.length !== 1) throw new Error('请选择含原始 Alpha101 #101 四字段公式的已保存版本；不自动修改其他公式。');
  return matches[0];
}

export function mainDailyResearchRequest(research: Research, revisionId: string, registration: MainDailyRegistrationSummary, title: string) {
  const revision = research.revisions?.find(item => item.id === revisionId && item.research_id === research.id);
  if (!revision) throw new Error('没有取得明确选择的原研究版本；不会替换成最新版本。');
  const candidate = alpha101Candidate(revision);
  const hypothesis = revision.task.hypotheses.find(item => item.id === candidate.hypothesis_id);
  if (!hypothesis || !hypothesis.evidence_ids.length) throw new Error('候选缺少关联假说或原文证据。');
  const evidence = revision.task.evidence.filter(item => hypothesis.evidence_ids.includes(item.id));
  if (evidence.length !== hypothesis.evidence_ids.length) throw new Error('假说引用的原文证据未完整保存。');
  if (!title.trim() || title.trim().length > 200) throw new Error('请填写 1 到 200 字符的研究标题。');
  return JSON.parse(JSON.stringify({ title: title.trim(), paper_id: research.paper_id, dataset_id: registration.dataset_id,
    task: { schema_version: 1, evidence, hypotheses: [hypothesis], candidates: [{ ...candidate, id: 'alpha101-101' }],
      evaluation: registration.selection.config,
      budget: { max_candidates: 1, max_attempts_per_candidate: 1, max_tool_calls: 8, max_seconds: 30 } } })) as Record<string, unknown>;
}
