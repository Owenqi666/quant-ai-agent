import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Health } from "./domain";
import { WorkspaceRequestScope } from "./workspaceScope";

export function useWorkspaceHealth() {
  const [health, setHealth] = useState<Health | null>(null);
  const scope = useRef(new WorkspaceRequestScope()).current;
  const beginHealthRequest = useCallback(() => scope.beginHealthRequest(), [scope]);
  const acceptHealth = useCallback((sequence: number, result: Health | null) => {
    if (scope.acceptHealthRequest(sequence, result?.workspace_id ?? null)) setHealth(result);
  }, [scope]);

  useEffect(() => {
    let active = true;
    const timer = setInterval(() => {
      const sequence = beginHealthRequest();
      api<Health>("/health")
        .then((result) => { if (active) acceptHealth(sequence, result); })
        .catch(() => { if (active) acceptHealth(sequence, null); });
    }, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [acceptHealth, beginHealthRequest]);

  return { health, workspaceId: scope.workspaceId, workspaceScope: scope, beginHealthRequest, acceptHealth };
}
