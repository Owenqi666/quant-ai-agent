import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { activeStatuses } from "./domain";
import { emptyEventFeed, EventFeed } from "./events";
import type { EventPage } from "./events";

export function useRunEvents(runId: string, status: string | undefined, refreshNonce: number, workspaceScope = "") {
  const [snapshot, setSnapshot] = useState(() => ({ workspaceScope, state: emptyEventFeed() }));
  const [resetNonce, setResetNonce] = useState(0);
  const current = useRef<{ workspaceScope: string; feed: EventFeed } | null>(null);
  const currentStatus = useRef(status);
  currentStatus.current = status;

  useEffect(() => {
    setSnapshot({ workspaceScope, state: emptyEventFeed(runId) });
    if (!runId) return;
    const feed = new EventFeed(runId, (id, request) => {
      const params = new URLSearchParams({ after: String(request.after), limit: String(request.limit) });
      if (request.through !== undefined) params.set("through", String(request.through));
      return api<EventPage>(`/runs/${id}/events/page?${params}`, { signal: request.signal });
    }, (state) => setSnapshot({ workspaceScope, state }));
    current.current = { workspaceScope, feed };
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      await feed.sync();
      if (!active) return;
      const delay = feed.state.error ? 5000 : !feed.state.caughtUp ? 250 :
        activeStatuses.has(currentStatus.current ?? "") ? 1500 : 5000;
      timer = setTimeout(poll, delay);
    }
    void poll();
    return () => {
      active = false;
      feed.dispose();
      clearTimeout(timer);
      if (current.current?.feed === feed) current.current = null;
    };
  }, [runId, resetNonce, workspaceScope]);

  useEffect(() => {
    const owned = current.current;
    if (owned?.workspaceScope !== workspaceScope || owned.feed.runId !== runId) return;
    owned.feed.invalidateSnapshot();
    void owned.feed.sync();
  }, [runId, status, refreshNonce, workspaceScope]);

  const feed = () => current.current?.workspaceScope === workspaceScope && current.current.feed.runId === runId ? current.current.feed : null;
  return {
    state: snapshot.workspaceScope === workspaceScope && snapshot.state.runId === runId ? snapshot.state : emptyEventFeed(runId),
    retry: () => { void feed()?.sync(); },
    reset: () => setResetNonce((value) => value + 1),
    openHistory: () => { void feed()?.openHistory(); },
    nextHistory: () => { void feed()?.nextHistory(); },
    retryHistory: () => { void feed()?.retryHistory(); },
    closeHistory: () => feed()?.closeHistory(),
  };
}
