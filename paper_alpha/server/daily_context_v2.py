"""Two registered daily projections with shared host binding and adoption rules."""
from copy import deepcopy
import json
from ..contracts import Evidence, Task
from ..daily_task_v2 import CORE_KEYS, parse_daily_task
from ..evidence import verify_paper, match_evidence
from ..expressions import validate_expression, ExpressionError
from ..storage import digest, json_text
from ..daily_adapter import _json_input
from ..workflow import verify_candidate_origin
from .daily_research_v2 import assert_daily_task, source_for_dataset, freeze_task
from .material_context import build_material_projection
from .paper_materials import PaperMaterials
from .paper_segments import verify_current_selection


def resolve_revision_source(store,research_id,base_revision_id,connection=None):
    research=store._fetch('researches',research_id); revision=store._revision(store._fetch('revisions',base_revision_id))
    if revision['research_id'] != research_id or research['latest_revision_id'] != base_revision_id or digest(revision['task']) != revision['digest']: raise ValueError('Research revision changed')
    source=assert_daily_task(store,revision['task'],research['dataset_id'],connection)
    paper=store._fetch('papers',research['paper_id']);document=json.loads(paper['document']);verify_paper(document,paper['pdf_path'])
    metadata=json.loads(source['dataset']['metadata'])
    file_metadata,metadata_file_sha256=_json_input(source['dataset']['metadata_path'])
    if digest(file_metadata)!=digest(metadata): raise ValueError('Daily metadata changed during source resolution')
    return {**source,'research':research,'revision':revision,'paper_sha256':paper['sha256'],
            'paper_document_digest':digest(document),'dataset_sha256':source['dataset']['sha256'],
            'dataset_metadata_digest':digest(metadata),'data_file_sha256':metadata['market_sha256'],
            'metadata_file_sha256':metadata_file_sha256}


def _projection(source,evidence,hypotheses):
    value={'schema_version':2,'context_version':'registered-daily-context-v2',
           'dataset_id':source['dataset']['id'],'registration_id':source['registration']['id'],
           'source_digest':digest(source),'execution':deepcopy(source['execution']),
           'evidence':deepcopy(evidence),'hypotheses':deepcopy(hypotheses),
           'catalog':deepcopy(source['catalog']),
           'capabilities':deepcopy(source['catalog']['public_projection']['declared_capabilities']),
           'development':deepcopy(source['catalog']['public_projection']['declared_capabilities']['development']),
           'semantic_fidelity':'unverified','real_source_ready':False,'test_freshness':'unknown',
           'exposure_manifest':{'source_may_contain_formula_answers':True,'review_source':'automation_or_maintained_assertions',
              'omitted':['candidate_answers','market_rows','final_bounds_values_labels_results','private_paths','review_or_oracle_labels']}}
    if len(json_text(value).encode())>131072: raise ValueError('Context exceeds 128 KiB; evidence is never truncated')
    return value


def build_revision_context(store,research_id,base_revision_id):
    source=resolve_revision_source(store,research_id,base_revision_id)
    task=source['revision']['task'];paper=store._fetch('papers',source['research']['paper_id']);document=json.loads(paper['document'])
    for item in task['evidence']: match_evidence(Evidence(**item),document)
    value=_projection(source,task['evidence'],[h for h in task['hypotheses'] if h['attribution']=='paper_original'])
    value.update(research_id=research_id,base_revision_id=base_revision_id,paper_id=source['research']['paper_id'],paper_sha256=source['paper_sha256'])
    if digest(resolve_revision_source(store,research_id,base_revision_id)) != digest(source): raise ValueError('Source changed during context projection')
    return value


def build_material_context(store,material_id,version_id,*,dataset_id,evaluation,method_policy,registration_id=None):
    if digest(method_policy) != digest({'schema_version':2,'kind':'registered_daily_v2','allow_field_substitution':False,'evaluation_period':'validation'}): raise ValueError('Closed registered daily material policy required')
    source=source_for_dataset(store,dataset_id,registration_id)
    if digest(evaluation)!=source['execution']['config_digest']: raise ValueError('Material evaluation differs from registration')
    version=PaperMaterials(store).get_version(material_id,version_id);verify_current_selection(store,version['selection'])
    adopted=build_material_projection(version)
    private={**source,'material_id':material_id,'version_id':version_id,'material_digest':version['digest'],'selection_digest':version['selection']['selection_digest'],'method_policy':deepcopy(method_policy)}
    value=_projection(private,adopted['evidence'],adopted['hypotheses']);value.update(origin=adopted['origin'],material_details=adopted['material_details'],paper_id=version['source']['paper_id'],paper_sha256=version['source']['paper_sha256'])
    if digest(source_for_dataset(store,dataset_id,registration_id))!=digest(source) or PaperMaterials(store).get_version(material_id,version_id)!=version: raise ValueError('Material source changed')
    verify_current_selection(store,version['selection'])
    return value


def validate_candidate_output(store,*,source,draft,allow_partial_execution=False,method_status='resolved',unresolved_rules=None):
    if method_status!='resolved' or unresolved_rules: return 'blocked',{'code':'RULES_UNRESOLVED','message':'Resolve original rules first'},{}
    task=freeze_task(store,source['research']['title'],source['research']['dataset_id'],{**draft,'evaluation':source['revision']['task']['evaluation'],'budget':source['revision']['task']['budget']},registration_id=source['execution']['registration_id'])
    paper=store._fetch('papers',source['research']['paper_id']);document=json.loads(paper['document']);verify_paper(document,paper['pdf_path'])
    for e in task['evidence']:match_evidence(Evidence(**e),document)
    old={h['id']:h for h in source['revision']['task']['hypotheses']};evidence={e['id']:Evidence(**e) for e in task['evidence']}
    fields={f['name'] for f in source['catalog']['public_projection']['declared_capabilities']['fields']}
    checks=[];accepted=[]
    parsed=Task.parse({**{k:task[k] for k in CORE_KEYS},'schema_version':1,'title':task['title'],'paper':'paper.json','paper_pdf':'paper.pdf','data':'market.csv','data_metadata':'metadata.json'})
    hypotheses={h.id:h for h in parsed.hypotheses}
    for h in task['hypotheses']:
        prior=old.get(h['id'])
        if h['attribution']=='paper_original' and (prior is None or any(digest(h[k])!=digest(prior[k]) for k in ('claim','evidence_ids','required_fields','signal_direction','assumptions'))): raise ValueError('ATTRIBUTION_INVALID')
        if h['mechanism_attribution']=='paper_original' and (prior is None or h['economic_mechanism']!=prior['economic_mechanism'] or prior['mechanism_attribution']!='paper_original'): raise ValueError('ATTRIBUTION_INVALID')
    for c in parsed.candidates:
        check={'candidate_id':c.id,'status':'ready','submitted_expression':c.expression,'accepted_expression':c.expression,'normalization':'unchanged','code':'VALIDATED'}
        try:
            h=hypotheses[c.hypothesis_id];verify_candidate_origin(c,h,evidence)
            v=validate_expression(c.expression,fields)
            if set(h.required_fields)-fields: raise ExpressionError('Declared field unavailable','required_field_missing')
            if set(v['used_fields'])-set(h.required_fields): raise ExpressionError('Undeclared expression field','undeclared_field')
            accepted.append(next(x for x in task['candidates'] if x['id']==c.id))
        except (ValueError,SyntaxError) as exc:
            check.update(status='blocked',code=getattr(exc,'code','ATTRIBUTION_INVALID'),message=str(exc),accepted_expression=None)
        checks.append(check)
    if not accepted or (len(accepted)!=len(checks) and not allow_partial_execution):return 'blocked',{'code':'CANDIDATES_BLOCKED','message':'Legal complete candidate set required'}, {'candidate_checks':checks}
    task['candidates']=accepted
    from .daily_research_v2 import inspect_daily_task
    preflight=inspect_daily_task(task,json.loads(source['dataset']['metadata']))
    if any(c['status']=='blocked' for c in preflight['candidates']): return 'blocked',{'code':'PREFLIGHT_BLOCKED','message':'No usable development rows; windows unchanged'},{'candidate_checks':checks,'preflight':preflight}
    assert_daily_task(store,task)
    return 'validated',None,{'task':task,'candidate_checks':checks,'preflight':preflight,'attribution_scope':'Exact assertions checked; economic fidelity unverified.'}
