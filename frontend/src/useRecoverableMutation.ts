import { useEffect, useMemo, useState } from "react";
import { ApiError, post } from "./api";
import { describeError } from "./domain";
import { readMutation, stageMutation, rejectMutation, acknowledgeMutation, sameMutationRequest } from "./reviewRecovery";
import type { PendingMutation } from "./reviewRecovery";

/** One mounted instance belongs to one immutable workspace/operation/target scope. */
export function useRecoverableMutation<T>(key: string, path: string) {
  const scope = useMemo(() => ({key,path,current:null as PendingMutation|null,active:false}),[key,path]);
  const [state,setState]=useState({scope,pending:null as PendingMutation|null,error:"",ready:false,blocked:false});
  const value=state.scope===scope?state:{scope,pending:null,error:"",ready:false,blocked:false};
  const {pending,error,ready,blocked}=value;
  const update=(changes:Partial<typeof state>)=>{if(scope.active)setState(previous=>({...previous,...changes,scope}));};
  useEffect(() => {
    scope.active = true;
    try {scope.current=readMutation(localStorage,key,path);setState({scope,pending:scope.current,error:"",blocked:false,ready:true});}
    catch(e){setState({scope,pending:null,error:`无法读取待确认请求。${describeError(e)}`,blocked:true,ready:true});}
    return () => {scope.active=false;};
  }, [scope,key,path]);
  async function execute(body?: Record<string, unknown>, context?: Record<string, unknown>): Promise<T> {
    let item = scope.current;
    if (!item) {
      if (!body || !ready || blocked) throw new Error("请求恢复状态尚未准备好，请检查提示。");
      try {
        item = stageMutation(localStorage, key, path, body, crypto.randomUUID(), context);
        scope.current = item;
        update({pending:item,error:""});
      } catch (e) {
        update({error:`无法持久保存请求，尚未发送。${describeError(e)}`});
        throw e;
      }
    }
    if (item.state === "rejected") throw new Error("请求已明确拒绝，请先解除该请求后修改。");
    let result: T;
    try { result = await post<T>(item.path, item.body); }
    catch (e) {
      // Only a valid, explicit client error proves this exact request was rejected.
      // A conflict can describe an existing receipt, so it remains pinned for diagnosis.
      const rejected = e instanceof ApiError && !e.contractViolation && e.status >= 400 && e.status < 500 && e.status !== 409;
      if (rejected) {
        try {
          const next = rejectMutation(localStorage, key, path, item);
          if (next && sameMutationRequest(scope.current, item)) {scope.current=next;update({pending:next});}
        }
        catch { /* Keep the already-durable original request; safe replay remains possible. */ }
      }
      if(sameMutationRequest(scope.current,item))update({error:`${rejected ? "服务明确拒绝了此次请求" : "提交结果待确认；服务器可能已保存，请用原请求安全重试"}。${describeError(e)}`});
      throw e;
    }
    // Keep the durable request until the caller has cleared its original draft.
    // A crash between POST success and local cleanup must replay the same key.
    if (!sameMutationRequest(scope.current, item)) throw new Error("原请求已被另一项确认流程处理；请刷新查看原记录，当前请求保持不变。");
    return result;
  }
  function acknowledge(cleanup?: () => void) {
    const item = scope.current;
    if (!item) throw new Error("没有可确认的原请求。");
    acknowledgeMutation(localStorage, key, path, item, cleanup);
    scope.current = null;
    update({pending:null,error:""});
  }
  function dismissRejected() {
    if (scope.current?.state !== "rejected") return;
    try { acknowledge(); }
    catch (e) { update({error:describeError(e)}); }
  }
  return { pending, error, ready, blocked, execute, acknowledge, dismissRejected };
}
