import { canReview } from "./domain";
import type { Run } from "./domain";
import type { EvidenceLineageResponse } from "./generated/api-contract";
export type { EvidenceLineageResponse } from "./generated/api-contract";
export type LineageTarget = EvidenceLineageResponse["target"];
export type LineageQuote = NonNullable<EvidenceLineageResponse["evidence"][number]["original"]>;
export type LineageChange = EvidenceLineageResponse["evidence"][number]["status"];
const object = (value:unknown):value is Record<string,unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value:unknown):value is string => typeof value === "string" && value.length > 0;
const sha = (value:unknown):value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const change = (value:unknown):value is LineageChange => ["unchanged","edited","added","unknown"].includes(String(value));
const strings = (value:unknown, max:number):value is string[] => Array.isArray(value) && value.length <= max && value.every(item=>typeof item === "string");
/** Python str/re \s whitespace after NFKC; FEFF is intentionally retained. */
export function normalizeLineageQuote(value:string):string {
  return value.normalize("NFKC").replace(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/gu," ").replace(/^ +| +$/g,"");
}
export function lineageTargetKey(target:LineageTarget):string {
  return JSON.stringify([target.run_id,target.revision_id,target.candidate_id,target.attempt_id,target.state_digest,target.result_digest]);
}
export function lineageTarget(run:Run,candidateId:string):LineageTarget|null {
  const candidates=run.state?.candidates?.filter(item=>item.id===candidateId) || [];
  const targets=run.review_targets?.filter(item=>item.candidate_id===candidateId) || [];
  const state=run.regression_target;
  if (!canReview(run) || candidates.length!==1 || targets.length!==1 || !state || state.attempt_id!==targets[0].attempt_id ||
    ![run.id,run.revision_id,candidateId,state.attempt_id].every(text) || !sha(state.result_digest) || !sha(targets[0].result_digest)) return null;
  return {run_id:run.id,revision_id:run.revision_id,candidate_id:candidateId,attempt_id:state.attempt_id,state_digest:state.result_digest,result_digest:targets[0].result_digest};
}
export function lineagePath(target:LineageTarget):string {
  const query=new URLSearchParams({candidate_id:target.candidate_id,expected_attempt_id:target.attempt_id,expected_state_digest:target.state_digest,expected_result_digest:target.result_digest});
  return `/runs/${encodeURIComponent(target.run_id)}/evidence-lineage?${query}`;
}
function quote(value:unknown,id:string):boolean {
  return value===null || (object(value) && value.id===id && Number.isInteger(value.page) && Number(value.page)>0 && Number(value.page)<=500 && text(value.quote) && value.quote.length<=8192 && text(value.chunk_id) &&
    Number.isInteger(value.normalized_start) && Number.isInteger(value.normalized_end) && Number(value.normalized_start)>=0 && Number(value.normalized_end)>Number(value.normalized_start) && Number(value.normalized_end)<=2000000 &&
    (value.printed_page===null || typeof value.printed_page === "string") && sha(value.original_quote_sha256));
}
/** Validate public identities and shape; the server proves private receipt digests. */
export function assertLineage(value:unknown,target:LineageTarget,run:Run):asserts value is EvidenceLineageResponse {
  const fail=()=>{throw new Error("LINEAGE_RESPONSE_MISMATCH");};
  const responseTarget=object(value) && object(value.target) ? value.target : {};
  if (!object(value) || value.schema_version!==1 || value.kind!=="run_evidence_lineage" || !object(value.target) ||
    Object.keys(target).some(key=>responseTarget[key]!==target[key as keyof LineageTarget]) ||
    !["verified","unknown"].includes(String(value.status)) || !text(value.reason_code) || value.semantic_fidelity!=="unverified" || value.llm_api_called!==false ||
    typeof value.public_projection_redacted!=="boolean" || !sha(value.public_view_digest) || !Array.isArray(value.evidence) || value.evidence.length>100 || !Array.isArray(value.history) || value.history.length>100 || !object(value.hypothesis)) return fail();
  const declared=run.state?.task?.candidates?.filter(item=>item.id===target.candidate_id) || [];
  const executed=run.state?.candidates?.filter(item=>item.id===target.candidate_id) || [];
  if (declared.length!==1 || executed.length!==1 || declared[0].hypothesis_id!==executed[0].hypothesis_id) return fail();
  const hypotheses=run.state?.task?.hypotheses?.filter(item=>item.id===declared[0].hypothesis_id) || [];
  if (hypotheses.length!==1 || value.hypothesis.id!==hypotheses[0].id || !change(value.hypothesis.status) || typeof value.hypothesis.inheritance_intact!=="boolean" || !strings(value.hypothesis.changes,30)) return fail();
  for (const stage of [value.hypothesis.original,value.hypothesis.adopted,value.hypothesis.final]) if (stage!==null && (!object(stage) || stage.id!==hypotheses[0].id || typeof stage.claim!=="string")) return fail();
  if (["unchanged","edited"].includes(String(value.hypothesis.status)) && (!value.hypothesis.original || !value.hypothesis.adopted || !value.hypothesis.inheritance_intact)) return fail();
  if (value.hypothesis.status==="added" && (!value.hypothesis.adopted || value.hypothesis.inheritance_intact)) return fail();
  const ids=hypotheses[0].evidence_ids;
  if (!Array.isArray(ids) || new Set(ids).size!==ids.length || value.evidence.length!==ids.length || new Set(value.evidence.map(row=>object(row)?row.id:null)).size!==value.evidence.length) return fail();
  for (const item of value.evidence) {
    if (!object(item) || !text(item.id) || !ids.includes(item.id) || !change(item.status) || !text(item.reason_code) || typeof item.inheritance_intact!=="boolean" || !quote(item.original,item.id) || !quote(item.adopted,item.id) || !object(item.final)) return fail();
    const final=run.state?.task?.evidence?.filter(row=>row.id===item.id) || [];
    if (final.length!==1 || item.final.id!==item.id || item.final.page!==final[0].page || !text(item.final.quote) || (!value.public_projection_redacted && normalizeLineageQuote(item.final.quote)!==normalizeLineageQuote(final[0].quote))) return fail();
    if (["unchanged","edited"].includes(item.status) && (!item.original || !item.adopted || !item.inheritance_intact)) return fail();
    if (item.status==="added" && (!item.adopted || item.inheritance_intact)) return fail();
    if (item.excerpt!==null) {
      const ex=item.excerpt;
      if (!object(ex) || ex.source_stage!=="adopted" || typeof ex.before!=="string" || typeof ex.after!=="string" || !text(ex.quote) || ex.before.length>512 || ex.after.length>512 || ex.quote.length>8192 || ex.bounded_context!==true || typeof ex.exact_highlight!=="boolean" || typeof ex.public_projection_redacted!=="boolean" ||
        (ex.public_projection_redacted && ex.exact_highlight) || !object(item.adopted) || (!ex.public_projection_redacted && (typeof item.adopted.quote!=="string" || ex.quote!==normalizeLineageQuote(item.adopted.quote)))) return fail();
    }
  }
  for (const row of value.history) if (!object(row) || !text(row.version_id) || !sha(row.digest) || !strings(row.changed_paths,256)) return fail();
  if (value.status==="unknown") {
    if (value.provenance!==null || value.hypothesis.status!=="unknown" || value.hypothesis.original!==null || value.hypothesis.adopted!==null || value.hypothesis.inheritance_intact || value.evidence.some(item=>item.status!=="unknown" || item.original!==null || item.adopted!==null || item.excerpt!==null)) return fail();
  } else {
    const p=value.provenance;
    if (!object(p) || !text(p.experiment_id) || !object(p.material) || !text(p.material.id) || !text(p.material.version_id) || !sha(p.material.draft_digest) || !sha(p.material.material_digest) || !object(p.selection)) return fail();
    for (const proposal of [p.generation,p.extraction]) if (!object(proposal) || !text(proposal.proposal_id) || !text(proposal.attempt_id) || !sha(proposal.context_digest) || !sha(proposal.accepted_response_digest)) return fail();
    if (!text(p.selection.paper_id) || ![p.selection.paper_sha256,p.selection.document_digest,p.selection.selection_digest].every(sha) || p.selection.normalization_version!=="nfkc-whitespace-unicode-v1" || p.selection.offset_basis!=="normalized_pdf_page_unicode_code_points_half_open" || p.selection.printed_page_verification!=="unverified") return fail();
  }
}
