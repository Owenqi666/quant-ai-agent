import type { JsonObject } from "./domain";

export interface RunEvent extends JsonObject {
  id: number;
  sequence: number;
  run_id: string;
  attempt_id: string | null;
}
export interface EventPage {
  run_id: string;
  items: RunEvent[];
  next_cursor: number;
  has_more: boolean;
  high_watermark: number;
  total_records: number;
}
export interface EventHistory {
  items: RunEvent[];
  after: number;
  nextCursor: number;
  through: number;
  totalRecords: number;
  loadedThrough: number;
  hasMore: boolean;
  loading: boolean;
  error: string;
}
export interface EventFeedState {
  runId: string;
  items: RunEvent[];
  cursor: number;
  highWatermark: number;
  totalRecords: number;
  receivedRecords: number;
  syncing: boolean;
  caughtUp: boolean;
  error: string;
  history: EventHistory | null;
}
export const EVENT_PAGE_SIZE = 200;
export const EVENT_CACHE_LIMIT = 1000;
export const EVENT_PAGES_PER_CYCLE = 4;
export const emptyEventFeed = (runId = ""): EventFeedState => ({
  runId, items: [], cursor: 0, highWatermark: 0, totalRecords: 0,
  receivedRecords: 0, syncing: false, caughtUp: false, error: "", history: null,
});
export type EventPageRequest = {
  after: number; limit: number; through?: number; signal: AbortSignal;
};
export type FetchEventPage = (runId: string, request: EventPageRequest) => Promise<EventPage>;

function validatePage(page: EventPage, runId: string, after: number, through?: number) {
  const integer = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;
  if (!page || page.run_id !== runId || !Array.isArray(page.items) ||
      !integer(page.next_cursor) || !integer(page.high_watermark) || !integer(page.total_records) ||
      typeof page.has_more !== "boolean" || page.high_watermark < after ||
      (through !== undefined && page.high_watermark !== through) ||
      page.items.length > EVENT_PAGE_SIZE) {
    throw new Error("事件分页响应不符合当前实验与水位，已保留原游标。请重试或重新同步。");
  }
  let previous = after;
  for (const event of page.items) {
    if (event.run_id !== runId || !integer(event.sequence) || event.sequence <= previous ||
        event.sequence > page.high_watermark || event.id !== event.sequence) {
      throw new Error("事件顺序或所属实验无效，已保留原游标。");
    }
    previous = event.sequence;
  }
  if (page.next_cursor !== previous || (page.has_more && page.next_cursor === after) ||
      (!page.has_more && page.next_cursor !== page.high_watermark) ||
      page.total_records < page.items.length) {
    throw new Error("事件分页游标未正确推进，已停止本轮同步。");
  }
}

/** Pure, bounded consumer; the server remains the authoritative event history. */
export class EventFeed {
  state: EventFeedState;
  private abort = new AbortController();
  private historyAbort: AbortController | null = null;
  private pending: Promise<void> | null = null;
  private through: number | undefined;
  private requestedSnapshot = 0;
  private snapshotRevision = -1;
  private disposed = false;

  constructor(
    readonly runId: string,
    private fetchPage: FetchEventPage,
    private onChange: (state: EventFeedState) => void,
  ) { this.state = emptyEventFeed(runId); }

  private update(values: Partial<EventFeedState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...values };
    this.onChange(this.state);
  }

  /** In-flight snapshots cannot satisfy a newer terminal/status observation. */
  invalidateSnapshot() {
    this.requestedSnapshot += 1;
    this.update({ caughtUp: false });
  }

  sync(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (this.pending) return this.pending;
    this.pending = this.readCycle().finally(() => { this.pending = null; });
    return this.pending;
  }

  private async readCycle() {
    this.update({ syncing: true, error: "" });
    try {
      for (let count = 0; count < EVENT_PAGES_PER_CYCLE; count += 1) {
        const after = this.state.cursor;
        const requestRevision = this.requestedSnapshot;
        const through = this.through;
        const page = await this.fetchPage(this.runId, {
          after, limit: EVENT_PAGE_SIZE, through, signal: this.abort.signal,
        });
        if (this.disposed) return;
        validatePage(page, this.runId, after, through);
        const receivedRecords = this.state.receivedRecords + page.items.length;
        if (receivedRecords > page.total_records || (!page.has_more && receivedRecords !== page.total_records)) {
          throw new Error("事件总数与已接收记录不一致，不能确认历史完整；请重新同步。");
        }
        if (through === undefined) this.snapshotRevision = requestRevision;
        // Strictly increasing server IDs also prevent duplicate merges after retries.
        const merged = [...this.state.items, ...page.items];
        this.through = page.has_more ? page.high_watermark : undefined;
        this.update({
          items: merged.slice(-EVENT_CACHE_LIMIT), cursor: page.next_cursor,
          highWatermark: page.high_watermark, totalRecords: page.total_records,
          receivedRecords,
          caughtUp: !page.has_more && this.snapshotRevision === this.requestedSnapshot,
        });
        if (!page.has_more) break;
      }
    } catch (error) {
      if (!this.disposed) this.update({
        error: error instanceof Error ? error.message : String(error), caughtUp: false,
      });
    } finally { this.update({ syncing: false }); }
  }

  async openHistory() {
    this.historyAbort?.abort();
    const through = this.state.highWatermark;
    this.update({ history: {
      items: [], after: 0, nextCursor: 0, through, totalRecords: this.state.totalRecords,
      loadedThrough: 0, hasMore: through > 0, loading: false, error: "",
    } });
    await this.readHistory(0, 0);
  }

  async nextHistory() {
    const history = this.state.history;
    if (!history || history.loading || !history.hasMore) return;
    await this.readHistory(history.nextCursor, history.loadedThrough);
  }

  async retryHistory() {
    const history = this.state.history;
    if (!history || history.loading) return;
    // Failed next-page loads retain the displayed page and its cursor.
    const after = history.error && history.items.length ? history.nextCursor : history.after;
    const loaded = after === history.after ? history.loadedThrough - history.items.length : history.loadedThrough;
    await this.readHistory(after, loaded);
  }

  private async readHistory(after: number, loaded: number) {
    const history = this.state.history;
    if (!history) return;
    const controller = new AbortController();
    this.historyAbort?.abort();
    this.historyAbort = controller;
    this.update({ history: { ...history, loading: true, error: "" } });
    try {
      const page = await this.fetchPage(this.runId, {
        after, limit: EVENT_PAGE_SIZE, through: history.through, signal: controller.signal,
      });
      if (this.disposed || controller.signal.aborted || this.historyAbort !== controller) return;
      validatePage(page, this.runId, after, history.through);
      this.update({ history: {
        items: page.items, after, nextCursor: page.next_cursor, through: page.high_watermark,
        totalRecords: page.total_records, loadedThrough: loaded + page.items.length,
        hasMore: page.has_more, loading: false, error: "",
      } });
    } catch (error) {
      if (!this.disposed && !controller.signal.aborted && this.historyAbort === controller) {
        this.update({ history: { ...history, loading: false,
          error: error instanceof Error ? error.message : String(error) } });
      }
    }
  }

  closeHistory() {
    this.historyAbort?.abort();
    this.historyAbort = null;
    this.update({ history: null });
  }

  dispose() {
    this.disposed = true;
    this.abort.abort();
    this.historyAbort?.abort();
  }
}
