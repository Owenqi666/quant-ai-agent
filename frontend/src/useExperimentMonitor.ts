import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { activeStatuses, describeError } from "./domain";
import type { Artifact, Run } from "./domain";
import { useRunEvents } from "./useRunEvents";
import type { WorkspaceRequestScope, WorkspaceSnapshot } from "./workspaceScope";
import type { RunStatusResponse } from "./generated/api-contract";

type RunSnapshot = { workspace: WorkspaceSnapshot; run: Run };
type ArtifactSnapshot = { workspace: WorkspaceSnapshot; runId: string; artifacts: Artifact[] };

type Options = {
  runId: string;
  workspaceScope: WorkspaceRequestScope;
  refreshNonce: number;
  onRun: (run: Run) => void;
  onError: (message: string) => void;
};

/** One selected experiment owns its detail polling, artifacts, and event feed. */
export function useExperimentMonitor({ runId, workspaceScope, refreshNonce, onRun, onError }: Options) {
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [artifactSnapshot, setArtifactSnapshot] = useState<ArtifactSnapshot | null>(null);
  const scopeKey = workspaceScope.key;
  const run = snapshot && snapshot.run.id === runId && workspaceScope.isCurrent(snapshot.workspace) ? snapshot.run : null;
  const artifacts = artifactSnapshot && artifactSnapshot.runId === runId && workspaceScope.isCurrent(artifactSnapshot.workspace) ? artifactSnapshot.artifacts : [];
  const eventFeed = useRunEvents(runId, run?.status, refreshNonce, scopeKey);
  const reset = useCallback(() => { setSnapshot(null); setArtifactSnapshot(null); }, []);

  useEffect(() => {
    if (!runId) { reset(); return; }
    const abort = new AbortController();
    const workspace = workspaceScope.capture();
    const current = () => workspaceScope.isCurrent(workspace, abort.signal);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let detail:Run|null=null;
    let artifactAttempt = "";
    // A same-experiment refresh keeps the detail mounted, including unsaved review work.
    setArtifactSnapshot(null);
    async function poll() {
      try {
        if(detail&&!activeStatuses.has(detail.status)&&artifactAttempt===`${detail.attempt_count}:${detail.status_token || ""}`) {
          const probe=await api<RunStatusResponse>(`/runs/${runId}/status`,{signal:abort.signal});
          if(!current())return;
          if(detail.status_token && probe.change_token===detail.status_token) {
            timer=setTimeout(poll,5000);return;
          }
        }
        const response = await api<Run>(`/runs/${runId}`, { signal: abort.signal });
        if (!current()) return;
        const result={...response,client_loaded_at:new Date().toISOString()};
        detail=result;
        setSnapshot({ workspace, run: result });
        onRun(result);
        const artifactVersion=`${result.attempt_count}:${result.status_token || ""}`;
        if (!activeStatuses.has(result.status) && artifactAttempt !== artifactVersion) {
          const listed = await api<Artifact[]>(`/runs/${runId}/artifacts`, { signal: abort.signal });
          if (!current()) return;
          setArtifactSnapshot({ workspace, runId, artifacts: listed });
          artifactAttempt = artifactVersion;
        }
        if (current()) timer = setTimeout(poll, activeStatuses.has(result.status) ? 1500 : 5000);
      } catch (error) {
        if (current()) { onError(describeError(error)); timer = setTimeout(poll, 5000); }
      }
    }
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [runId, scopeKey, workspaceScope, refreshNonce, onRun, onError, reset]);

  return { run, artifacts, eventFeed, reset };
}
