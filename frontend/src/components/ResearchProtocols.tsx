import { useEffect, useRef, useState } from "react";
import { api, ApiError, post } from "../api";
import { describeError, formatDate } from "../domain";
import type { ProtocolConfig, ProtocolPage, ProtocolPreset, ProtocolPreview, ProtocolRecord } from "../generated/api-contract";
import type { WorkspaceRequestScope } from "../workspaceScope";
import { finishProtocolSave, projectProtocol, protocolConfigError, protocolFields, protocolMonthError, protocolNumber, protocolRecoveryKey, protocolViewKey, readProtocolSave, stageProtocolSave } from "../researchProtocols";
import type { PendingProtocol } from "../researchProtocols";
import { writeDurably } from "../reviewRecovery";
import { Field, JsonDetails } from "./ui";
import "./ResearchProtocols.css";

const stateNames={blocked:"未决规则，暂不可计算",ready:"信号诊断已完成",partial:"部分资产或字段不可用"};
export default function ResearchProtocols({workspaceScope}: {workspaceScope:WorkspaceRequestScope}) {
  const [presets,setPresets]=useState<ProtocolPreset[]>([]),[config,setConfig]=useState<ProtocolConfig>(projectProtocol);
  const [target,setTarget]=useState("2025-01"),[title,setTitle]=useState("项目 MOM / ID 规则"),[note,setNote]=useState("");
  const [parent,setParent]=useState<string|null>(null),[selected,setSelected]=useState<ProtocolRecord|null>(null);
  const [page,setPage]=useState<ProtocolPage|null>(null),[offset,setOffset]=useState(0),[listVersion,setListVersion]=useState(0);
  const [preview,setPreview]=useState<ProtocolPreview|null>(null),[pending,setPending]=useState<PendingProtocol|null>(null);
  const [ready,setReady]=useState(false),[blocked,setBlocked]=useState(false),[loading,setLoading]=useState("");
  const [error,setError]=useState(""),[notice,setNotice]=useState("");
  const mounted=useRef(false),epoch=useRef(0),listEpoch=useRef(0),selectionEpoch=useRef(0);
  const identity=protocolViewKey(config,target),identityRef=useRef(identity);identityRef.current=identity;
  const recoveryKey=protocolRecoveryKey(workspaceScope.workspaceId);
  const invalid=protocolConfigError(config)||protocolMonthError(target);
  const activePreset=presets.find(item=>item.config.mode===config.mode);
  const pendingRef=useRef(pending);pendingRef.current=pending;
  useEffect(()=>{
    mounted.current=true;const workspace=workspaceScope.capture(),abort=new AbortController();
    try{setPending(readProtocolSave(localStorage,recoveryKey));setReady(true);}catch(error){setError(describeError(error));setReady(true);setBlocked(true);}
    void api<{presets:ProtocolPreset[]}>("/research-protocols/presets",{signal:abort.signal}).then(value=>{
      if(mounted.current&&workspaceScope.isCurrent(workspace,abort.signal))setPresets(value.presets);
    }).catch(error=>{if(mounted.current&&workspaceScope.isCurrent(workspace,abort.signal))setError(describeError(error));});
    return()=>{mounted.current=false;abort.abort();};
  },[workspaceScope,recoveryKey]);
  useEffect(()=>{
    const sequence=++listEpoch.current,workspace=workspaceScope.capture(),abort=new AbortController();
    void api<ProtocolPage>(`/research-protocols?limit=20&offset=${offset}`,{signal:abort.signal}).then(value=>{
      if(mounted.current&&sequence===listEpoch.current&&workspaceScope.isCurrent(workspace,abort.signal))setPage(value);
    }).catch(error=>{if(mounted.current&&workspaceScope.isCurrent(workspace,abort.signal))setError(describeError(error));});
    return()=>{abort.abort();};
  },[offset,listVersion,workspaceScope]);
  function invalidate(){epoch.current++;selectionEpoch.current++;setPreview(null);setError("");setNotice("");if(loading==="preview"||loading==="record")setLoading("");}
  function change(next:ProtocolConfig){invalidate();setConfig(next);}
  function choosePreset(mode:string){const preset=presets.find(item=>item.config.mode===mode);if(!preset)return;invalidate();setConfig(structuredClone(preset.config));setParent(null);setSelected(null);setTitle(preset.title);setNote("");}
  async function loadRecord(id:string){
    if(!id)return;invalidate();const request=++selectionEpoch.current,workspace=workspaceScope.capture();setLoading("record");
    try{const record=await api<ProtocolRecord>(`/research-protocols/${encodeURIComponent(id)}`);
      if(!mounted.current||request!==selectionEpoch.current||!workspaceScope.isCurrent(workspace))return;
      setSelected(record);setParent(record.id);setConfig(structuredClone(record.config));setTitle(record.title);setNote("");
    }catch(error){if(mounted.current&&request===selectionEpoch.current&&workspaceScope.isCurrent(workspace))setError(describeError(error));}
    finally{if(mounted.current&&request===selectionEpoch.current&&workspaceScope.isCurrent(workspace))setLoading("");}
  }
  async function runPreview(){
    if(invalid){setError(invalid);return;}
    const request=++epoch.current,expected=identity,workspace=workspaceScope.capture();setLoading("preview");setError("");setPreview(null);
    try{const result=await post<ProtocolPreview>("/research-protocols/preview",{config,target_month:target});
      if(mounted.current&&request===epoch.current&&expected===identityRef.current&&workspaceScope.isCurrent(workspace))setPreview(result);
    }catch(error){if(mounted.current&&request===epoch.current&&workspaceScope.isCurrent(workspace))setError(describeError(error));}
    finally{if(mounted.current&&request===epoch.current&&workspaceScope.isCurrent(workspace))setLoading("");}
  }
  async function saveVersion(){
    if(!ready||blocked||loading==="save")return;
    const workspace=workspaceScope.capture(),expected=identity,request=epoch.current;
    setLoading("save");setError("");setNotice("");let original:PendingProtocol|null=pendingRef.current;
    try{
      if(!original){if(protocolConfigError(config))throw new Error(protocolConfigError(config));if(!title.trim())throw new Error("请填写规则版本名称。");
        original=stageProtocolSave(localStorage,recoveryKey,{title:title.trim(),note:note.trim(),parent_id:parent,config});setPending(original);pendingRef.current=original;}
      if(original.state==="rejected")throw new Error("原请求已明确拒绝，请先解除后修改。");
      const record=await post<ProtocolRecord>("/research-protocols",original.body);
      if(!mounted.current||!workspaceScope.isCurrent(workspace))return;
      try{finishProtocolSave(localStorage,recoveryKey,original);setPending(null);pendingRef.current=null;}catch(error){setError(`版本已保存；本机确认未完成，请安全重试。${describeError(error)}`);}
      setNotice(`规则版本已保存：${record.id}。相同正文重试返回同一版本。`);setListVersion(value=>value+1);
      if(expected===identityRef.current&&request===epoch.current&&protocolViewKey(original.body.config,target)===expected){setSelected(record);setParent(record.id);}
    }catch(error){
      if(!mounted.current||!workspaceScope.isCurrent(workspace))return;
      const rejected=error instanceof ApiError&&!error.contractViolation&&error.status>=400&&error.status<500&&error.status!==409;
      if(rejected&&original){try{const next={...original,state:"rejected" as const};writeDurably(localStorage,recoveryKey,next);setPending(next);pendingRef.current=next;}catch{/* Keep the durable original and its safe replay. */}}
      setError(`${original?rejected?"服务明确拒绝了原保存请求。":"保存结果待确认；请使用原请求安全重试。":""}${describeError(error)}`);
    }finally{if(mounted.current&&workspaceScope.isCurrent(workspace))setLoading("");}
  }
  function dismissRejected(){if(pending?.state!=="rejected")return;try{finishProtocolSave(localStorage,recoveryKey,pending);setPending(null);pendingRef.current=null;setError("");}catch(error){setError(describeError(error));}}
  function downloadPreview(){if(!preview)return;const url=URL.createObjectURL(new Blob([JSON.stringify(preview,null,2)+"\n"],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download=`protocol-demo-${preview.windows.target_month}-${preview.config_digest.slice(0,12)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  const locked=!ready||blocked||!!pending;
  const unchanged=selected&&parent===selected.id&&protocolViewKey(selected.config,target)===identity&&selected.title===title.trim()&&selected.note===note.trim();
  return <div className="research-protocols">
    <section className="panel" aria-label="研究规则设置">
      <h2>选择规则 → 检查演示 → 保存版本</h2>
      <p>此入口独立保存月度研究规则，不需要先创建研究。预览只使用虚构日总收益与演示日历，输出信号和覆盖诊断；不产生未来收益、组合净值或交易结论。</p>
      {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
      {pending&&<section className="info-note" aria-label="规则保存恢复"><p>{pending.state==="rejected"?"原请求已明确拒绝，可解除后修改。":"保存结果待确认。安全重试使用下方冻结正文，内容寻址保证相同版本不重复创建。"}</p>
        <JsonDetails value={pending.body} label="查看原规则保存请求"/>
        {pending.state==="rejected"?<button className="button" onClick={dismissRejected}>解除被拒绝的规则请求</button>:<button className="button" disabled={!!loading} onClick={()=>void saveVersion()}>安全重试原规则保存</button>}
      </section>}
      <fieldset disabled={locked}>
        <Field label="规则预设"><select value={config.mode} onChange={event=>choosePreset(event.target.value)} disabled={!presets.length}><option value="project">项目约定（可派生）</option><option value="paper">论文原文（未决）</option></select></Field>
        <p>预设起点（当前参数以下方为准）：{activePreset?.description}</p>
        {config.mode==="paper"?<div className="info-note" aria-label="论文规则未决"><strong>论文预设未决，blocked；不会输出信号。</strong><p>MOM 保留十一自然月、跳过一月。ID 窗口和缺失规则尚未确认，不能将项目推测写为论文定义。</p>
          <ul>{activePreset?.unresolved.map(item=><li key={item}>{item}</li>)}</ul>
          <button type="button" className="button" onClick={()=>{change(projectProtocol());setTitle("由论文预设派生的项目规则");}}>明确切换为项目变体</button>
        </div>:<p className="info-note">当前身份：{identity===protocolViewKey(projectProtocol(),target)?"项目默认约定":"项目参数变体"}。MOM 回看 {Number.isFinite(config.mom_window_months)?config.mom_window_months:"待填写"} 个月、跳过 {Number.isFinite(config.mom_skip_months)?config.mom_skip_months:"待填写"} 个月；ID 回看 {Number.isFinite(config.id_window_months)?config.id_window_months:"待填写"} 个月、跳过 {Number.isFinite(config.id_skip_months)?config.id_skip_months:"待填写"} 个月；{config.missing_policy==="complete"?"要求完整有效观察":"按明确覆盖阈值使用有效观察"}。</p>}
        <fieldset disabled={config.mode==="paper"}>
          <div className="protocol-windows">{(["mom_window_months","mom_skip_months","id_window_months","id_skip_months"] as const).map(field=><Field key={field} label={protocolFields[field]}><input type="number" min={field.includes("window")?1:0} max={field.includes("window")?36:12} step="1" value={config[field]===null||!Number.isFinite(config[field])?"":config[field]!} onChange={event=>change({...config,[field]:event.target.value===""?NaN:Number(event.target.value)})}/></Field>)}</div>
          <details><summary>高级：覆盖与连续缺失规则</summary>
            <Field label="缺失观察规则"><select value={config.missing_policy} onChange={event=>change({...config,missing_policy:event.target.value as ProtocolConfig["missing_policy"],min_coverage:1,max_missing_run:0})}><option value="complete">完整观察（strict）</option><option value="available">允许部分有效观察（项目变体）</option></select></Field>
            {config.missing_policy==="available"&&<><p className="info-note warning">只复合已观察到的有效日收益，不代表完整窗口收益；缺行和空值不填 0，真实零收益进入有效观察分母。最少一个有效日，历史不足仍不可用。</p>
              <Field label="最低有效覆盖率" hint="比例值，大于 0 且不超过 1。"><input type="number" min="0.000001" max="1" step="any" value={Number.isFinite(config.min_coverage)?config.min_coverage!:""} onChange={event=>change({...config,min_coverage:event.target.value===""?NaN:Number(event.target.value)})}/></Field>
              <Field label="最大连续缺失交易日"><input type="number" min="0" max="366" step="1" value={Number.isFinite(config.max_missing_run)?config.max_missing_run!:""} onChange={event=>change({...config,max_missing_run:event.target.value===""?NaN:Number(event.target.value)})}/></Field></>}
          </details>
        </fieldset>
        <p className="fine-print">零收益：计入有效观察。填补：不填补。窗口依据自然月展开，缺失依据声明的交易日历判断。</p>
        <Field label="规则版本名称"><input value={title} maxLength={200} onChange={event=>{invalidate();setTitle(event.target.value);}}/></Field>
        <Field label="规则修改说明"><textarea value={note} maxLength={4000} rows={2} onChange={event=>{invalidate();setNote(event.target.value);}}/></Field>
      </fieldset>
      <Field label="诊断目标月份" hint="仅控制本次演示，不写入规则版本。"><input type="month" min="1905-01" max="2099-12" value={target} onChange={event=>{invalidate();setTarget(event.target.value);}}/></Field>
      {invalid&&<p role="alert">{invalid}</p>}
      <div className="button-row"><button className="button" disabled={!!invalid||!!loading} onClick={()=>void runPreview()}>预览日期与覆盖</button><button className="button primary" disabled={locked||!!protocolConfigError(config)||!title.trim()||!!loading||!!unchanged} onClick={()=>void saveVersion()}>保存规则版本</button></div>
      <p className="fine-print">父版本：{parent||"无（新根版本，与对应服务器预设比较）"}。保存后原版本不会被覆盖。</p>
      <details><summary>查看规则依据与当前 JSON</summary><ul>{activePreset?.sources.map(item=><li key={item}>{item}</li>)}</ul><pre>{JSON.stringify(config,null,2)}</pre></details>
    </section>
    {preview&&<section className="panel" aria-label="规则演示诊断" data-target-month={preview.windows.target_month}>
      <h2>{stateNames[preview.status]}</h2><p>演示数据：{preview.data_kind} · 来源 {preview.source_id} · 计算语义 {preview.semantics_version}</p>
      <p>目标月 {preview.windows.target_month} · 形成时点 {preview.windows.as_of}</p>
      <p>MOM：{preview.windows.momentum.start} 至 {preview.windows.momentum.end}；ID：{preview.windows.id?`${preview.windows.id.start} 至 ${preview.windows.id.end}`:"未决"}</p>
      {preview.warnings.length>0&&<ul>{preview.warnings.map((item,index)=><li key={index}>{item}</li>)}</ul>}
      <div className="table-scroll"><table><thead><tr><th>资产 / 状态</th><th>MOM</th><th>PRET</th><th>ID</th><th>MOM / ID 有效覆盖</th><th>原因</th></tr></thead><tbody>{preview.assets.map(asset=><tr key={asset.asset}><td>{asset.asset} / {asset.status}</td><td>{protocolNumber(asset.momentum,true)}</td><td>{protocolNumber(asset.pret,true)}</td><td>{protocolNumber(asset.id)}</td><td>{asset.momentum_coverage?`${asset.momentum_coverage.valid}/${asset.momentum_coverage.expected}`:"未决"} / {asset.id_coverage?`${asset.id_coverage.valid}/${asset.id_coverage.expected}`:"未决"}</td><td>{asset.reasons.join("；")||"满足当前规则"}</td></tr>)}</tbody></table></div>
      <details><summary>检查零收益、缺行、空值与连续缺失日期</summary>{preview.assets.map(asset=><div key={asset.asset}><h3>{asset.asset}</h3>{(["momentum_coverage","id_coverage"] as const).map(field=>{const coverage=asset[field];return <div key={field}><strong>{field==="momentum_coverage"?"MOM":"ID"}</strong>{coverage?<><p>应有 {coverage.expected}；有效 {coverage.valid}（正 {coverage.positive} / 负 {coverage.negative} / 零 {coverage.zero}）；缺行 {coverage.missing_rows}；空值 {coverage.null_values}；覆盖 {protocolNumber(coverage.coverage,true)}；最长缺失连段 {coverage.max_missing_run} 个交易日。</p><JsonDetails value={coverage.missing_dates} label="缺失日期与原因"/></>:<p>规则未决。</p>}</div>;})}</div>)}</details>
      <JsonDetails value={preview} label="完整演示结果、配置摘要与输入摘要"/>
      <button type="button" className="button small" onClick={downloadPreview}>下载演示诊断 JSON</button>
    </section>}
    <section className="panel" aria-label="已保存研究规则"><h2>已保存规则版本</h2><p>从记录中选择父版本，读取后可派生下一版；原版本保持不变。</p>
      <Field label="读取规则版本"><select value={selected?.id||""} disabled={locked} onChange={event=>void loadRecord(event.target.value)}><option value="">选择已保存版本</option>{selected&&!page?.items.some(item=>item.id===selected.id)&&<option value={selected.id}>{selected.title} · {selected.id.slice(-8)}</option>}{page?.items.map(record=><option key={record.id} value={record.id}>{record.title} · {record.id.slice(-8)} · {formatDate(record.created_at)}</option>)}</select></Field>
      <div className="button-row"><button className="button small" onClick={()=>setListVersion(value=>value+1)}>刷新规则列表</button><button className="button small" disabled={!offset} onClick={()=>setOffset(value=>Math.max(0,value-20))}>上一页规则</button><button className="button small" disabled={!page||offset+page.items.length>=page.total} onClick={()=>setOffset(value=>value+20)}>下一页规则</button><span>{page?`本页 ${page.items.length} / 共 ${page.total} 个版本`:"正在读取"}</span></div>
      {selected&&<article aria-label="保存规则详情"><h3>{selected.title}</h3><p>版本 {selected.id} · 父版本 {selected.parent_id||"无"}</p><p>{selected.note||"未填写修改说明"}</p>
        <div className="table-scroll"><table><thead><tr><th>规则变化</th><th>原值</th><th>新值</th></tr></thead><tbody>{selected.changes.map(item=><tr key={item.field}><td>{protocolFields[item.field as keyof typeof protocolFields]||item.field}</td><td>{item.before===null?"未决 / 空值":String(item.before)}</td><td>{item.after===null?"未决 / 空值":String(item.after)}</td></tr>)}</tbody></table></div>
        {!selected.changes.length&&<p>与父版本或对应预设的配置无差异。</p>}<JsonDetails value={selected} label="完整保存记录与摘要"/>
      </article>}
    </section>
  </div>;
}
