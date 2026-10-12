"""Additive host capability gate; frozen base v2 resources remain historical."""
from pathlib import Path

from .provider_contract import ProviderContractError, canonical_json

EXTENSION_ID = 'research_workflow_g3b_v1'
ENABLED_OPERATIONS = frozenset({'extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action'})
RESOURCE = Path(__file__).resolve().parents[2] / 'contracts' / EXTENSION_ID / 'capabilities.json'


def expected_capabilities():
    return {'schema_version': 1, 'extension_id': EXTENSION_ID,
        'base_contract_id': 'research_provider_v2', 'base_wire_schema_version': 2,
        'base_prompt_version': 'bounded_research_v2',
        'activated_reserved_operations': ['repair_candidate', 'decide_next_action'],
        'provider_mode': 'fixture', 'real_inference_qualified': False,
        'repair_policy': 'same_hypothesis_ast_alias_and_integral_window_v1',
        'decision_target_digest': 'completed_job_frozen_result_digest',
        'session_snapshot': 'verified_event_state_before_reservation_v1',
        'loop_scope': {'source': 'research_revision', 'candidates': 1,
                       'max_repairs': 2, 'max_experiments': 1,
                       'experiment_requires_explicit_host_policy': True},
        'preserves_base_contract_resources': True}


def verify_capabilities():
    expected = (canonical_json(expected_capabilities()) + '\n').encode()
    try:
        valid = not RESOURCE.is_symlink() and RESOURCE.is_file() and RESOURCE.read_bytes() == expected
    except OSError:
        valid = False
    if not valid:
        raise ProviderContractError('CAPABILITY_CONTRACT_DRIFT', 'G3B host capability resource failed exact verification')
    return expected_capabilities()
