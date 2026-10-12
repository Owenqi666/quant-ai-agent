"""Read saved main-producer series through the original immutable review target."""
from ..real_daily_main_workflow import value, FILES
from ..storage import digest, json_text
from .real_daily_main_execution import verify_saved_main_result
from .candidate_series_schema import CandidateSeriesResponse
from .service import ServiceError


def get_main_series(store,connection,run,candidate_id,attempt_id,result_digest):
    # Share projection arithmetic, never invoke a factor/evaluation tool.
    from .candidate_series import _project_points, _require, LIMITATIONS, SUMMARY_FIELDS, MAX_RESPONSE_BYTES
    verify_saved_main_result(store,connection,run)
    output=store.root/'runs'/run['id']/'attempts'/attempt_id/'output'
    state=value(output,'state.json');matches=[c for c in state['candidates'] if c['id']==candidate_id]
    if not matches:raise ServiceError('Candidate is absent from this frozen run',404)
    _require(len(matches)==1,'Duplicate main candidate identity')
    candidate=matches[0]
    _require(digest(candidate)==result_digest,'Selected result digest is stale; reload the experiment before viewing its series')
    result=value(output,'result.json');registration=value(output,'inputs/registration.json')
    _require(result==candidate['result'] and result['config']==state['task']['evaluation'],'Main result/config binding changed')
    metadata=result['market_metadata']
    _require(metadata['calendar_dates']==registration['selection']['development_sessions'] and
        metadata['universe']==registration['selection']['assets'] and
        metadata['method_digest']==registration['method_digest'] and metadata['selection_digest']==registration['selection_digest'] and
        metadata['final_numeric_access_allowed'] is False,'Main saved market metadata changed')
    points=_project_points(result,metadata)
    artifact=store._one(connection,'artifacts',connection.execute(
        "SELECT id FROM artifacts WHERE attempt_id=? AND name='result.json'",(attempt_id,)).fetchone()[0])
    revision=store._one(connection,'revisions',run['revision_id'])
    research=store._one(connection,'researches',run['research_id']);dataset=store._one(connection,'datasets',research['dataset_id'])
    payload={'schema_version':1,'run_id':run['id'],'research_id':run['research_id'],
        'revision_id':run['revision_id'],'revision_digest':revision['digest'],'attempt_id':attempt_id,
        'candidate_id':candidate_id,'result_digest':result_digest,'integrity':'verified','status':result['status'],'reason':result['reason'],
        'source':{'artifact_id':artifact['id'],'artifact_name':'result.json','artifact_sha256':artifact['sha256'],
            'original_sha256':candidate['artifacts']['result.json'],'result_sha256':digest(result),'json_pointer':'/daily'},
        'data':{'dataset_id':dataset['id'],'dataset_sha256':dataset['sha256'],'version':metadata['version'],
            'data_kind':metadata['data_kind'],'universe_size':len(metadata['universe'])},
        'evaluation':{'split':'validation',**result['config']['splits']['validation'],'min_assets':result['config']['min_assets']},
        'summary':{k:result['metrics'][k] for k in SUMMARY_FIELDS},'points':points,
        'limitations':list(LIMITATIONS)+result.get('limitations',[])+[
            '此结果来自主任务 OHLC-only 开发期路径；原价毛收益排除现金分红，保留最终期只含日期身份。']}
    response=CandidateSeriesResponse.model_validate(store._public(payload)).model_dump()
    if len(json_text(response).encode())>MAX_RESPONSE_BYTES:
        raise ServiceError('Candidate-series response exceeds 2 MiB; download the complete saved result',413)
    return response
