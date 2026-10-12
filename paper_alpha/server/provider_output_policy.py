"""Opt-in, frozen host policy for one hypothesis's missing data fields.

This is an additive acceptance policy, not a new wire schema or a semantic
review. Callers bind its identity and exact instruction before model dispatch.
The legacy research-provider contract and its free-text abstentions are intact.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from .provider_contract import ProviderContractError, canonical_json

POLICY_ID = 'research_missing_fields_v1'
RESOURCE_DIRECTORY = Path(__file__).resolve().parents[2] / 'contracts' / 'research_output_policy_v1'

INSTRUCTION = '''Apply host output policy research_missing_fields_v1 to this request.
The scope is candidate generation from exactly one maintained research hypothesis.
Implement only that hypothesis, preserving its evidence and hypothesis objects.
Determine missing fields by exact set difference: the hypothesis required_fields
minus the names in capabilities.fields. Do not infer availability from prose or
substitute another field. Field names and evidence IDs are case-sensitive.
If any required field is missing, return outcome abstain and reason_code
FIELD_UNAVAILABLE. In missing_requirements include exactly one string
field:<name> for each missing field, using the exact field name after the colon.
Include every missing field and no other entries. Do not add whitespace,
backticks, explanations or duplicate entries to these machine-readable strings.
Set evidence_ids to exactly the hypothesis evidence_ids, without duplicates.
Use reason for a nonblank, freely worded explanation; its wording is not scored.
If all required fields are available, do not claim FIELD_UNAVAILABLE. A draft
still has to satisfy the original schema and all source and domain checks.
Other abstention reasons remain legal under the base wire contract, but are
outside this small policy's supported acceptance scope; do not misstate a
missing field to obtain acceptance. A field check is not proof of economic
mechanism, paper fidelity, data quality or profitable performance.
Return only the original requested JSON response; do not add policy fields.
'''


def expected_policy():
    return {'schema_version': 1, 'policy_id': POLICY_ID,
        'base_contract_id': 'research_provider_v2', 'base_wire_schema_version': 2,
        'scope': {'operation': 'generate_candidates', 'source_kind': 'research_revision',
                  'hypotheses': 1},
        'selection': 'explicit_host_evaluation_plan_only',
        'instruction_transport': 'complete_request_instruction',
        'instruction': INSTRUCTION,
        'missing_fields': 'exact_required_fields_minus_capabilities_field_names',
        'missing_field_priority': 'abstain_FIELD_UNAVAILABLE_before_candidate_generation',
        'requirements': 'unique_exact_field_colon_name_covering_all_missing_fields',
        'evidence': 'unique_exact_target_hypothesis_evidence_ids',
        'reason': 'nonblank_free_text_not_semantically_scored',
        'other_abstention_reasons': 'base_wire_legal_but_policy_unsupported',
        'semantic_fidelity': 'unverified', 'preserves_base_contract_resources': True}


def expected_files():
    policy = (canonical_json(expected_policy()) + '\n').encode('utf-8')
    manifest = {'schema_version': 1, 'policy_id': POLICY_ID,
        'files': {'policy.json': {'bytes': len(policy), 'sha256': sha256(policy).hexdigest()}},
        'instruction_sha256': sha256(INSTRUCTION.encode('utf-8')).hexdigest(),
        'base_contract_resources_unchanged': True, 'real_inference_qualified': False}
    return {'policy.json': policy, 'manifest.json': (canonical_json(manifest) + '\n').encode('utf-8')}


def verify_policy(policy_id=POLICY_ID):
    if policy_id != POLICY_ID:
        raise ProviderContractError('OUTPUT_POLICY_UNSUPPORTED', 'Unknown host output policy')
    try:
        expected = expected_files()
        valid = (not RESOURCE_DIRECTORY.is_symlink() and RESOURCE_DIRECTORY.is_dir()
                 and {path.name for path in RESOURCE_DIRECTORY.iterdir()} == set(expected)
                 and all(not (RESOURCE_DIRECTORY / name).is_symlink()
                         and (RESOURCE_DIRECTORY / name).is_file()
                         and (RESOURCE_DIRECTORY / name).read_bytes() == data
                         for name, data in expected.items()))
    except OSError:
        valid = False
    if not valid:
        raise ProviderContractError('OUTPUT_POLICY_DRIFT', 'Frozen host output policy failed exact verification')
    return expected_policy()


def policy_identity(policy_id=POLICY_ID):
    verify_policy(policy_id)
    return {'policy_id': POLICY_ID,
            'resource_sha256': sha256(expected_files()['policy.json']).hexdigest(),
            'instruction_sha256': sha256(INSTRUCTION.encode('utf-8')).hexdigest()}


def policy_instruction(policy_id=POLICY_ID):
    return verify_policy(policy_id)['instruction']


def _need(condition, message):
    if not condition:
        raise ProviderContractError('OUTPUT_POLICY_CONTEXT_UNSUPPORTED', message)


def _unambiguous_name(value, *, field_name=False):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(character.isspace() or ord(character) < 32 or ord(character) == 127
                        for character in value)
            and (not field_name or (':' not in value and '`' not in value)))


def _names(values, *, label, field_names=False, allow_empty=True):
    _need(isinstance(values, list) and (allow_empty or bool(values)), label + ' must be an explicit list')
    _need(all(_unambiguous_name(value, field_name=field_names) for value in values),
          label + ' contains an ambiguous name')
    _need(len(set(values)) == len(values), label + ' contains duplicate names')
    return set(values)


def validate_policy_context(context):
    """Derive facts solely from the already exposed, source-bound context."""
    _need(isinstance(context, dict), 'Policy context must be an object')
    _need(type(context.get('schema_version')) is int and context['schema_version'] == 2
          and context.get('operation') == 'generate_candidates'
          and context.get('source_kind') == 'research_revision',
          'This policy supports only v2 maintained-revision candidate generation')
    hypotheses = context.get('hypotheses')
    _need(isinstance(hypotheses, list) and len(hypotheses) == 1
          and isinstance(hypotheses[0], dict), 'Exactly one exposed hypothesis is required')
    hypothesis = hypotheses[0]
    _names([hypothesis.get('id')], label='Hypothesis ID', allow_empty=False)
    required = _names(hypothesis.get('required_fields'), label='Required fields', field_names=True)
    target_evidence = _names(hypothesis.get('evidence_ids'), label='Hypothesis evidence', allow_empty=False)
    evidence = context.get('evidence')
    _need(isinstance(evidence, list) and all(isinstance(item, dict) for item in evidence),
          'Exposed evidence must be an explicit list')
    exposed = _names([item.get('id') for item in evidence], label='Exposed evidence')
    _need(target_evidence <= exposed, 'Hypothesis evidence was not exposed')
    capabilities = context.get('capabilities')
    _need(isinstance(capabilities, dict), 'A capabilities catalog is required')
    fields = capabilities.get('fields')
    _need(isinstance(fields, list) and all(isinstance(item, dict) for item in fields),
          'Available fields must be an explicit list')
    available = _names([item.get('name') for item in fields], label='Available fields', field_names=True)
    return {'hypothesis_id': hypothesis['id'], 'required_fields': sorted(required),
            'available_fields': sorted(available), 'missing_fields': sorted(required - available),
            'evidence_ids': sorted(target_evidence)}


def validate_output_policy(context, output):
    """Keep syntax, mechanically supported requirements, and safe stop separate.

    ``capability_supported_stop`` only observes a missing-field abstention tied
    to the correct source evidence. It does not bless its free text or turn
    unstructured requirements into correctly formatted or factual field tokens.
    Wire/schema and full draft-domain acceptance remain the caller's checks.
    """
    verify_policy()
    facts = validate_policy_context(context)
    if hasattr(output, 'model_dump'):
        output = output.model_dump()
    missing = facts['missing_fields']
    result = {'policy_id': POLICY_ID, 'valid': False, 'diagnostics': [],
        'missing_fields': missing, 'format_valid': False, 'mechanical_valid': False,
        'capability_supported_stop': False, 'semantic_fidelity': 'unverified'}

    def problem(code, message):
        result['diagnostics'].append({'code': code, 'message': message,
                                     'semantic_fidelity': 'unverified'})

    if not isinstance(output, dict):
        problem('OUTPUT_POLICY_OUTPUT_INVALID', 'Policy validation requires a parsed response object')
        return result
    if output.get('outcome') == 'draft':
        if missing:
            problem('OUTPUT_POLICY_MISSING_FIELD_STOP_REQUIRED', 'Required unavailable fields must stop candidate generation')
        else:
            result.update(valid=True, format_valid=True, mechanical_valid=True)
        return result
    abstention = output.get('abstention')
    if output.get('outcome') != 'abstain' or not isinstance(abstention, dict):
        problem('OUTPUT_POLICY_OUTCOME_UNSUPPORTED', 'Outcome is outside this policy acceptance scope')
        return result
    if abstention.get('reason_code') != 'FIELD_UNAVAILABLE':
        problem('OUTPUT_POLICY_REASON_UNSUPPORTED', 'This abstention reason is outside this policy acceptance scope; the base wire contract is unchanged')
        return result
    requirements = abstention.get('missing_requirements')
    evidence = abstention.get('evidence_ids')
    evidence_format = (isinstance(evidence, list)
        and all(_unambiguous_name(item) for item in evidence)
        and len(evidence) == len(set(evidence)))
    evidence_matches = evidence_format and set(evidence) == set(facts['evidence_ids'])
    reason = abstention.get('reason')
    reason_present = isinstance(reason, str) and bool(reason.strip())
    requirement_format = (isinstance(requirements, list) and bool(requirements)
        and all(isinstance(item, str) and item.startswith('field:')
                and _unambiguous_name(item[6:], field_name=True) for item in requirements)
        and len(requirements) == len(set(requirements)))
    result['format_valid'] = bool(requirement_format and evidence_format and reason_present)
    result['mechanical_valid'] = bool(requirement_format and missing
        and set(requirements) == {'field:' + name for name in missing} and evidence_matches)
    result['capability_supported_stop'] = bool(missing and evidence_matches and reason_present)
    if not result['format_valid']:
        problem('OUTPUT_POLICY_FORMAT_INVALID', 'Use unique exact field:<name> entries and evidence IDs; keep explanations in the nonblank reason')
    if not result['mechanical_valid']:
        problem('OUTPUT_POLICY_REQUIREMENTS_INVALID', 'Field tokens must exactly cover required unavailable fields and cite the target hypothesis evidence')
    result['valid'] = result['format_valid'] and result['mechanical_valid']
    return result
