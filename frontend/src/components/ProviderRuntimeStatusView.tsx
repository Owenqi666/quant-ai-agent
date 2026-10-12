import {useEffect,useState} from "react";
import {useI18n} from "../i18n";
import {api} from "../api";
import {describeError,formatDate} from "../domain";
import type {ProviderRuntimeStatus} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";


export default function ProviderRuntimeStatusView({workspaceScope,refresh}:{workspaceScope:WorkspaceRequestScope;refresh:number}) {
  const {t}=useI18n();
  const workerLabels={online:t("在线","Online"),stale:t("心跳已过期","Heartbeat expired"),stopped:t("已停止","Stopped"),not_observed:t("尚未观察到","Not yet observed")};
  const phaseLabels={idle:t("等待任务","Waiting for tasks"),preparing:t("准备请求","Preparing request"),provider_execution:t("正在生成","Generating"),paused:t("派发已暂停","Dispatch paused"),fenced:t("等待执行锁","Waiting for execution lock"),stopped:t("已停止","Stopped"),unknown:t("未知","Unknown")};
  const pauseLabels={interrupted_invocation:t("上次调用的停止状态待核对","Previous invocation stop state requires verification"),restored_workspace:t("恢复的工作区需要核对","Restored workspace requires verification"),other:t("宿主已暂停派发","Host paused dispatch"),none:t("无","None")};
  const [status,setStatus]=useState<ProviderRuntimeStatus|null>(null),[error,setError]=useState("");
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();
    let timer:ReturnType<typeof setTimeout>|undefined;
    setStatus(null);setError("");
    async function read(){
      try {
        const value=await api<ProviderRuntimeStatus>("/provider-runtime/status",{signal:abort.signal});
        if(workspaceScope.isCurrent(capture,abort.signal)){setStatus(value);setError("");}
      } catch(error) {
        if(workspaceScope.isCurrent(capture,abort.signal)){setStatus(null);setError(describeError(error));}
      } finally {
        if(workspaceScope.isCurrent(capture,abort.signal))timer=setTimeout(()=>void read(),5000);
      }
    }
    void read();
    return()=>{abort.abort();if(timer)clearTimeout(timer);};
  },[workspaceScope,refresh]);
  if(error)return <p role="status">{t("生成执行器状态暂不可用：","Generation executor status is unavailable: ")}{error}</p>;
  if(!status)return <p role="status">{t("正在读取生成执行器状态…","Loading generation executor status…")}</p>;
  const worker=status.proposal_worker;
  return <div aria-label={t("生成执行器运行状态","Generation executor runtime status")}>
    <p>{t("生成执行器：","Generation executor: ")}{workerLabels[worker.status]}{t("；最近阶段：","; latest phase: ")}{phaseLabels[worker.phase]}。
      {worker.last_seen&&<>{t(" 最后心跳："," Last heartbeat: ")}{formatDate(worker.last_seen)}。</>}</p>
    {status.dispatch_pause.paused&&<p>{t("暂停原因：","Pause reason: ")}{pauseLabels[status.dispatch_pause.reason]}。</p>}
    {status.recovery.required&&<p role="status">{t("需要在本机停服务后核对恢复记录；旧调用不会自动重跑。","Stop the local service before verifying recovery records. Previous invocations are not automatically rerun.")}
      {status.recovery.reason==="integrity_unknown"?t("部分记录无法核验。","Some records could not be verified."):t(`待处理尝试：${status.dispatch_pause.unresolved_count}。`,`Unresolved attempts: ${status.dispatch_pause.unresolved_count}.`)}</p>}
  </div>;
}
