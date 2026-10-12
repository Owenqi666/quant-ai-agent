"""Durable host permission for two research operations, independent of G4 evaluation.

Only the explicit local grant helper creates/revokes grants. HTTP can consume an
existing grant, never create permission. Every charged attempt is permanent,
including a failed preflight; there is no retry/refund or inferred authorization.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from ..storage import digest, json_text
from .db import connect, transaction
from .provider_contract import digest as wire_digest
from .provider_host_schema import HostGrantCreate, HostCapabilities
from .provider_research_schema import CONTRACT_ID
from .service import ServiceError
from .provider_native_transport_identity import (current_native_identity, authority_profile_digest,
    app_server_operation_supported)

PAIRS = {'extract_material':'paper_selection','generate_candidates':'accepted_material'}
ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = ('provider_host_authority.py','provider_host_schema.py','provider_runtime.py',
    'provider_native.py','provider_research_qualification.py','provider_research_api.py',
    'provider_research_api_schema.py','proposal_requests.py','proposal_worker.py',
    'research_session_execution.py','research_sessions.py','provider_research_context.py',
    'provider_research_contract.py','provider_material_bridge.py','paper_materials.py',
    'paper_segments.py','material_context.py','provider_field_semantics.py',
    'provider_contract_registry.py','paper_material_spans.py','provider_material_spans_contract.py')

class HostAuthorityError(ServiceError):
    def __init__(self,message,code='HOST_AUTHORITY_INVALID',status=409):
        super().__init__(message,status); self.code=code; self.retryable=False

def _need(value,message,code='HOST_AUTHORITY_INVALID'):
    if not value: raise HostAuthorityError(message,code)

def _now(): return datetime.now(timezone.utc)

def _decode(row):
    from .provider_contract import strict_json
    _need(row is not None,'Host authorization record was not found','HOST_GRANT_REQUIRED')
    value=strict_json(row['payload'].encode(),max_bytes=512*1024)
    _need(digest(value)==row['digest'],'Host authorization record digest differs')
    return value

def _workspace(connection):
    row=connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()
    _need(row is not None,'Workspace identity is missing'); return row[0]

def _profile(config):
    if config.mode=='codex_cli':
        identity, profile=current_native_identity(config,contract_id=CONTRACT_ID)
        _need(profile['static_checks']['passed'],'Native v2 profile failed static verification','NATIVE_PROFILE_UNSUPPORTED')
    else:
        identity={'contract_id':CONTRACT_ID,'config_digest':wire_digest(config.model_dump()),
                  'executable_sha256':hashlib.sha256(Path(config.executable).read_bytes()).hexdigest()}
    return {'native_identity':identity,'source_digests':{name:hashlib.sha256((ROOT/'paper_alpha/server'/name).read_bytes()).hexdigest() for name in SOURCE_FILES}}

class HostResearchAuthority:
    """The runtime authority is always reconstructed from the same host registry."""
    def __init__(self,store,registry): self.store,self.registry=store,registry

    def _grant(self,connection,identity,*,require_workspace=True):
        row=connection.execute('SELECT * FROM provider_host_grants WHERE id=?',(identity,)).fetchone()
        value=_decode(row)
        _need(value['id']==row['id']=='host_grant_'+digest(value['request']) and row['idempotency_key']==value['request']['idempotency_key'] and row['request_digest']==digest(value['request']),'Host grant identity differs')
        if require_workspace:
            _need(value['workspace_id']==_workspace(connection),'Host grant belongs to another workspace','HOST_WORKSPACE_MISMATCH')
        return value

    def _attempts(self,connection,grant):
        result=[]
        for row in connection.execute('SELECT * FROM provider_host_attempts WHERE grant_id=?',(grant['id'],)):
            value=_decode(row)
            _need(all(value[k]==row[k] for k in ('id','grant_id','session_id','proposal_id','attempt_id')),'Host attempt index differs')
            _need(value['operation'] in PAIRS and value['source_kind']==PAIRS[value['operation']],'Host attempt operation differs')
            result.append(value)
        actual = {r[0] for r in connection.execute(
            "SELECT a.id FROM proposal_attempts a JOIN proposal_requests p ON p.id=a.proposal_id "
            "JOIN provider_host_sessions s ON s.session_id=json_extract(p.payload,'$.request.session_id') WHERE s.grant_id=?",
            (grant['id'],))}
        _need(actual == {a['attempt_id'] for a in result}, 'Host permit inventory differs from the original charged attempts', 'HOST_ATTEMPT_INTEGRITY')
        return result

    def _active(self,connection,grant,*,allow_spent=False,operation=None):
        request=grant['request']; config=self.registry.ensure_dispatchable(request['provider_config_id'])
        _need(operation is None or app_server_operation_supported(config,operation),
              'App Server supports only compact extraction and generation in this version','APP_SERVER_OPERATION_UNSUPPORTED')
        _need(config.model_dump()==grant['config'] and _profile(config)==grant['profile'],'Host grant profile/configuration changed','HOST_PROFILE_CHANGED')
        revoked=connection.execute('SELECT * FROM provider_host_revocations WHERE grant_id=?',(grant['id'],)).fetchone()
        if revoked is not None:
            _decode(revoked); raise HostAuthorityError('Host grant was explicitly revoked','HOST_GRANT_REVOKED')
        _need(_now()<datetime.fromisoformat(grant['deadline_at']),'Host grant deadline elapsed','HOST_GRANT_EXPIRED')
        _need(not connection.execute("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'").fetchone(),'Provider dispatch is paused','DISPATCH_PAUSED')
        if request['authorization_kind']=='qualified': self._qualification(grant)
        attempts=self._attempts(connection,grant)
        if not allow_spent:
            _need(len(attempts)<request['max_dispatches'],'Host shared attempt budget is exhausted','HOST_BUDGET_EXHAUSTED')
            if operation:
                limit=request['max_extract' if operation=='extract_material' else 'max_generate']
                _need(sum(a['operation']==operation for a in attempts)<limit,'Host operation attempt budget is exhausted','HOST_OPERATION_EXHAUSTED')
        return config

    def _qualification(self,grant):
        from .proposal_requests import ProposalRequests
        ops=set()
        for frozen in grant['qualifications']:
            proposal=ProposalRequests(self.store,configs={}).get(frozen['proposal_id'])
            _need(proposal['status']=='ready','Qualified operation evidence is not accepted','HOST_QUALIFICATION_INVALID')
            attempt=next(a for a in proposal['attempts'] if a['id']==frozen['attempt_id'])
            evidence=self._saved_native(proposal,attempt)
            _need(evidence==frozen['evidence'] and proposal['config']==grant['config'],'Qualified operation evidence changed','HOST_QUALIFICATION_INVALID')
            ops.add(proposal['request']['operation'])
        wanted={'extract_material'} | ({'generate_candidates'} if grant['request']['max_generate'] else set())
        _need(ops==wanted,'Each operation requires independent current native evidence','HOST_QUALIFICATION_REQUIRED')

    def _check_source(self,connection,grant,descriptor,private,*,session_id=None):
        kind=descriptor['kind']; request=grant['request']
        _need(kind in PAIRS.values(),'Source is outside host research scope','HOST_OPERATION_DENIED')
        if kind=='paper_selection':
            _need(any(item['source']==descriptor and item['source_digest']==digest(private) for item in grant['selections']),'Paper selection is outside the frozen host grant','HOST_SOURCE_MISMATCH')
            _need(not any(a['operation']=='extract_material' and a['source_digest']==digest(private) and a['session_id']!=session_id for a in self._attempts(connection,grant)), 'This exact extraction selection already consumed its one attempt', 'HOST_SELECTION_SPENT')
            return 'extract_material'
        policy=request['generation_policy']
        _need(policy is not None and all(descriptor.get(k)==v for k,v in policy.items()),'Generation configuration differs from the host grant','HOST_SOURCE_MISMATCH')
        _need(private['dataset']==grant['generation_dataset'],'Generation dataset identity differs from the host grant','HOST_SOURCE_MISMATCH')
        from .paper_materials import PaperMaterials
        version=PaperMaterials(self.store).get_version(descriptor['material_id'],descriptor['version_id'])
        proposal_id=version['source']['proposal_id']
        row=connection.execute('SELECT * FROM provider_host_attempts WHERE grant_id=? AND proposal_id=?',(grant['id'],proposal_id)).fetchone()
        attempt=_decode(row)
        _need(attempt['operation']=='extract_material' and attempt['attempt_id']==version['source']['attempt_id'],'Material is not from this grant extraction','HOST_MATERIAL_LINEAGE_MISMATCH')
        return 'generate_candidates'

    def inspect_source(self,grant_id,descriptor,private,config_id):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN'); grant=self._grant(connection,grant_id)
            _need(grant['request']['provider_config_id']==config_id,'Grant names another provider','HOST_CONFIG_MISMATCH')
            operation=self._check_source(connection,grant,descriptor,private)
            self._active(connection,grant,operation=operation)
        return self.capability(grant_id,operation)

    def create_session(self,body,source,config):
        from .research_sessions import ResearchSessions
        from .provider_research_context import resolve_research_source
        grant_id=body.host_grant_id
        _need(grant_id is not None,'A local host grant is required','HOST_GRANT_REQUIRED')
        descriptor=body.source.model_dump()
        with transaction(self.store.db_path) as connection:
            grant=self._grant(connection,grant_id)
            old=connection.execute('SELECT id FROM research_sessions WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
            if old:
                self.verify_session_binding(connection,old['id'],grant_id)
            else:
                private=resolve_research_source(self.store,descriptor,connection=connection)
                _need(digest(private)==body.expected_source_digest,'Source changed before authorization','SOURCE_CHANGED')
                operation=self._check_source(connection,grant,descriptor,private)
                self._active(connection,grant,operation=operation)
                _need(config['provider_config_id']==grant['request']['provider_config_id'] and config['provider_config_digest']==wire_digest(grant['config']),'Grant configuration differs','HOST_CONFIG_MISMATCH')
                _need(body.budget.max_provider_dispatches==1,'One host session permits one provider attempt','HOST_BUDGET_MISMATCH')
                _need(_now()+timedelta(seconds=body.budget.max_seconds)<=datetime.fromisoformat(grant['deadline_at']),'Session exceeds the host absolute deadline','HOST_BUDGET_MISMATCH')
            session=ResearchSessions(self.store).create(source,body.purpose,config,body.budget.model_dump(),body.idempotency_key,_connection=connection)
            if not old:
                binding={'session_id':session['id'],'grant_id':grant_id,'source':descriptor,'source_digest':body.expected_source_digest,'operation':operation,'created_at':_now().isoformat()}
                connection.execute('INSERT INTO provider_host_sessions VALUES(?,?,?,?)',(session['id'],grant_id,json_text(binding),digest(binding)))
            return session

    def verify_session_binding(self,connection,session_id,grant_id):
        row=connection.execute('SELECT * FROM provider_host_sessions WHERE session_id=?',(session_id,)).fetchone()
        if row is None:
            _need(grant_id is None,'Replay cannot add a grant to an old session','IDEMPOTENCY_CONFLICT'); return None
        value=_decode(row)
        _need(value['session_id']==row['session_id']==session_id and value['grant_id']==row['grant_id']==grant_id,'Session replay differs from its original host grant','IDEMPOTENCY_CONFLICT')
        self._grant(connection,grant_id); return value

    def owns_session(self,connection,identity):
        return connection.execute('SELECT 1 FROM provider_host_sessions WHERE session_id=?',(identity,)).fetchone() is not None

    def verify_request(self,connection,request,config,*,phase):
        from .provider_research_context import resolve_research_source
        if hasattr(request,'model_dump'): request=request.model_dump()
        if hasattr(config,'model_dump'): config=config.model_dump()
        row=connection.execute('SELECT * FROM provider_host_sessions WHERE session_id=?',(request['session_id'],)).fetchone()
        binding=_decode(row); grant=self._grant(connection,binding['grant_id'])
        self.verify_session_binding(connection,request['session_id'],grant['id'])
        _need(request['operation'] in PAIRS and PAIRS[request['operation']]==request['source']['kind'] and request['target'] is None,'Operation is outside this host grant','HOST_OPERATION_DENIED')
        _need(binding['source']==request['source'] and binding['source_digest']==request['expected_source_digest'] and binding['operation']==request['operation'],'Proposal differs from the authorized session source','HOST_SOURCE_MISMATCH')
        _need(config==grant['config'],'Host grant uses another provider configuration','HOST_CONFIG_MISMATCH')
        _need(app_server_operation_supported(config, request['operation']),
              'App Server supports only compact extraction and generation in this version', 'APP_SERVER_OPERATION_UNSUPPORTED')
        prior=connection.execute('SELECT * FROM provider_host_attempts WHERE session_id=?',(request['session_id'],)).fetchone()
        self._active(connection,grant,allow_spent=prior is not None,operation=request['operation'])
        if prior:
            permit=_decode(prior)
            _need(permit['request_digest']==digest(request),'Session attempt cannot be replaced','HOST_ATTEMPT_SPENT')
        budget=request['budget']
        _need(budget['max_provider_invocations']==1 and budget['max_seconds']<=grant['request']['max_proposal_seconds'] and request['candidate_limit']==1 and all(budget[k] is None for k in ('hard_token_limit','hard_model_call_limit','hard_cost_limit')),'Proposal exceeds host limits','HOST_BUDGET_MISMATCH')
        source=resolve_research_source(self.store,request['source'],connection=connection)
        _need(digest(source)==request['expected_source_digest'],'Host source changed','SOURCE_CHANGED')
        self._check_source(connection,grant,request['source'],source,session_id=request['session_id'])
        return grant,binding

    def reserve_attempt(self,connection,proposal,attempt_id):
        request=proposal['request']; grant,binding=self.verify_request(connection,request,proposal['config'],phase='claim')
        old=connection.execute('SELECT * FROM provider_host_attempts WHERE session_id=?',(request['session_id'],)).fetchone()
        if old:
            value=_decode(old); _need(value['attempt_id']==attempt_id and value['proposal_id']==proposal['id'],'Spent attempt cannot be replaced','HOST_ATTEMPT_SPENT'); return value
        self._active(connection,grant,operation=request['operation'])
        value={'id':'host_permit_'+digest({'grant':grant['id'],'session':request['session_id']}),'grant_id':grant['id'],'session_id':request['session_id'],'proposal_id':proposal['id'],'attempt_id':attempt_id,'operation':request['operation'],'source_kind':request['source']['kind'],'source_digest':request['expected_source_digest'],'request_digest':digest(request),'grant_digest':digest(grant),'config_digest':wire_digest(proposal['config']),'profile_digest':authority_profile_digest(grant['profile'], grant['config']),'prepared_digest':None,'fencing_token':None,'dispatch_intent':None,'charged_at':_now().isoformat()}
        connection.execute('INSERT INTO provider_host_attempts VALUES(?,?,?,?,?,?,?)',(value['id'],grant['id'],request['session_id'],proposal['id'],attempt_id,json_text(value),digest(value)))
        return value

    def authorize_prepared(self,proposal,attempt,prepared_digest):
        from .proposal_requests import ProposalRequests
        from .provider_contract_registry import request_handler
        with transaction(self.store.db_path) as connection:
            self.verify_request(connection,proposal['request'],proposal['config'],phase='claim')
            row=connection.execute('SELECT * FROM provider_host_attempts WHERE attempt_id=?',(attempt['id'],)).fetchone(); value=_decode(row)
            handler=request_handler(proposal['request']); request=handler.parse_request(proposal['request'])
            expected={'proposal_id':proposal['id'],'attempt_id':attempt['id'],'context_digest':proposal['context_digest'],'input_text':handler.build_prompt(request,proposal['context'],proposal['id'],attempt['id']),'output_schema':handler.output_schema(proposal['context']),'provider_config':proposal['config'],'budget':proposal['request']['budget']}
            folder=ProposalRequests(self.store,configs={})._folder(proposal['id'])/'attempts'/attempt['id']
            actual=ProposalRequests(self.store,configs={})._json_path(folder/'prepared.json')
            _need(actual==expected and digest(actual)==prepared_digest==attempt['prepared_digest'],'Prepared content differs from frozen host request','HOST_PREPARED_MISMATCH')
            _need(value['prepared_digest'] in {None,prepared_digest} and value['fencing_token'] in {None,attempt['fencing_token']},'Prepared binding cannot change')
            value.update(prepared_digest=prepared_digest,fencing_token=attempt['fencing_token'])
            connection.execute('UPDATE provider_host_attempts SET payload=?,digest=? WHERE id=?',(json_text(value),digest(value),value['id']))

    @staticmethod
    def _summary(value):
        return {**{k:value[k] for k in ('grant_id','grant_digest','session_id','request_digest','proposal_id','attempt_id','config_digest','profile_digest','prepared_digest','operation','source_kind')},'permit_id':value['id'],'contract_id':CONTRACT_ID,'dispatch_intent_digest':digest(value['dispatch_intent']) if value['dispatch_intent'] else None}

    def verify_native(self,request,*,phase):
        from .proposal_requests import ProposalRequests
        from .research_session_execution import SessionExecution
        _need(phase in {'entry','dispatch'},'Unknown host dispatch phase')
        proposals=ProposalRequests(self.store,configs={},research_authority=self)
        with transaction(self.store.db_path) as connection:
            proposal=proposals._load(connection,request.proposal_id)
            self.verify_request(connection,proposal['request'],proposal['config'],phase='claim')
            attempt=next((a for a in proposal['attempts'] if a['id']==request.attempt_id),None)
            _need(attempt and proposal['status']=='running' and attempt['status']=='running','Host native attempt is not active')
            SessionExecution(self.store,proposals).authorize_proposal_dispatch(connection,proposal)
            row=connection.execute('SELECT * FROM provider_host_attempts WHERE attempt_id=?',(request.attempt_id,)).fetchone(); value=_decode(row)
            expected={'proposal_id':request.proposal_id,'attempt_id':request.attempt_id,'context_digest':request.context_digest,'input_text':request.input_text,'output_schema':request.output_schema,'provider_config':request.config.model_dump(),'budget':request.budget.model_dump()}
            _need(value['prepared_digest']==digest(expected)==attempt['prepared_digest'] and value['fencing_token']==attempt['fencing_token'],'Native input differs from prepared permit','HOST_PREPARED_MISMATCH')
            from .provider_contract_registry import request_handler
            handler=request_handler(proposal['request'])
            _need(request.output_schema==handler.output_schema(proposal['context'])
                and request.input_text==handler.build_prompt(handler.parse_request(proposal['request']),proposal['context'],proposal['id'],attempt['id']),
                'Native schema or prompt differs from frozen host context','HOST_PREPARED_MISMATCH')
            if phase=='dispatch':
                _need(value['dispatch_intent'] is None,'Native host permit was already consumed','HOST_DISPATCH_SPENT')
                value['dispatch_intent']={'permit_id':value['id'],'attempt_id':request.attempt_id,'prepared_digest':value['prepared_digest'],'created_at':_now().isoformat()}
                connection.execute('UPDATE provider_host_attempts SET payload=?,digest=? WHERE id=?',(json_text(value),digest(value),value['id']))
            return self._summary(value)

    def _saved_native(self,proposal,attempt,result=None):
        from .provider_research_qualification import verify_host_native_attempt
        from .provider_supervision_journal import read_metadata
        from .proposal_requests import ProposalRequests
        with closing(connect(self.store.db_path)) as connection:
            value=_decode(connection.execute('SELECT * FROM provider_host_attempts WHERE attempt_id=?',(attempt['id'],)).fetchone())
            _need(value['proposal_id']==proposal['id'] and value['prepared_digest']==attempt['prepared_digest'] and value['dispatch_intent'] is not None,'Result lacks its consumed host permit')
            grant = self._grant(connection, value['grant_id'])
            from .provider_schema import ProviderConfig
            _need(value['grant_digest'] == digest(grant) and grant['profile'] == _profile(ProviderConfig.model_validate(proposal['config'])), 'Host native evidence belongs to a changed authorization profile', 'HOST_PROFILE_CHANGED')
        evidence=verify_host_native_attempt(self.store,proposal,attempt,result=result)
        folder=ProposalRequests(self.store,configs={})._folder(proposal['id'])/'attempts'/attempt['id']/'transport'
        native=read_metadata(folder/'native-evidence.json',512*1024); intent=read_metadata(folder/'dispatch-intent.json')
        expected=self._summary(value)
        _need(native.get('research_authorization_dispatch')==expected and intent.get('research_authorization')==expected and native.get('research_authorization_entry')=={**expected,'dispatch_intent_digest':None},'Original native evidence differs from the host permit')
        return evidence

    def cancelled(self, proposal):
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM provider_host_sessions WHERE session_id=?', (proposal['request']['session_id'],)).fetchone()
            if row is None:
                return False
            binding = _decode(row); grant = self._grant(connection, binding['grant_id'])
            revoked = connection.execute('SELECT * FROM provider_host_revocations WHERE grant_id=?', (grant['id'],)).fetchone()
            if revoked is not None:
                _decode(revoked)
            return revoked is not None or _now() >= datetime.fromisoformat(grant['deadline_at'])

    def verify_result(self,proposal,attempt,*,response,receipt,error):
        return self._saved_native(proposal,attempt,{'response':response,'receipt':receipt,'error':error})

    def capability(self,grant_id,operation):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN'); grant=self._grant(connection,grant_id,require_workspace=False); request=grant['request']; attempts=self._attempts(connection,grant)
            reason=None; message='Fixture demonstration; no real model.' if request['authorization_kind']=='fixture' else 'Bounded real qualification; semantic fidelity remains unverified.' if request['authorization_kind']=='bounded_qualification' else 'Operation has current native transport evidence; semantic fidelity remains unverified.'
            qualified=False; supported=app_server_operation_supported(grant['config'],operation)
            try:
                _need(supported,'App Server supports only compact extraction and generation in this version','APP_SERVER_OPERATION_UNSUPPORTED')
                _need(grant['workspace_id']==_workspace(connection),'Host grant belongs to another workspace','HOST_WORKSPACE_MISMATCH')
                if request['authorization_kind']=='qualified':
                    self._qualification(grant)
                    qualified=True
                self._active(connection,grant,operation=operation)

            except (ServiceError,ValueError,OSError) as exc:
                reason=getattr(exc,'code','HOST_AUTHORITY_INVALID'); message=str(exc)
            return {'provider_config_id':request['provider_config_id'],'provider_mode':grant['config']['mode'],'operation':operation,'source_kind':PAIRS[operation],'supported':supported,'real_qualified':qualified,'available':reason is None,'reason_code':reason,'message':message,'host_grant_id':grant_id,'authorization_kind':request['authorization_kind'],'remaining_dispatches':max(0,request['max_dispatches']-len(attempts)),'remaining_operation_dispatches':max(0,request['max_extract' if operation=='extract_material' else 'max_generate']-sum(a['operation']==operation for a in attempts)),'deadline_at':grant['deadline_at'],'max_proposal_seconds':request['max_proposal_seconds'],'scope':{key:request[key] for key in ('paper_selections','generation_policy')}}

    def capabilities(self):
        result=[]
        with closing(connect(self.store.db_path)) as connection:
            ids=[r[0] for r in connection.execute('SELECT id FROM provider_host_grants ORDER BY id')]
        for config_id,config in self.registry.configs.items():
            found=False
            for identity in ids:
                with closing(connect(self.store.db_path)) as connection: grant=self._grant(connection,identity,require_workspace=False)
                if grant['request']['provider_config_id']!=config_id: continue
                found=True
                for op in PAIRS: result.append(self.capability(identity,op))
            if not found:
                error=None
                try: self.registry.ensure_dispatchable(config_id)
                except ServiceError as exc: error=exc
                for op in PAIRS:
                    supported=app_server_operation_supported(config,op)
                    available=supported and config.mode=='fixture' and error is None
                    result.append({'provider_config_id':config_id,'provider_mode':config.mode,'operation':op,'source_kind':PAIRS[op],'supported':supported,'real_qualified':False,'available':available,'reason_code':None if available else 'APP_SERVER_OPERATION_UNSUPPORTED' if not supported else getattr(error,'code','HOST_GRANT_REQUIRED'),'message':'Fixture demonstration; no real model.' if available else 'App Server supports only compact extraction and generation in this version' if not supported else str(error) if error else 'An explicit local host grant is required.','host_grant_id':None,'authorization_kind':'fixture' if config.mode=='fixture' else 'none','remaining_dispatches':0,'remaining_operation_dispatches':0,'deadline_at':None,'max_proposal_seconds':240,'scope':None})
        return HostCapabilities(items=result).model_dump()


def create_host_grant(store,registry,**kwargs):
    """Explicit local action only. Performs static checks; never invokes a CLI."""
    from .provider_research_context import resolve_research_source
    from .material_context import _dataset_source,parse_material_method_policy
    request=HostGrantCreate.model_validate(kwargs).model_dump()
    config=registry.ensure_dispatchable(request['provider_config_id'])
    if config.mode=='fixture': request['authorization_kind']='fixture'
    _need((config.mode=='fixture')==(request['authorization_kind']=='fixture'),'Authorization kind differs from provider mode')
    generation_dataset = None
    if request['generation_policy']:
        policy=request['generation_policy']
        generation_dataset = _dataset_source(store,policy['dataset_id'],policy['evaluation'])
        policy['evaluation']=generation_dataset['evaluation']
        policy['method_policy']=parse_material_method_policy(policy['method_policy'])
    authority=HostResearchAuthority(store,registry)
    with transaction(store.db_path) as connection:
        old=connection.execute('SELECT * FROM provider_host_grants WHERE idempotency_key=?',(request['idempotency_key'],)).fetchone()
        if old:
            existing=authority._grant(connection,old['id']); _need(existing['request']==request,'Grant key was used with another request','IDEMPOTENCY_CONFLICT'); return existing
        selections=[{'source':s,'source_digest':digest(resolve_research_source(store,s,connection=connection))} for s in request['paper_selections']]
        created=_now(); value={'id':'host_grant_'+digest(request),'request':request,'workspace_id':_workspace(connection),'config':config.model_dump(),'profile':_profile(config),'selections':selections,'generation_dataset':generation_dataset,'created_at':created.isoformat(),'deadline_at':(created+timedelta(seconds=request['max_seconds'])).isoformat(),'qualifications':[]}
        if request['authorization_kind']=='qualified':
            from .proposal_requests import ProposalRequests
            for identity in request['qualification_proposal_ids']:
                proposal=ProposalRequests(store,configs={})._load(connection,identity)
                _need(proposal['status']=='ready','Qualification proposal is not accepted','HOST_QUALIFICATION_REQUIRED')
                attempt=next(a for a in proposal['attempts'] if a['id']==proposal['accepted_attempt_id'])
                value['qualifications'].append({'proposal_id':identity,'attempt_id':attempt['id'],'evidence':authority._saved_native(proposal,attempt)})
            authority._qualification(value)
        else: _need(not request['qualification_proposal_ids'],'Bounded qualification cannot claim existing qualification')
        connection.execute('INSERT INTO provider_host_grants VALUES(?,?,?,?,?)',(value['id'],request['idempotency_key'],digest(request),json_text(value),digest(value)))
        return deepcopy(value)


def revoke_host_grant(store,registry,grant_id,reason):
    _need(isinstance(reason,str) and 0<len(reason.strip())<=1000,'Revocation reason is required')
    authority=HostResearchAuthority(store,registry)
    with transaction(store.db_path) as connection:
        authority._grant(connection,grant_id)
        old=connection.execute('SELECT * FROM provider_host_revocations WHERE grant_id=?',(grant_id,)).fetchone()
        if old: return _decode(old)
        value={'grant_id':grant_id,'reason':reason,'created_at':_now().isoformat()}
        connection.execute('INSERT INTO provider_host_revocations VALUES(?,?,?)',(grant_id,json_text(value),digest(value))); return value
