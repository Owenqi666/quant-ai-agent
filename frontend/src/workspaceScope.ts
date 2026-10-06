/** Confirmed workspace identity is separate from transient connection health. */
export type WorkspaceSnapshot = Readonly<{ workspaceId: string; epoch: number }>;

export class WorkspaceRequestScope {
  private identity = "";
  private epoch = 0;
  private healthSequence = 0;

  get workspaceId() { return this.identity; }
  get key() { return JSON.stringify([this.identity, this.epoch]); }

  beginHealthRequest() { return ++this.healthSequence; }

  /** Only the latest health request may change identity or connection state. */
  acceptHealthRequest(sequence: number, workspaceId: string | null) {
    if (sequence !== this.healthSequence) return false;
    if (workspaceId && workspaceId !== this.identity) {
      this.identity = workspaceId;
      this.epoch += 1;
    }
    return true;
  }

  capture(): WorkspaceSnapshot { return { workspaceId: this.identity, epoch: this.epoch }; }

  isCurrent(snapshot: WorkspaceSnapshot, signal?: AbortSignal) {
    return !signal?.aborted && snapshot.workspaceId === this.identity && snapshot.epoch === this.epoch;
  }
}
