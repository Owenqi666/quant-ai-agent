import type { PendingMutation } from "../reviewRecovery";
import { useI18n } from "../i18n";
import { JsonDetails } from "./ui";
import "./experimentFlow.css";

const recoveryNames: Record<string, string> = {
  "修订": "revision", "回归检查": "regression check", "领域研究执行": "domain research run", "领域执行步骤": "domain execution step",
  "行业 MOM 实验": "industry MOM experiment", "行业 MOM 重试": "industry MOM retry", "行业 MOM 取消": "industry MOM cancellation", "行业 MOM 案例": "industry MOM case",
  "研究任务": "research task", "研究准入": "research eligibility", "准入审核": "eligibility review", "参考集冻结": "reference-set freeze", "参考结果核对": "reference result check",
  "结论草稿": "claim draft", "材料标注": "material annotation", "结论审核": "claim review", "研究来源绑定": "research source binding", "月度实验": "monthly experiment",
  "月度重试": "monthly retry", "月度审核": "monthly review", "月度报告": "monthly report", "研究执行": "research run", "执行步骤": "execution step", "执行结果关联": "result binding",
  "问题创建": "issue creation", "问题处理": "issue decision", "工具会话": "tool session", "工具调用": "tool call", "AI 草案请求": "AI draft", "实验交接": "experiment handoff", "作者面板": "author panel",
};
export default function MutationRecovery({name,pending,error,busy,onRetry,onDismiss}: {
  name:string;pending:PendingMutation|null;error:string;busy:boolean;onRetry:()=>void;onDismiss:()=>void;
}) {
  const { t } = useI18n();
  const english = recoveryNames[name] || name;
  return <>{error&&<p className="info-note warning" role="alert">{error}</p>}{pending&&<section aria-label={t(`${name}提交恢复`, `${english} submission recovery`)} className="info-note flow-recovery">
    <p>{pending.state==="rejected"?t(`${name}请求已明确拒绝，可以解除后修改。`, `The ${english} request was explicitly rejected. Release it before editing.`):t(`${name}结果待确认。安全重试会使用已保存的原请求，不会采用当前表单或选择。`, `The ${english} outcome is uncertain. A safe retry uses the saved original request, not the current form or selection.`)}</p>
    <JsonDetails value={{path:pending.path,...pending.body}} label={t(`查看冻结${name}请求`, `View frozen ${english} request`)}/>
    {pending.state==="rejected"?<button type="button" className="button" disabled={busy} onClick={onDismiss}>{t(`解除被拒绝的${name}请求`, `Release rejected ${english} request`)}</button>:
      <button type="button" className="button" disabled={busy} onClick={onRetry}>{t(`安全重试${name}提交`, `Safely retry ${english} submission`)}</button>}
  </section>}</>;
}
