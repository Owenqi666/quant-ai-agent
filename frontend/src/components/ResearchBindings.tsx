import {useEffect,useRef,useState} from "react";
import {api,post} from "../api";
import {describeError} from "../domain";
import type {StudyDetail,PaperSummary,ProtocolPage,BindingPreview,ResearchBinding,BindingPage} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";

export default function ResearchBindings({study,workspaceScope}:{study:StudyDetail;workspaceScope:WorkspaceRequestScope}){
  const [papers,setPapers]=useState<PaperSummary[]>([]),[protocols,setProtocols]=useState<ProtocolPage|null>(null),[paper,setPaper]=useState(""),[protocol,setProtocol]=useState(""),[protocolOffset,setProtocolOffset]=useState(0);
  const [title,setTitle]=useState(""),[note,setNote]=useState(""),[preview,setPreview]=useState<BindingPreview|null>(null),[body,setBody]=useState<Record<string,unknown>|null>(null);
  const [page,setPage]=useState<BindingPage|null>(null),[selected,setSelected]=useState<ResearchBinding|null>(null),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false),epoch=useRef(0);
  const mutation=useRecoverableMutation<ResearchBinding>(recoveryKey(workspaceScope.workspaceId,"research-binding-create",study.id,study.digest),"/research-bindings");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setProtocols(null);
    void Promise.all([api<PaperSummary[]>("/papers",{signal:abort.signal}),api<ProtocolPage>(`/research-protocols?limit=20&offset=${protocolOffset}`,{signal:abort.signal})]).then(([p,r])=>{if(workspaceScope.isCurrent(scope,abort.signal)){setPapers(p);setProtocols(r);}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[workspaceScope,protocolOffset]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setPage(null);
    void api<BindingPage>(`/research-bindings?study_id=${study.id}&limit=20&offset=${offset}`,{signal:abort.signal}).then(p=>{if(workspaceScope.isCurrent(scope,abort.signal))setPage(p);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[study.id,workspaceScope,offset,refresh]);
  function invalidate(){epoch.current++;setPreview(null);setBody(null);}
  async function check(){if(sending.current)return;sending.current=true;setBusy(true);setError("");setPreview(null);setBody(null);const scope=workspaceScope.capture(),seq=++epoch.current;
    try{const v=await post<BindingPreview>("/research-bindings/prepare",{paper_id:paper,protocol_id:protocol,study_id:study.id,source_scope:"author_paper",title:title.trim(),note});if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current){setPreview(v);setBody({...v.request,preview_digest:v.preview_digest});}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function save(request?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();
    try{const v=await mutation.execute(request);if(mounted.current&&workspaceScope.isCurrent(scope)){setSelected(v);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(()=>{invalidate();setTitle("");setNote("");});}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function open(id:string){const scope=workspaceScope.capture(),seq=++epoch.current;setSelected(null);setError("");setPreview(null);setBody(null);
    try{const v=await api<ResearchBinding>(`/research-bindings/${id}`);if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setSelected(v);}catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}
  }
  const locked=busy||!mutation.ready||!!mutation.pending||mutation.blocked;
  return <section aria-label="论文协议来源绑定" className="semantic-material"><h3>连接论文、规则与本次作者扫描</h3><p>当前 study 已固定。选择正确论文与已有协议，服务派生并核对源、计划、扫描及结果摘要。只保存研究准备；不会启动收益实验。</p>
    {error&&<p role="alert">{error}</p>}<MutationRecovery name="研究来源绑定" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    <details><summary>创建来源绑定</summary><fieldset disabled={locked}>
      <Field label="来源绑定标题"><input value={title} maxLength={200} onChange={e=>{setTitle(e.target.value);invalidate();}}/></Field>
      <Field label="选择绑定论文"><select value={paper} onChange={e=>{setPaper(e.target.value);invalidate();}}><option value="">请选择实际研究论文</option>{papers.map(p=><option key={p.id} value={p.id}>{p.title}</option>)}</select></Field>
      <Field label="选择绑定协议"><select value={protocol} onChange={e=>{setProtocol(e.target.value);invalidate();}}><option value="">请选择冻结规则</option>{protocols?.items.map(p=><option key={p.id} value={p.id}>{p.title}</option>)}</select></Field>
      <div className="button-row"><button className="button small" disabled={!protocolOffset} onClick={()=>{setProtocolOffset(v=>Math.max(0,v-20));setProtocol("");invalidate();}}>上一页绑定协议</button><button className="button small" disabled={!protocols||protocolOffset+20>=protocols.total} onClick={()=>{setProtocolOffset(v=>v+20);setProtocol("");invalidate();}}>下一页绑定协议</button></div>
      <Field label="来源绑定说明"><textarea value={note} maxLength={4000} onChange={e=>{setNote(e.target.value);invalidate();}}/></Field>
      <div className="button-row"><button className="button" disabled={!title.trim()||!paper||!protocol} onClick={()=>void check()}>核对论文规则与来源</button><button className="button primary" disabled={!body} onClick={()=>{if(body)void save(body);}}>保存准确来源绑定</button></div>
    </fieldset></details>
    {preview&&<><BindingOutput value={preview.context}/><JsonDetails label="预检的准确来源与规则" value={preview}/></>}
    <div className="button-row"><button className="button small" onClick={()=>setRefresh(v=>v+1)}>刷新来源绑定历史</button><button className="button small" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>上一页来源绑定</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(v=>v+20)}>下一页来源绑定</button></div>
    {page?.items.map(r=><p key={r.id}><button className="text-button" disabled={locked} onClick={()=>void open(r.id)}>查看来源绑定 {r.title} · {r.source_scope}</button></p>)}
    {selected&&<section aria-label="来源绑定详情" data-binding-id={selected.id}><BindingOutput value={selected.context}/><JsonDetails label="已冻结的研究来源" value={selected}/><a href={`/api/research-bindings/${selected.id}/export`} download>下载准确来源绑定</a></section>}
  </section>;
}
function BindingOutput({value}:{value:BindingPreview["context"]}){return <><p>执行状态：仍有阻断；当前核验 {value.verification_scope}，原 MAT 当次核验：否，组合执行就绪：否。</p><ul>{value.blockers.map((b,i)=><li key={i}>{b}</li>)}</ul></>;}
