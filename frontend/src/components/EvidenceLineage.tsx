import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Run } from "../domain";
import { shortId } from "../domain";
import { assertLineage, lineagePath, lineageTarget, lineageTargetKey } from "../evidenceLineage";
import type { EvidenceLineageResponse, LineageChange, LineageQuote } from "../evidenceLineage";
import { useI18n } from "../i18n";
import { JsonDetails } from "./ui";

export const LINEAGE_TIMEOUT_MS=15000;
function useChangeLabel() {
  const {t}=useI18n();
  const labels:Record<LineageChange,string>={unchanged:t("未改动","Unchanged"),edited:t("已编辑","Edited"),added:t("新增或继承中断","Added or inheritance interrupted"),unknown:t("未知","Unknown")};
  return (value:LineageChange)=>labels[value];
}
function QuoteStage({quote,label}: {quote:LineageQuote|null;label:string}) {
  const {t}=useI18n();
  return <details className="lineage-stage"><summary>{label}</summary>{quote ? <>
    <p className="fine-print">{t("证据 ","Evidence ")}{quote.id} · {t("PDF 物理页 ","PDF physical page ")}{quote.page}<br/>
      {t("本阶段记录的 chunk：","Chunk recorded in this stage: ")}<code>{quote.chunk_id}</code><br/>
      {t("规范化整页字符区间：","Normalized full-page character range: ")}<code>[{quote.normalized_start}, {quote.normalized_end})</code>
      {quote.printed_page!==null && <><br/>{t("声明纸面页号（未核验）：","Declared printed page (unverified): ")}{quote.printed_page}</>}</p>
    <blockquote>{quote.quote}</blockquote>
    <p className="fine-print">{t("原记录引文 SHA256：","Recorded quotation SHA256: ")}<code>{quote.original_quote_sha256}</code></p>
  </> : <p>{t("此阶段没有可证明的原始记录。","No provable source record exists for this stage.")}</p>}</details>;
}
export function EvidenceLineageView({lineage}:{lineage:EvidenceLineageResponse}) {
  const {t}=useI18n(), changeLabel=useChangeLabel();
  const p=lineage.provenance;
  return <div className="lineage-record" data-lineage-run={lineage.target.run_id} data-lineage-revision={lineage.target.revision_id}
    data-lineage-candidate={lineage.target.candidate_id} data-lineage-attempt={lineage.target.attempt_id} data-lineage-state={lineage.target.state_digest} data-lineage-result={lineage.target.result_digest} data-lineage-status={lineage.status}>
    <p role="status">{lineage.status==="verified" ? t("已核对冻结来源账本；研究语义仍未评定。","Frozen source receipts checked; research semantics remain unassessed.") : t("未记录可证明的 AI 抽取来源，保留为未知。","A provable AI extraction source is not recorded; its lineage remains unknown.")} <code>{lineage.reason_code}</code></p>
    {lineage.public_projection_redacted && <p className="info-note">{t("此公开视图包含脱敏文本；摘要标识原记录，不能对公开文本重算来证明原文。","This public view contains redacted text. Digests identify the original record; hashing public text cannot prove the original.")}</p>}
    {p && <p className="fine-print">{t("采用材料版本：","Adopted material version: ")}<code>{p.material.version_id}</code> · {t("抽取尝试：","Extraction attempt: ")}{shortId(p.extraction.attempt_id)}</p>}
    <details className="lineage-hypothesis"><summary>{t("假说 ","Hypothesis ")}{lineage.hypothesis.id} · {changeLabel(lineage.hypothesis.status)}</summary>
      <p>{t("引文是否改动与假说是否改动分别核对。","Quotation changes and hypothesis changes are checked separately.")}</p>
      {!!lineage.hypothesis.changes.length && <p>{t("改动字段：","Changed fields: ")}{lineage.hypothesis.changes.join(" · ")}</p>}
      <JsonDetails value={lineage.hypothesis.original} label={t("原始抽取假说","Original extracted hypothesis")}/>
      <JsonDetails value={lineage.hypothesis.adopted} label={t("采用版本假说","Adopted hypothesis")}/>
      <JsonDetails value={lineage.hypothesis.final} label={t("冻结实验假说","Frozen experiment hypothesis")}/>
    </details>
    <p className="fine-print">{t("区间为规范化 PDF 整页文本的 Python Unicode 字符位置，左闭右开；不是 PDF 视觉坐标。下方上下文属于采用片段，不冒称原始 AI 片段的位置。","Ranges use Python Unicode code points in normalized full-page PDF text, start inclusive and end exclusive; they are not PDF visual coordinates. Context below belongs to the adopted excerpt and does not claim an original AI span.")}</p>
    {lineage.evidence.map(item=><details className="lineage-evidence" key={item.id} data-lineage-evidence={item.id} data-lineage-change={item.status}><summary>{t("证据 ","Evidence ")}{item.id} · {changeLabel(item.status)} · {t("物理页 ","physical page ")}{item.final.page}</summary>
      <p className="fine-print"><code>{item.reason_code}</code> · {item.inheritance_intact ? t("版本链中保留相同证据 ID；标签不等于语义通过。","The same evidence ID is retained through the version chain; this does not establish semantic validity.") : t("未证明连续继承，不能把同名或同文当作原始 AI 来源。","Continuous inheritance is unproved. Matching IDs or text do not establish an original AI source.")}</p>
      <QuoteStage quote={item.original} label={t("1 · 原始 AI 抽取记录","1 · Original AI extraction record")}/>
      <QuoteStage quote={item.adopted} label={t("2 · 最终采用版本","2 · Exact adopted version")}/>
      <details className="lineage-stage"><summary>{t("3 · 冻结实验证据","3 · Frozen experiment evidence")}</summary><p className="fine-print">{item.final.id} · {t("PDF 物理页 ","PDF physical page ")}{item.final.page}</p><blockquote>{item.final.quote}</blockquote></details>
      {item.excerpt && <details className="lineage-stage"><summary>{t("采用片段与有界上下文","Adopted excerpt with bounded context")}</summary>
        <p className="fine-print">{t("只显示相邻最多 256 个规范化字符；完整引文仍在各阶段中可读。","At most 256 neighboring normalized code points are shown; the full quotation remains available in each stage.")}</p>
        {item.excerpt.public_projection_redacted || !item.excerpt.exact_highlight ? <><p className="info-note">{t("此片段已脱敏或无法显示精确文字高亮。","This excerpt is redacted or cannot show an exact text highlight.")}</p><blockquote>{item.excerpt.before}{item.excerpt.quote}{item.excerpt.after}</blockquote></>
          : <blockquote>{item.excerpt.before}<mark>{item.excerpt.quote}</mark>{item.excerpt.after}</blockquote>}
      </details>}
    </details>)}
    <details className="lineage-identities"><summary>{t("来源身份、版本历史与公开视图摘要","Source identities, version history and public view digest")}</summary>
      <JsonDetails value={p} label={t("来源身份与原记录摘要","Source identities and original record digests")}/>
      <ul>{lineage.history.map(item=><li key={item.version_id}><code>{item.version_id}</code> · {item.changed_paths.join(" · ") || t("无改动字段","No changed fields")}<br/><code>{item.digest}</code></li>)}</ul>
      <p className="fine-print">{t("公开视图摘要（由服务计算）：","Public view digest (computed by the server): ")}<code>{lineage.public_view_digest}</code></p>
      <p className="fine-print">{t("此操作只读取冻结记录，未调用模型。","This operation reads frozen records and does not call a model.")}</p>
    </details>
  </div>;
}
export default function EvidenceLineage({run,candidateId}:{run:Run;candidateId:string}) {
  const {t}=useI18n();
  const target=lineageTarget(run,candidateId), key=target ? lineageTargetKey(target) : "unavailable";
  const sequence=useRef(0), [retry,setRetry]=useState(0);
  const [state,setState]=useState<{key:string;lineage?:EvidenceLineageResponse;error?:"timeout"|"unavailable"}>({key});
  useEffect(()=>{
    const epoch=++sequence.current, controller=new AbortController();
    let active=true, timedOut=false;
    setState({key});
    if (!target) return ()=>{active=false;controller.abort();};
    const timeout=setTimeout(()=>{timedOut=true;controller.abort();if(active && sequence.current===epoch)setState({key,error:"timeout"});},LINEAGE_TIMEOUT_MS);
    api<unknown>(lineagePath(target),{signal:controller.signal}).then(value=>{
      assertLineage(value,target,run);
      if(active && !timedOut && !controller.signal.aborted && sequence.current===epoch)setState({key,lineage:value});
    }).catch(()=>{if(active && !timedOut && !controller.signal.aborted && sequence.current===epoch)setState({key,error:"unavailable"});}).finally(()=>clearTimeout(timeout));
    return ()=>{active=false;clearTimeout(timeout);controller.abort();};
  },[key,retry,run]);
  const current=state.key===key ? state : null;
  return <section className="evidence-lineage" aria-label={t("原始片段与采用追溯","Original excerpts and adoption lineage")} aria-busy={!!target && !current?.lineage && !current?.error}>
    <details><summary>{t("原始片段与采用追溯","Original excerpts and adoption lineage")}</summary>
      {!target ? <p role="status">{t("缺少完整冻结目标，追溯保持未知；现有计算输出可独立核对。","The complete frozen target is unavailable. Lineage remains unknown; computed output can be checked independently.")}</p>
        : current?.lineage ? <EvidenceLineageView key={key} lineage={current.lineage}/>
          : current?.error ? <div role="status"><p>{current.error==="timeout" ? t("来源读取已超时，现有计算输出不受影响。","Source loading timed out; computed output is unaffected.") : t("来源暂不可读取或身份不一致，现有计算输出不受影响。","The source is unavailable or its identity differs; computed output is unaffected.")}</p><button type="button" onClick={()=>setRetry(value=>value+1)}>{t("重新读取来源（只读）","Reload source (read only)")}</button></div>
            : <p role="status">{t("正在读取当前冻结目标的来源…","Loading sources for this frozen target…")}</p>}
    </details>
  </section>;
}
