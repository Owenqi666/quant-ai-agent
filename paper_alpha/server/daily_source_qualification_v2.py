"""Source-review completeness is distinct from independent real/PIT qualification."""
from copy import deepcopy
import re
from ..daily_contract import parse_contract, parse_source_receipt
from ..storage import digest
FACTS=('provider','rights','calendar','units','universe','events','vintage','raw_identity')

def validate_source_review(value, *, contract, receipt):
    c=parse_contract(contract);r=parse_source_receipt(receipt)
    if type(value) is not dict or set(value) != {'schema_version','source_id','facts','pit','reviewer','limitations'}:
        raise ValueError('Closed source review required')
    if type(value['schema_version']) is not int or value['schema_version'] != 1: raise ValueError('Unsupported source review')
    if value['source_id'] != r.to_dict()['source_id'] or value['source_id'] != c.to_dict()['source']['source_id']: raise ValueError('Source identities disagree')
    if type(value['facts']) is not dict or set(value['facts']) != set(FACTS): raise ValueError('Every required source fact must remain in denominator')
    for name,item in value['facts'].items():
        if type(item) is not dict or set(item) != {'status','statement','evidence_sha256'}: raise ValueError('Closed source fact required')
        if item['status'] not in ('unknown','supported','rejected'): raise ValueError('Unsupported fact status')
        if item['statement'] is not None and (type(item['statement']) is not str or len(item['statement'])>4000): raise ValueError('Invalid source statement')
        sha=item['evidence_sha256']
        if sha is not None and (type(sha) is not str or re.fullmatch('[0-9a-f]{64}',sha) is None): raise ValueError('Invalid source evidence digest')
        if item['status']=='supported' and (not item['statement'] or sha is None): raise ValueError('Supported source fact requires retained evidence')
    if value['pit'] not in ('unknown','not_certified','certification_evidence_pending'): raise ValueError('Review cannot self-certify PIT')
    if type(value['reviewer']) is not str or not value['reviewer'].strip(): raise ValueError('Reviewer identity required')
    if type(value['limitations']) is not list or any(type(x) is not str for x in value['limitations']): raise ValueError('Explicit limitations required')
    missing=[name for name in FACTS if value['facts'][name]['status'] != 'supported']
    return {'schema_version':1,'review':deepcopy(value),'review_digest':digest(value),
            'contract_digest':c.contract_digest,'receipt_declaration_digest':r.declaration_digest,
            'checks_complete':not missing,'missing_inputs':missing,'real_source_ready':False,
            'independent_authentication':'pending','point_in_time':value['pit'],
            'execution_allowed':False,'scope':'source_evidence_completeness_only'}
