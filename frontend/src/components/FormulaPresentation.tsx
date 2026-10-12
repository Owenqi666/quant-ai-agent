import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import type {Run} from '../domain';
import {formulaAlignmentTarget,formulaAlignmentTargetKey} from '../formulaAlignment';
import type {FormulaAlignmentTarget} from '../formulaAlignment';
import {assertFormulaPresentation,formulaPresentationPath} from '../formulaPresentation';
import type {FormulaPresentationResponse,FormulaReadingItem} from '../formulaPresentation';
import {useI18n} from '../i18n';
import './formulaPresentation.css';
const TIMEOUT_MS=15000;
export default function FormulaPresentation({run,candidateId}:{run:Run;candidateId:string}) {
  const target=formulaAlignmentTarget(run,candidateId),key=target?formulaAlignmentTargetKey(target):'unavailable';
  return <FormulaReader key={key} run={run} target={target}/>;
}
function FormulaReader({run,target}:{run:Run;target:FormulaAlignmentTarget|null}) {
  const {t}=useI18n();const [activated,setActivated]=useState(false),[retry,setRetry]=useState(0);
  const [result,setResult]=useState<FormulaPresentationResponse|null>(null),[error,setError]=useState<'timeout'|'unavailable'|null>(null);
  const sequence=useRef(0),key=target?formulaAlignmentTargetKey(target):'unavailable';
  useEffect(()=>{
    const epoch=++sequence.current,abort=new AbortController();let active=true,timedOut=false;setResult(null);setError(null);
    if(!activated||!target)return()=>{active=false;abort.abort();};
    const timer=setTimeout(()=>{timedOut=true;abort.abort();if(active&&sequence.current===epoch)setError('timeout');},TIMEOUT_MS);
    void api<unknown>(formulaPresentationPath(target),{signal:abort.signal}).then(value=>{
      assertFormulaPresentation(value,target,run);if(active&&!timedOut&&!abort.signal.aborted&&sequence.current===epoch)setResult(value);
    }).catch(()=>{if(active&&!timedOut&&!abort.signal.aborted&&sequence.current===epoch)setError('unavailable');}).finally(()=>clearTimeout(timer));
    return()=>{active=false;clearTimeout(timer);abort.abort();};
  },[activated,retry,key]);
  return <section className="formula-presentation" aria-label={t('数学阅读视图','Mathematical reading view')} aria-busy={activated&&!!target&&!result&&!error}>
    <details onToggle={event=>{if(event.currentTarget.open)setActivated(true);}}><summary>{t('查看公式数学展示','View mathematical formulas')}</summary>
      <p className="fine-print">{t('数学展示从本次冻结原式确定性派生，不用于执行，不验证论文忠实度；原始表达式始终保留。','Notation is derived deterministically from this frozen expression. It is not executable and does not verify paper fidelity; the literal expression is retained.')}</p>
      {!target?<p role="status">{t('缺少完整冻结目标，仅保留上方原式。','The complete frozen target is unavailable; literal expressions remain above.')}</p>
        :result?<div data-math-candidate={target.candidate_id}>{result.items.map(item=><FormulaReading key={item.role+item.expression} item={item}/>)}
          {result.public_projection_redacted&&<p className="fine-print">{t('本视图为脱敏公开投影；源与目标身份已由服务端核验。','This is a redacted public projection; the server verified source and target identity.')}</p>}</div>
          :error?<div role="status"><p>{error==='timeout'?t('数学视图读取超时；原式与计算结果仍可查看。','Mathematical view loading timed out; literal expressions and computed results remain available.'):t('数学视图暂不可用或身份不一致；原式完整保留。','The mathematical view is unavailable or its identity differs; literal expressions are retained.')}</p><button type="button" onClick={()=>setRetry(n=>n+1)}>{t('重试读取数学视图（只读）','Retry mathematical view (read only)')}</button></div>
            :<p role="status">{activated?t('正在读取冻结数学视图…','Loading the frozen mathematical view…'):t('展开后只读加载，不调用模型。','Expand to load this reading view; no model is called.')}</p>}
    </details>
  </section>;
}
export function FormulaReading({item}:{item:FormulaReadingItem}) {
  const {t}=useI18n(),target=useRef<HTMLDivElement>(null),sequence=useRef(0);const [state,setState]=useState<'idle'|'loading'|'ready'|'failed'>('idle');
  const label={paper_reference:t('论文参考式','Paper reference'),declared:t('候选声明式','Declared candidate expression'),executed:t('实际执行式','Executed expression')}[item.role];
  useEffect(()=>{
    const epoch=++sequence.current;let active=true;const node=target.current;node?.replaceChildren();
    if(item.status!=='available'||!item.latex){setState('idle');return()=>{active=false;};}
    setState('loading');const timer=setTimeout(()=>{if(active&&sequence.current===epoch)setState('failed');active=false;},TIMEOUT_MS);
    void import('../mathmlRenderer').then(renderer=>{
      if(!active||sequence.current!==epoch||!node)return;renderer.renderTrustedMath(item.latex!,node);setState('ready');
    }).catch(()=>{if(active&&sequence.current===epoch)setState('failed');}).finally(()=>clearTimeout(timer));
    return()=>{active=false;clearTimeout(timer);node?.replaceChildren();};
  },[item.latex,item.status]);
  return <article className="formula-reading-item" data-formula-role={item.role} data-math-state={state}><h5>{label}</h5>
    {item.expression===null?<p role="status">{t('本次未提供可确认的原式；不借用其他表达式。','No confirmed expression is available for this role; another expression is not substituted.')}</p>:<>
      <p className="formula-source-label">{t('原始表达式（保留文本）','Literal expression (retained source)')}</p><code className="expression">{item.expression}</code>
      {item.status==='unsupported'?<p className="fine-print" role="status">{t('当前语法未生成数学展示，保留文本。','This syntax has no mathematical display; the literal text is retained.')}</p>:<>
        <div className="formula-math" ref={target} aria-label={t('派生数学展示','Derived mathematical notation')} aria-hidden={state!=='ready'||undefined}/>
        {state==='loading'&&<p role="status">{t('正在加载本地数学渲染…','Loading local mathematical rendering…')}</p>}
        {state==='failed'&&<p role="status">{t('数学渲染不可用；请核对保留的原式。','Mathematical rendering is unavailable; inspect the retained literal expression.')} <button type="button" onClick={()=>window.location.reload()}>{t('重新加载页面以重试数学组件','Reload the page to retry mathematical rendering')}</button></p>}
      </>}
    </>}
  </article>;
}
