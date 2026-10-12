import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Research, Run } from "./domain";
import { browserSelectionStorage } from "./researchSelection";
import type { WorkspaceRequestScope } from "./workspaceScope";
import { readWorkPosition, resolveWorkPosition, saveWorkPosition, WorkPositionRequest, workPositionNoticeMessage } from "./workPosition";
import type { NavigationIssue, WorkPosition, WorkPositionNotice, WorkPositionResolution } from "./workPosition";

type State = { key: string; status: "idle" | "restoring" | "ready" | "unavailable"; notices: WorkPositionNotice[] };
/** App applies a completed resolution atomically; this hook never changes business state. */
export function useWorkPosition({ workspaceScope, researchId }: { workspaceScope: WorkspaceRequestScope; researchId: string }) {
  const key = JSON.stringify([workspaceScope.key, researchId]);
  const liveKey = useRef(key);
  liveKey.current = key;
  const request = useRef(new WorkPositionRequest()).current;
  const readyKey = useRef("");
  const [state, setState] = useState<State>({ key: "", status: "idle", notices: [] });
  useEffect(() => () => request.cancel(), [request]);

  const restore = useCallback(async (research: Research): Promise<WorkPositionResolution> => {
    if (liveKey.current !== key || JSON.stringify([workspaceScope.key, researchId]) !== key)
      return { status: "stale", position: null, run: null, notices: [] };
    const workspace = workspaceScope.capture();
    const isCurrent = request.begin(() => liveKey.current === key && workspaceScope.isCurrent(workspace));
    readyKey.current = "";
    setState({ key, status: "restoring", notices: [] });
    const saved = readWorkPosition(browserSelectionStorage(), workspace.workspaceId, researchId);
    const result = await resolveWorkPosition({
      workspaceId: workspace.workspaceId, researchId, research, ...saved, isCurrent,
      lookupRun: id => api<Run>(`/runs/${encodeURIComponent(id)}`),
      lookupIssue: id => api<NavigationIssue>(`/issues/${encodeURIComponent(id)}`),
    });
    if (!isCurrent() || result.status === "stale") return { status: "stale", position: null, run: null, notices: [] };
    if (result.status !== "unavailable") readyKey.current = key;
    setState({ key, status: result.status === "unavailable" ? "unavailable" : "ready", notices: result.notices });
    return result;
  }, [key, researchId, request, workspaceScope]);

  // A deliberate user navigation takes precedence over an in-flight restore.
  const cancel = useCallback(() => {
    if (liveKey.current !== key || JSON.stringify([workspaceScope.key, researchId]) !== key) return;
    request.cancel();
    readyKey.current = key;
    setState(old => ({ key, status: "ready", notices: old.key === key ? old.notices.filter(n => n === "storage-unavailable") : [] }));
  }, [key, researchId, request, workspaceScope]);
  const save = useCallback((position: WorkPosition) => {
    if (liveKey.current !== key || JSON.stringify([workspaceScope.key, researchId]) !== key || readyKey.current !== key || !workspaceScope.workspaceId || !researchId) return false;
    const result = saveWorkPosition(browserSelectionStorage(), workspaceScope.workspaceId, researchId, position);
    if (!result.saved) setState(old => ({ key, status: "ready", notices: [...new Set([...(old.key === key ? old.notices : []), ...result.notices])] }));
    return result.saved;
  }, [key, researchId, workspaceScope]);
  const visible = state.key === key ? state : { key, status: "idle" as const, notices: [] };
  return { restore, save, cancel, status: visible.status, ready: visible.status === "ready",
    notices: visible.notices, notice: visible.notices.map(workPositionNoticeMessage).join(" ") };
}
