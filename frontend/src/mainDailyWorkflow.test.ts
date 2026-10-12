import { describe, expect, it } from 'vitest';
import { mainDailyCheckpointKey, readMainDailyCheckpoint, saveMainDailyCheckpoint, mainDailyStage, mainDailyResearchRequest,
  canStartNewMainDailyTask, mainDailyHistoryKey, readMainDailyHistory, startNewMainDailyTask } from './mainDailyWorkflow';
import type { MainDailyCheckpoint } from './mainDailyWorkflow';
import type { Research, Revision } from './domain';
import type { MainDailyRegistrationSummary } from './generated/api-contract';

function storage() {
  const values = new Map<string, string>();
  return {getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, raw: string) => {values.set(key, raw);} };
}
const pin = 'a'.repeat(64);
const base: MainDailyCheckpoint = {version: 1, workspaceId: 'workspace', handleId: 'source', handleDigest: pin};
const selected = {splits: {train: {start: '2024-01-01', end: '2024-01-03'}, validation: {start: '2024-01-04', end: '2024-01-12'}, test: {start: '2024-01-13', end: '2024-01-15'}}, min_assets: 3};
const evidence = {id: 'evidence101', page: 15, quote: '101 Formula original citation'};
const hypothesis = {id: 'h101', claim: 'original claim', attribution: 'paper_original', evidence_ids: ['evidence101'],
  economic_mechanism: 'original interpretation', mechanism_attribution: 'user_modification', signal_direction: 'source direction',
  required_fields: ['open', 'high', 'low', 'close'], assumptions: ['original condition']};
const candidate = {id: 'alpha101', hypothesis_id: 'h101', expression: '((close - open) / ((high - low) + .001))', origin: 'paper_original', changes: []};
const revision = {id: 'selected-v1', research_id: 'study', number: 1, task: {evidence: [evidence], hypotheses: [hypothesis], candidates: [candidate], evaluation: {min_assets: 5}, budget: {max_candidates: 5}}} as unknown as Revision;
const research = {id: 'study', paper_id: 'source-paper', latest_revision_id: 'unselected-v2', revisions: [revision]} as Research;
const registration = {dataset_id: 'registered-four-field', selection: {config: selected}} as MainDailyRegistrationSummary;

describe('main daily workflow frozen identities and original citations', () => {
  it('persists and reads a workspace scoped compact checkpoint', () => {
    const store = storage(); saveMainDailyCheckpoint(store, base);
    expect(readMainDailyCheckpoint(store, 'workspace')).toEqual(base);
    expect(readMainDailyCheckpoint(store, 'another-workspace')).toEqual({version: 1, workspaceId: 'another-workspace'});
    expect(JSON.stringify(base)).not.toContain('raw');
  });
  it('fails closed for cross workspace, unknown fields, bad pins and incomplete stages', () => {
    for (const value of [{...base, workspaceId: 'other'}, {...base, handleDigest: 'bad'}, {...base, raw_path: '/tmp/private'},
      {...base, approvalId: 'approval', approvalDigest: pin}, {...base, intentId: 'intent'}]) {
      const store = storage(); store.setItem(mainDailyCheckpointKey('workspace'), JSON.stringify(value));
      expect(() => readMainDailyCheckpoint(store, 'workspace')).toThrow();
    }
  });
  it('does not accept a storage write without exact readback', () => {
    expect(() => saveMainDailyCheckpoint({getItem: () => null, setItem: () => undefined}, base)).toThrow();
  });
  it('derives one sequential primary stage from saved server identities', () => {
    expect(mainDailyStage(base)).toBe(1);
    expect(mainDailyStage({...base, assessmentId: 'assessment'})).toBe(2);
    expect(mainDailyStage({...base, approvalId: 'approval'})).toBe(3);
    expect(mainDailyStage({...base, registrationId: 'registration'})).toBe(4);
    expect(mainDailyStage({...base, researchId: 'research'})).toBe(5);
  });
  it('preserves source quote, hypothesis, attribution and formula while changing only candidate identity', () => {
    const body = mainDailyResearchRequest(research, 'selected-v1', registration, 'OHLC Study') as {task: typeof revision.task};
    expect(body.task.evidence).toEqual([evidence]); expect(body.task.hypotheses).toEqual([hypothesis]);
    expect(body.task.candidates).toEqual([{...candidate, id: 'alpha101-101'}]);
    expect(revision.task.candidates[0].id).toBe('alpha101');
    body.task.hypotheses[0].claim = 'mutated returned copy';
    expect(hypothesis.claim).toBe('original claim');
  });
  it('uses registered frozen evaluation instead of the source study windows and never adds volume', () => {
    const body = mainDailyResearchRequest(research, 'selected-v1', registration, 'OHLC Study') as {task: typeof revision.task; dataset_id: string};
    expect(body.dataset_id).toBe('registered-four-field'); expect(body.task.evaluation).toEqual(selected);
    expect(body.task.budget).toEqual({max_candidates: 1, max_attempts_per_candidate: 1, max_tool_calls: 8, max_seconds: 30});
    expect(JSON.stringify(body)).not.toContain('volume'); expect(JSON.stringify(body)).not.toContain('execution');
  });
  it('refuses missing explicit revision instead of silently selecting latest', () => {
    expect(() => mainDailyResearchRequest(research, 'missing', registration, 'OHLC Study')).toThrow('不会替换');
  });
  it('refuses changed formulas and incomplete original evidence instead of generating substitutes', () => {
    const changed = structuredClone(research); changed.revisions![0].task.candidates[0].expression = 'rank(close)';
    expect(() => mainDailyResearchRequest(changed, 'selected-v1', registration, 'OHLC Study')).toThrow();
    const missing = structuredClone(research); missing.revisions![0].task.evidence = [];
    expect(() => mainDailyResearchRequest(missing, 'selected-v1', registration, 'OHLC Study')).toThrow('未完整');
  });
  it('allows a fresh source only after blocked admission or a terminal original run with all mutations resolved', () => {
    const options = {ready: true, busy: false, blocked: false, pending: false, mutationsReady: true};
    const admission = {...base, assessmentId: 'assessment', assessmentDigest: pin};
    const finished = {...admission, researchId: 'study', revisionId: 'selected-v1', runId: 'original-run'};
    expect(canStartNewMainDailyTask(admission, {...options, assessmentStatus: 'blocked'})).toBe(true);
    for (const runStatus of ['completed', 'failed', 'cancelled', 'interrupted'])
      expect(canStartNewMainDailyTask(finished, {...options, runStatus})).toBe(true);
    for (const runStatus of ['queued', 'running', 'cancelling', 'unknown', undefined])
      expect(canStartNewMainDailyTask(finished, {...options, runStatus})).toBe(false);
    for (const changed of [{ready: false}, {busy: true}, {blocked: true}, {pending: true}, {mutationsReady: false}])
      expect(canStartNewMainDailyTask(admission, {...options, ...changed, assessmentStatus: 'blocked'})).toBe(false);
    expect(canStartNewMainDailyTask({...admission, approvalId: 'grant'}, {...options, assessmentStatus: 'blocked'})).toBe(false);
  });
  it('archives every original ID and resets only the scoped browser checkpoint without discarding other data', () => {
    const store = storage();
    const checkpoint = {...base, assessmentId: 'assessment', assessmentDigest: pin,
      approvalId: 'approval', approvalDigest: pin, reservationId: 'reservation', reservationDigest: pin,
      normalizationKey: 'original-key', intentId: 'intent', intentDigest: pin, registrationId: 'registration',
      registrationDigest: pin, datasetId: 'data', researchId: 'study', revisionId: 'selected-v1', runId: 'original-run'};
    saveMainDailyCheckpoint(store, checkpoint); store.setItem('unrelated-record', 'untouched');
    const value = startNewMainDailyTask(store, checkpoint, ['recovery-assessment', 'recovery-run']);
    expect(value.checkpoint).toEqual({version: 1, workspaceId: 'workspace'});
    expect(readMainDailyCheckpoint(store, 'workspace')).toEqual(value.checkpoint);
    expect(readMainDailyHistory(store, 'workspace')).toEqual([checkpoint]);
    expect(store.getItem('unrelated-record')).toBe('untouched');
    expect(readMainDailyHistory(store, 'other-workspace')).toEqual([]);
  });
  it('refuses hidden pending mutations and changed active identity without clearing any original request', () => {
    const store = storage(); saveMainDailyCheckpoint(store, base);
    store.setItem('recovery-assessment', JSON.stringify({state: 'unconfirmed', idempotency_key: 'original'}));
    expect(() => startNewMainDailyTask(store, base, ['recovery-assessment'])).toThrow('待确认');
    expect(store.getItem('recovery-assessment')).toContain('original');
    expect(readMainDailyHistory(store, 'workspace')).toEqual([]);
    const clean = storage(); saveMainDailyCheckpoint(clean, {...base, handleId: 'changed'});
    expect(() => startNewMainDailyTask(clean, base, [])).toThrow('已改变');
    expect(readMainDailyCheckpoint(clean, 'workspace').handleId).toBe('changed');
  });
  it('refuses history overflow, malformed scope and failed archive readback while retaining the current IDs', () => {
    const store = storage(); saveMainDailyCheckpoint(store, base);
    for (const history of [{version: 1, workspaceId: 'other', checkpoints: []},
      {version: 1, workspaceId: 'workspace', checkpoints: Array(51).fill(base)},
      {version: 1, workspaceId: 'workspace', checkpoints: [{...base, raw_path: '/private/source'}]}]) {
      store.setItem(mainDailyHistoryKey('workspace'), JSON.stringify(history));
      expect(() => startNewMainDailyTask(store, base, [])).toThrow();
      expect(readMainDailyCheckpoint(store, 'workspace')).toEqual(base);
    }
    const broken = storage(); saveMainDailyCheckpoint(broken, base);
    expect(() => startNewMainDailyTask({getItem: broken.getItem, setItem: () => undefined}, base, [])).toThrow('未能确认');
    expect(readMainDailyCheckpoint(broken, 'workspace')).toEqual(base);
  });
  it('keeps an archived copy when active checkpoint write fails and does not duplicate it on recovery', () => {
    const store = storage(); saveMainDailyCheckpoint(store, base);
    let ignoreActive = true;
    const unstable = {getItem: store.getItem, setItem: (key: string, raw: string) => {
      if (key !== mainDailyCheckpointKey('workspace') || !ignoreActive) store.setItem(key, raw);
    }};
    expect(() => startNewMainDailyTask(unstable, base, [])).toThrow();
    expect(readMainDailyHistory(store, 'workspace')).toEqual([base]);
    expect(readMainDailyCheckpoint(store, 'workspace')).toEqual(base);
    ignoreActive = false;
    expect(startNewMainDailyTask(unstable, base, []).history).toEqual([base]);
  });
});
