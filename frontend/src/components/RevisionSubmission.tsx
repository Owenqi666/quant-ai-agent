import { useEffect, useRef, useState } from "react";
import type { Dataset, Research, Revision, Task } from "../domain";
import { describeError } from "../domain";
import { draftKey as revisionDraftKey } from "../drafts";
import { recoveryKey } from "../reviewRecovery";
import { clearSubmittedDraft } from "../taskRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import MutationRecovery from "./MutationRecovery";
import RevisionEditor from "./RevisionEditor";

/** Recovery survives a changed selected base revision, scoped to the research. */
export default function RevisionSubmission({workspaceId,research,revision,dataset,editing,busy,onCancel,onSaved}: {
  workspaceId:string;research:Research;revision:Revision;dataset?:Dataset;editing:boolean;busy:string;
  onCancel:()=>void;onSaved:(revision:Revision,baseRevisionId:string)=>Promise<void>;
}) {
  const mutation=useRecoverableMutation<Revision>(recoveryKey(workspaceId,"revision-request",research.id),`/researches/${research.id}/revisions`);
  const [sending,setSending]=useState(false),[notice,setNotice]=useState(""),[error,setError]=useState("");
  const mounted=useRef(true);
  const view=useRef({revisionId:revision.id,editing,epoch:0});
  if(view.current.revisionId!==revision.id||view.current.editing!==editing)view.current={revisionId:revision.id,editing,epoch:view.current.epoch+1};
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  async function save(body?:Record<string,unknown>,draftKey?:string) {
    setSending(true);setNotice("");setError("");
    const originalView=view.current;
    let context=mutation.pending?.context;
    try {
      if(body)context={draftKey,draftRaw:draftKey?localStorage.getItem(draftKey):null};
      const saved=await mutation.execute(body,context);
      if(!mounted.current)return; // A remounted scope must explicitly confirm its own durable request.
      const base=String(body?.base_revision_id || mutation.pending?.body.base_revision_id || "");
      let warning="";
      try{mutation.acknowledge(()=>clearSubmittedDraft(localStorage,revisionDraftKey(workspaceId,`revision:${research.id}:${base}`),context));}
      catch(error){warning=`本机请求尚未清理，请安全重试完成确认。${describeError(error)}`;}
      setNotice(`版本 ${saved.number} 已保存，历史实验仍绑定原版本。${warning}`);
      // Completion cannot navigate a newer editor or a different workspace.
      if(view.current.epoch===originalView.epoch) {
        // Replaying confirms an earlier operation; its old base no longer owns navigation.
        try{await onSaved(saved,body?base:"");}catch(error){if(mounted.current)setNotice(`版本 ${saved.number} 已保存，列表刷新暂未成功。${describeError(error)}${warning}`);}
      }
    }catch(error){if(mounted.current)setError(describeError(error));}
    finally{if(mounted.current)setSending(false);}
  }
  return <>
    {notice&&<p role="status">{notice}</p>}
    <MutationRecovery name="修订" pending={mutation.pending} error={mutation.error||error} busy={sending||!!busy}
      onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    {editing&&<RevisionEditor key={`${workspaceId}:${research.id}:${revision.id}`} research={research} revision={revision}
      dataset={dataset} workspaceId={workspaceId} busy={sending||mutation.pending||mutation.blocked||!mutation.ready?"修订提交待确认":busy}
      onSave={(task:Task,note:string,key:string)=>save({base_revision_id:revision.id,task,note:note.trim()},key)} onCancel={onCancel}/>}
  </>;
}
