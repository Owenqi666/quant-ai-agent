"""Host-verified v2 sources and development-only model projections.

This module reads existing resources and performs static validation. It neither
creates a research/revision nor starts an experiment. The private source object
is hashed locally; only the explicit exposure projection enters the prompt.
"""
from __future__ import annotations

from copy import deepcopy
from pydantic import TypeAdapter

from ..storage import digest, json_text
from .material_context import (MaterialMethodPolicy, parse_material_method_policy, _dataset_source,
                               build_material_context, build_material_projection)
from .paper_materials import PaperMaterials
from .paper_segments import (build_paper_selection, validate_material_draft,
                             verify_current_selection)
from .provider_context import (ProviderContextError, _provider_source,
                               _validate_abstention, build_provider_context)
from .provider_contract import digest as wire_digest
from .provider_research_schema import ResearchProposalCreate, output_model
from .provider_research_source_schema import ResearchSource
from .provider_research_capabilities import ENABLED_OPERATIONS, verify_capabilities

SOURCE = TypeAdapter(ResearchSource)
CONTEXT_VERSION = 'research-provider-context-v2'
TARGET_OPERATIONS = frozenset({'repair_candidate', 'decide_next_action'})
TARGET_CONTEXT_VERSION = 'research-provider-g3b-context-v1'
MATERIAL_ENGINE_BUDGET = {'max_candidates': 3, 'max_attempts_per_candidate': 1,
                          'max_tool_calls': 24, 'max_seconds': 60}


def _error(message, code='SOURCE_INTEGRITY', status=409):
    return ProviderContextError(message, status, code=code)


def _version(store, source, connection):
    materials = PaperMaterials(store)
    if connection is None:
        return materials.get_version(source['material_id'], source['version_id'])
    draft = materials._draft(connection.execute('SELECT * FROM paper_materials WHERE id=?',
                                                (source['material_id'],)).fetchone())
    row = connection.execute('SELECT * FROM paper_material_versions WHERE id=?',
                             (source['version_id'],)).fetchone()
    return materials._version(row, draft, connection=connection)


def resolve_research_source(store, source_request, *, connection=None):
    """Return a verified private snapshot; its storage digest is the source ID.

    A material descriptor includes the destination and evaluation configuration,
    so a changed destination/test interval cannot reuse an existing permit. No
    descriptor is accepted as a substitute for reading actual registered bytes.
    """
    source = SOURCE.validate_python(source_request).model_dump()
    if source['kind'] == 'research_revision':
        return _provider_source(store, source['research_id'], source['base_revision_id'])
    if source['kind'] == 'paper_selection':
        selection = build_paper_selection(store, source['paper_id'], source['selected_pages'])
        return {'kind': 'paper_selection', 'paper_id': selection['paper_id'],
                'selection_digest': selection['selection_digest'], 'selection': selection}
    version = _version(store, source, connection)
    verify_current_selection(store, version['selection'])
    # Also proves that adoption was explicit and required scientific rules were
    # resolved. This cannot silently invent mechanisms to satisfy a Task.
    build_material_projection(version)
    policy = parse_material_method_policy(source['method_policy'])
    daily_source = None
    if policy['kind'] == 'registered_daily_v2':
        from .daily_research_v2 import source_for_dataset
        daily_source = source_for_dataset(store, source['dataset_id'], connection=connection)
        if digest(source['evaluation']) != daily_source['execution']['config_digest']:
            raise _error('Material evaluation differs from daily registration', 'SOURCE_INTEGRITY')
        dataset = {'dataset_id': source['dataset_id'], 'dataset_sha256': daily_source['dataset']['sha256'],
                   'evaluation': deepcopy(daily_source['contract']['evaluation']),
                   'execution': deepcopy(daily_source['execution'])}
    else:
        dataset = _dataset_source(store, source['dataset_id'], source['evaluation'])
    destination = deepcopy(source['destination'])
    if destination['kind'] == 'existing_research':
        target = _provider_source(store, destination['research_id'], destination['base_revision_id'])
        if (target['research']['paper_id'] != version['source']['paper_id']
                or target['research']['dataset_id'] != source['dataset_id']):
            raise _error('Material destination uses another paper or dataset', 'DESTINATION_MISMATCH')
        if target['revision']['task']['evaluation'] != dataset['evaluation']:
            raise _error('Existing research evaluation is frozen; use its exact configuration',
                         'DESTINATION_MISMATCH')
        if target['research']['title'] != source['title']:
            raise _error('Existing research title differs from the selected destination', 'DESTINATION_MISMATCH')
        destination['source'] = target
    result = {'kind': 'accepted_material', 'material_id': source['material_id'],
            'version_id': source['version_id'], 'material_digest': version['digest'],
            'draft_digest': version['draft_digest'], 'source_request': source,
            'material_source': deepcopy(version['source']),
            'paper_sha256': version['source']['paper_sha256'],
            'document_digest': version['source']['document_digest'],
            'selection_digest': version['selection']['selection_digest'],
            'dataset': dataset, 'method_policy': policy, 'destination': destination}
    if daily_source is not None:
        result['daily_source'] = daily_source
    return result


def _registered_daily_target_context(session, context, operation, target):
    """Separate registered-daily target projection; old v1 _base stays closed."""
    if operation not in TARGET_OPERATIONS:
        raise _error('Unsupported registered daily target operation', 'OPERATION_NOT_ENABLED')
    from .research_session_context import METRICS
    from .research_sessions_schema import RepairTarget, DecisionResult
    from .provider_field_semantics import provider_catalog_names
    provider_catalog_names(context)
    source = session['current_source']
    if (context.get('context_version') != 'registered-daily-context-v2'
            or source['kind'] != 'research_revision'
            or source['research_id'] != context['research_id']
            or source['revision_id'] != context['base_revision_id']
            or source['source_digest'] != context['source_digest']):
        raise _error('Session and registered daily source differ', 'SOURCE_CHANGED')
    remaining = {name: session['budget']['max_' + name] - session['usage'][name]
                 for name in ('provider_dispatches', 'tool_actions', 'experiments', 'failures')}
    value = {'schema_version': 2, 'session_id': session['id'], 'session_digest': session['state_digest'],
             'source_digest': source['source_digest'], 'deadline_at': session['deadline_at'],
             'remaining': remaining, 'research_context': deepcopy(context), 'operation': operation,
             'omissions': ['No final bounds, observations, freshness claims, oracle labels or private paths.'],
             'semantic_fidelity': 'unverified'}
    if operation == 'repair_candidate':
        row = RepairTarget.model_validate(target['repair_target']).model_dump()
        if (digest(row['candidate']) != row['candidate_digest'] or digest(row['diagnostics']) != row['diagnostics_digest']
                or any(d['candidate_id'] != row['candidate']['id'] for d in row['diagnostics'])
                or row['candidate']['hypothesis_id'] not in {h['id'] for h in context['hypotheses']}):
            raise _error('Daily repair target differs from its bound candidate', 'TARGET_MISMATCH')
        value['target'] = row
    else:
        rows = target['decision_results']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
            raise _error('Daily decision result inventory differs', 'TARGET_MISMATCH')
        rows = [DecisionResult.model_validate(r).model_dump() for r in rows]
        if (len({r['candidate_id'] for r in rows}) != len(rows)
                or any(not set(r['metrics']) <= METRICS for r in rows)
                or any(d['candidate_id'] != r['candidate_id'] for r in rows for d in r['diagnostics'])):
            raise _error('Daily decision result projection differs', 'TARGET_MISMATCH')
        value.update(results=rows, allowed_actions=['request_information', 'propose_experiment', 'stop'],
                     execution_policy='Suggestions require explicit host handoff; validation returns grant no execution authority.')
    return value


def build_research_context(store, request, *, connection=None, enable_material_span_references=False):
    request = ResearchProposalCreate.model_validate(request)
    if type(enable_material_span_references) is not bool:
        raise _error('Material span mode is selected only by an explicit host boolean', 'MATERIAL_SPAN_MODE_INVALID', 422)
    if request.operation not in ENABLED_OPERATIONS:
        raise _error('This research operation is not enabled',
                     'OPERATION_NOT_ENABLED', 422)
    if request.operation in TARGET_OPERATIONS:
        verify_capabilities()
    source_request = request.source.model_dump()
    source = resolve_research_source(store, source_request, connection=connection)
    if request.operation in TARGET_OPERATIONS:
        from .research_session_context import (frozen_session_snapshot, build_repair_context,
                                               build_decision_context)
        from .provider_research_targets import resolve_target
        session = frozen_session_snapshot(store, request, connection=connection)
        target = resolve_target(store, request, connection=connection)
        base = build_provider_context(store, request.source.research_id, request.source.base_revision_id, allow_registered_daily=True)
        if base.get('context_version') == 'registered-daily-context-v2':
            value = _registered_daily_target_context(session, base, request.operation, target)
        elif request.operation == 'repair_candidate':
            value = build_repair_context(session, base, target['repair_target'])
        else:
            value = build_decision_context(session, base, target['decision_results'])
            # A completed return series is not an implementation diagnostic.
            # Keep this activation narrower than the historical wire enum.
            value['allowed_actions'] = ['request_information', 'propose_experiment', 'stop']
            value['execution_policy'] = ('Completed validation results do not establish an implementation error. '
                'Suggestions carry no execution authority; a new experiment requires an explicit host handoff.')
        value['target_binding_digest'] = target['binding_digest']
    elif request.source.kind == 'paper_selection':
        value = {'paper_selection': deepcopy(source['selection']),
                 'exposure_manifest': {'source_may_contain_formula_answers': True,
                    'scope': 'Only explicitly selected physical PDF pages; citation matching is not semantic review.',
                    'omitted': ['unselected_paper_pages', 'datasets_and_market_rows',
                                'candidate_answers', 'reviews_and_assessments',
                                'expected_and_oracle_labels', 'local_paths']}}
    elif request.source.kind == 'accepted_material':
        value = build_material_context(store, request.source.material_id, request.source.version_id,
                    dataset_id=request.source.dataset_id, evaluation=request.source.evaluation,
                    method_policy=request.source.method_policy)
    else:
        value = build_provider_context(store, request.source.research_id, request.source.base_revision_id, allow_registered_daily=True)
    daily = 'daily_source' in source or 'business_revision_binding' in source
    context_version = (('research-provider-registered-daily-target-v2' if request.operation in TARGET_OPERATIONS else 'research-provider-registered-daily-v2') if daily else (TARGET_CONTEXT_VERSION if request.operation in TARGET_OPERATIONS else CONTEXT_VERSION))
    value.update(schema_version=2, context_version=context_version, operation=request.operation,
                 source_kind=request.source.kind, source_digest=digest(source),
                 semantic_fidelity='unverified')
    if enable_material_span_references and request.operation == 'extract_material':
        from .paper_material_spans import build_span_catalog, validate_context_mode
        value.update(citation_mode='material_span_ids_v1',
                     material_span_catalog=build_span_catalog(value['paper_selection']))
        validate_context_mode(value, operation=request.operation)
    # The full serialized prompt is separately bounded by the contract codec.
    if len(json_text(value).encode()) > request.budget.max_context_bytes:
        raise _error('Research projection exceeds its byte budget; nothing was truncated',
                     'CONTEXT_TOO_LARGE', 413)
    if resolve_research_source(store, source_request, connection=connection) != source:
        raise _error('Research source changed while creating the projection', 'SOURCE_CHANGED')
    return value


def validate_material_candidate_task(store, request, context, draft):
    """Reuse the existing engine checks with a candidate-free in-memory base.

    This is a validation source, not a fabricated research/revision record.
    Only explicitly adopted assertions can act as the base for paper_original
    claims. The resulting Task is not stored or submitted here.
    """
    from .research_draft_validation import validate_research_draft
    request = ResearchProposalCreate.model_validate(request)
    if request.source.kind != 'accepted_material':
        raise _error('Material candidate validation requires an adopted material', 'SOURCE_MISMATCH')
    actual = resolve_research_source(store, request.source.model_dump())
    if digest(actual) != context['source_digest']:
        raise _error('Material validation source changed', 'SOURCE_CHANGED')
    adopted = build_material_projection(PaperMaterials(store).get_version(
        request.source.material_id, request.source.version_id))
    if any(context.get(key) != adopted[key] or draft.get(key) != adopted[key]
           for key in ('evidence', 'hypotheses')):
        raise _error('Candidate assertions differ from the explicitly adopted material',
                     'HYPOTHESIS_CHANGED')
    source = {**deepcopy(actual.get('daily_source', {})), 'research': {'title': request.source.title,
                          'paper_id': actual['material_source']['paper_id'],
                          'dataset_id': request.source.dataset_id},
              'revision': {'task': {'evidence': deepcopy(context['evidence']),
                                   'hypotheses': deepcopy(context['hypotheses']),
                                   'evaluation': deepcopy(actual['dataset']['evaluation']),
                                   'budget': deepcopy(MATERIAL_ENGINE_BUDGET)}}}
    return validate_research_draft(store, source=source, draft=draft)


def validate_research_output(store, request, context, output):
    """Read-only v2 acceptance; no model-declared metrics or review verdicts."""
    from .research_draft_validation import validate_research_draft
    diagnostics, checks = [], []

    def problem(code, message):
        diagnostics.append({'code': code, 'message': message, 'semantic_fidelity': 'unverified'})

    try:
        request = ResearchProposalCreate.model_validate(request)
        output = output_model().model_validate(output)
        if request.operation not in ENABLED_OPERATIONS:
            raise _error('This operation has not passed its integration gate', 'OPERATION_NOT_ENABLED', 422)
        if (output.operation != request.operation or output.context_digest != wire_digest(context)
                or context['source_digest'] != request.expected_source_digest):
            raise _error('Output differs from its exact frozen source or operation', 'REQUEST_MISMATCH')
        from .paper_material_spans import validate_context_mode
        span_mode = validate_context_mode(context, operation=request.operation)
        if build_research_context(store, request, enable_material_span_references=span_mode) != context:
            raise _error('Research projection changed since request creation', 'SOURCE_CHANGED')
        exposed = context.get('research_context', context)
        if output.outcome == 'abstain':
            evidence = {item['id'] for item in exposed.get('evidence', [])}
            if not set(output.abstention.evidence_ids) <= evidence:
                problem('EVIDENCE_NOT_EXPOSED', 'Abstention cites evidence not sent in this request.')
            if 'capabilities' in exposed:
                _validate_abstention(output.abstention.model_dump(), exposed, problem)
        elif request.operation == 'extract_material':
            validate_material_draft(context['paper_selection'], output.material.model_dump())
        elif request.operation == 'decide_next_action':
            from .provider_research_targets import resolve_target, validate_decision_output
            target = resolve_target(store, request)
            for item in validate_decision_output(target, output.decision.model_dump()):
                problem(item['code'], item['message'])
        else:
            draft = output.draft.model_dump()
            if len(draft['candidates']) > request.candidate_limit:
                problem('CANDIDATE_LIMIT', 'Draft exceeds the frozen candidate limit.')
            # Generation implements the adopted hypothesis; changing its
            # direction, fields, mechanism or assumptions needs explicit adoption.
            if draft['evidence'] != exposed['evidence'] or draft['hypotheses'] != exposed['hypotheses']:
                problem('HYPOTHESIS_CHANGED', 'Candidate generation must preserve the exposed evidence and hypotheses exactly.')
            if request.operation == 'repair_candidate':
                from .provider_research_targets import resolve_target
                from .provider_repair_semantics import validate_repair_output
                target = resolve_target(store, request)
                for item in validate_repair_output(target['parent_draft'], draft, candidate_id=request.target.candidate_id):
                    problem(item['code'], item['message'])
            if any(candidate['origin'] == 'user_modification' for candidate in draft['candidates']):
                problem('ATTRIBUTION_INVALID', 'The model cannot claim user-modification authority for its implementation.')
            if not diagnostics:
                if request.source.kind == 'accepted_material':
                    state, error, result = validate_material_candidate_task(store, request, context, draft)
                else:
                    source = _provider_source(store, request.source.research_id, request.source.base_revision_id)
                    state, error, result = validate_research_draft(store, source=source, draft=draft)
                checks = result.get('candidate_checks', [])
                if state != 'validated':
                    problem(error['code'], error['message'])
        if build_research_context(store, request, enable_material_span_references=span_mode) != context:
            problem('SOURCE_CHANGED', 'Research source changed during output validation.')
    except (ValueError, KeyError, OSError, TypeError) as exc:
        problem(getattr(exc, 'code', 'DOMAIN_INVALID'), store._safe_error(exc))
    return {'valid': not diagnostics, 'diagnostics': diagnostics, 'candidate_checks': checks}
