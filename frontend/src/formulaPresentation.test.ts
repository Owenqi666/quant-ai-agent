import {describe,expect,it} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {assertFormulaPresentation,formulaPresentationPath,isBoundedDerivedLatex} from './formulaPresentation';
import type {FormulaPresentationResponse} from './formulaPresentation';
import {formulaAlignmentTarget} from './formulaAlignment';
import type {Run} from './domain';
import FormulaPresentation,{FormulaReading} from './components/FormulaPresentation';
const sha='a'.repeat(64),result='b'.repeat(64);
const run:Run={id:'r',research_id:'research',revision_id:'v',status:'completed',mode:'normalized_fixed',created_at:'',attempt_count:1,
 verification:{verified:true},regression_target:{attempt_id:'a',result_digest:sha},review_targets:[{candidate_id:'c',attempt_id:'a',result_digest:result}],
 state:{task:{candidates:[{id:'c',hypothesis_id:'h',expression:'close-open',origin:'paper_original',changes:[]}]},candidates:[{id:'c',hypothesis_id:'h',expression:'close-open',origin:'paper_original',changes:[],status:'evaluated',result:{expression:'close-open',metrics:{}}}]}} as Run;
const target=formulaAlignmentTarget(run,'c')!;
const latex='\\operatorname{close} - \\operatorname{open}';
function fixture():FormulaPresentationResponse{return {schema_version:1,kind:'run_formula_presentation',policy_version:'bounded-local-dsl-latex-v1',target,items:[
 {role:'paper_reference',status:'available',expression:'close-open',latex},
 {role:'declared',status:'available',expression:'close-open',latex},
 {role:'executed',status:'available',expression:'close-open',latex}],alignment_public_view_digest:sha,public_projection_redacted:false,public_view_digest:sha,
 semantic_fidelity:'unverified',human_review_written:false,llm_api_called:false,factor_computation_called:false};}
describe('exact frozen mathematical reading contract',()=>{
 it('uses the same exact candidate, attempt, state and result GET target',()=>{
  const u=new URL(formulaPresentationPath(target),'http://localhost');expect(u.pathname).toBe('/runs/r/formula-presentation');expect(Object.fromEntries(u.searchParams)).toEqual({candidate_id:'c',expected_attempt_id:'a',expected_state_digest:sha,expected_result_digest:result});expect(()=>assertFormulaPresentation(fixture(),target,run)).not.toThrow();
 });
 it('rejects every wrong identity including stale revisions',()=>{for(const key of Object.keys(target) as (keyof typeof target)[]){const v=fixture();v.target={...target,[key]:'different'};expect(()=>assertFormulaPresentation(v,target,run)).toThrow();}});
 it('rejects a mismatching declaration or executed expression, not replacing it from another source',()=>{for(const index of [1,2]){const v=fixture();v.items[index].expression='rank(close)';expect(()=>assertFormulaPresentation(v,target,run)).toThrow();}});
 it('rejects role duplicates, dishonest semantic status and incoherent availability',()=>{
  const mutate=[(v:FormulaPresentationResponse)=>v.items[0].role='executed',(v:FormulaPresentationResponse)=>v.items[2].status='missing',(v:FormulaPresentationResponse)=>v.items[1].latex=null,(v:FormulaPresentationResponse)=>(v as unknown as {semantic_fidelity:string}).semantic_fidelity='passed'];
  for(const fn of mutate){const v=fixture();fn(v);expect(()=>assertFormulaPresentation(v,target,run)).toThrow();}
 });
 it('accepts absent verified execution explicitly and never substitutes the declaration',()=>{const v=fixture();v.items[2]={role:'executed',status:'missing',expression:null,latex:null};const noResult={...run,state:{...run.state,candidates:[{...run.state!.candidates![0],result:undefined}]}};expect(()=>assertFormulaPresentation(v,target,noResult)).not.toThrow();});
 it('supports text-only redacted public expressions without trying to render HTML',()=>{const v=fixture();v.public_projection_redacted=true;v.items[1]={role:'declared',status:'unsupported',expression:'<script>alert(1)</script>',latex:null};expect(()=>assertFormulaPresentation(v,target,run)).not.toThrow();const markup=renderToStaticMarkup(createElement(FormulaReading,{item:v.items[1]}));expect(markup).toContain('&lt;script&gt;');expect(markup).not.toContain('<script>');expect(markup).toContain('保留文本');});
});
describe('bounded derived notation before loading KaTeX',()=>{
 it('accepts exactly the backend formatter command vocabulary',()=>{for(const s of [latex,'\\frac{\\operatorname{close} - \\operatorname{open}}{\\left(\\operatorname{high} - \\operatorname{low} + .001\\right)}','\\operatorname{ts\\_delta}\\left(\\operatorname{close}, 1\\right)','\\mathtt{1e-400}','\\operatorname{close} \\ge 0'])expect(isBoundedDerivedLatex(s)).toBe(true);});
 it('rejects links, includes, styles, macro definitions, HTML and excessive input',()=>{for(const s of ['\\href{javascript:alert(1)}{x}','\\includegraphics{https://x}','\\htmlClass{x}{y}','\\def\\x{x}\\x','<script>x</script>','\\color{red}x','x'.repeat(65537),'x\\','x#y','\\newcommand{x}{y}'])expect(isBoundedDerivedLatex(s)).toBe(false);});
 it('retains original source and frozen semantic disclaimer before activating any renderer',()=>{const markup=renderToStaticMarkup(createElement(FormulaPresentation,{run,candidateId:'c'}));expect(markup).toContain('查看公式数学展示');expect(markup).toContain('不验证论文忠实度');expect(markup).not.toContain('<math');expect(markup).not.toContain('data-math-candidate');});
 it('retains missing-role wording without borrowing any expression',()=>{const markup=renderToStaticMarkup(createElement(FormulaReading,{item:{role:'executed',status:'missing',expression:null,latex:null}}));expect(markup).toContain('不借用其他表达式');expect(markup).not.toContain('<code');});
});
