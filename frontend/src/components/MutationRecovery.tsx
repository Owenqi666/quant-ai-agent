import type { PendingMutation } from "../reviewRecovery";
import { JsonDetails } from "./ui";

export default function MutationRecovery({name,pending,error,busy,onRetry,onDismiss}: {
  name:string;pending:PendingMutation|null;error:string;busy:boolean;onRetry:()=>void;onDismiss:()=>void;
}) {
  return <>{error&&<p role="alert">{error}</p>}{pending&&<section aria-label={`${name}提交恢复`} className="info-note">
    <p>{pending.state==="rejected"?`${name}请求已明确拒绝，可以解除后修改。`:`${name}结果待确认。安全重试会使用已保存的原请求，不会采用当前表单或选择。`}</p>
    <JsonDetails value={{path:pending.path,...pending.body}} label={`查看冻结${name}请求`}/>
    {pending.state==="rejected"?<button type="button" className="button" disabled={busy} onClick={onDismiss}>解除被拒绝的{name}请求</button>:
      <button type="button" className="button" disabled={busy} onClick={onRetry}>安全重试{name}提交</button>}
  </section>}</>;
}
