"""Bounded provider inputs and read-only acceptance over the existing draft rules.

This projection is deliberately separate from research job and review contexts.
It never exposes source paths, market rows, review labels or final-test results.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from pydantic import Field

from ..author_panel_schema import AuthorModel
from ..contracts import Evidence
from ..evidence import match_evidence, verify_paper
from ..evaluation import AVAILABILITY, EXECUTION, FIELDS, MAX_FILE_BYTES
from ..expressions import OPERATOR_SEMANTICS_VERSION, MAX_WINDOW, _FUNCTION_ARITY, _ALIASES
from ..storage import digest, json_text, read_json
from .research_jobs_schema import DraftEvidence, DraftHypothesis
from .provider_contract import digest as wire_digest
from .service import ServiceError
from .provider_catalog import synthetic_daily_projection

if TYPE_CHECKING:
    from .provider_schema import ProposalCreate, ProposalModelOutput

CONTEXT_VERSION = 'daily-proposal-context-v1'
MAX_CONTEXT_BYTES = 128 * 1024


class ProviderContextError(ServiceError):
    def __init__(self, message, status=409, *, code):
        super().__init__(message, status)
        self.code = code
        self.retryable = False


class ProviderField(AuthorModel):
    name: str
    kind: Literal['raw', 'derived']
    available_at: str
    unit: str | None
    definition: str


class ProviderOperator(AuthorModel):
    name: str
    arity: int


class ProviderCapabilities(AuthorModel):
    fields: list[ProviderField] = Field(max_length=30)
    operators: list[ProviderOperator] = Field(max_length=100)
    documented_aliases: dict[str, str]
    operator_semantics_version: str
    maximum_window: int
    semantics: str
    data_kind: Literal['synthetic']


class ProviderRange(AuthorModel):
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class ProviderDevelopment(AuthorModel):
    train: ProviderRange
    validation: ProviderRange
    min_assets: int
    evaluation_period: Literal['validation']
    execution: dict[str, str | float]
    missing_policy: str
    final_test_policy: Literal['Final-test data and evaluation are unavailable to the provider.']


class ProviderExposure(AuthorModel):
    evidence_ids: list[str] = Field(max_length=100)
    hypothesis_ids: list[str] = Field(max_length=100)
    source_may_contain_formula_answers: Literal[True]
    scope: Literal['Implementation from already maintained paper assertions; not independent alpha discovery.']
    omitted: list[str] = Field(max_length=30)


class ProviderContext(AuthorModel):
    schema_version: Literal[1]
    context_version: Literal['daily-proposal-context-v1']
    research_id: str
    base_revision_id: str
    paper_id: str
    dataset_id: str
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    paper_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    data_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence: list[DraftEvidence] = Field(min_length=1, max_length=100)
    hypotheses: list[DraftHypothesis] = Field(max_length=100)
    capabilities: ProviderCapabilities
    development: ProviderDevelopment
    exposure_manifest: ProviderExposure
    semantic_fidelity: Literal['unverified']


def _provider_source(store, research_id, base_revision_id):
    """Private source snapshot: never include this full object in a prompt."""
    from .research_jobs import ResearchJobs
    research = store._fetch('researches', research_id)
    dataset = store._fetch('datasets', research['dataset_id'])
    if json.loads(dataset['metadata']).get('data_kind') == 'daily_development_snapshot':
        from contextlib import closing
        from .db import connect
        from .daily_context_v2 import resolve_revision_source
        from .daily_revision_bindings import assert_revision, assert_history
        from .research_guard import assert_revision_safe
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            raw_revision = store._one(connection, 'revisions', base_revision_id)
            raw_dataset = store._one(connection, 'datasets', research['dataset_id'])
            assert_history(connection)
            assert_revision_safe(connection, raw_dataset, raw_revision)
            binding = assert_revision(connection, raw_revision, dataset=raw_dataset)
            source = resolve_revision_source(store, research_id, base_revision_id, connection)
            return {**source, 'business_revision_binding': binding}
    source = ResearchJobs(store)._source(research_id, base_revision_id)
    if store._fetch('researches', research_id)['latest_revision_id'] != base_revision_id:
        raise ProviderContextError('The base revision is no longer current.', code='BASE_REVISION_CHANGED')
    dataset = store._fetch('datasets', source['research']['dataset_id'])
    metadata_path = Path(dataset['metadata_path'])
    if not 0 < metadata_path.stat().st_size <= MAX_FILE_BYTES:
        raise ProviderContextError('Dataset metadata exceeds the supported bound.', code='SOURCE_INTEGRITY')
    metadata = json.loads(dataset['metadata'])
    if (read_json(metadata_path) != metadata
            or metadata.get('market_sha256') != source['data_file_sha256']
            or digest({'data': source['data_file_sha256'], 'metadata': metadata}) != dataset['sha256']):
        raise ProviderContextError('Dataset bytes differ from their registered identity.', code='SOURCE_INTEGRITY')
    if (metadata.get('columns') != ['date', 'asset', *FIELDS]
            or metadata.get('field_availability') != AVAILABILITY
            or metadata.get('data_kind') != 'synthetic'):
        raise ProviderContextError('Only the existing daily synthetic OHLCV contract is supported.', code='DATASET_UNSUPPORTED')
    with Path(dataset['data_path']).open(encoding='utf-8', newline='') as stream:
        header = stream.readline(4097)
    if len(header) > 4096 or next(csv.reader([header])) != metadata['columns']:
        raise ProviderContextError('The actual data columns do not match the registered catalog.', code='SOURCE_INTEGRITY')
    return source


def build_provider_context(store, research_id: str, base_revision_id: str, *, allow_registered_daily=False) -> dict:
    """Read verified evidence and an allowlisted development-only capability view.

    Hashing validates source identity; no market observations are included or
    evaluated. Evidence is never truncated. Oversized projections are rejected.
    """
    if type(allow_registered_daily) is not bool:
        raise ProviderContextError('Context version dispatch requires a host boolean.', 422, code='CONTEXT_VERSION_INVALID')
    source = _provider_source(store, research_id, base_revision_id)
    if source['revision']['task'].get('schema_version') == 2:
        if not allow_registered_daily:
            raise ProviderContextError('Registered daily context requires Research v2 dispatch.', 422, code='DATASET_UNSUPPORTED')
        from .daily_context_v2 import build_revision_context
        context = build_revision_context(store, research_id, base_revision_id)
        context['source_digest'] = digest(source)
        if _provider_source(store, research_id, base_revision_id) != source:
            raise ProviderContextError('Registered daily source changed.', code='SOURCE_CHANGED')
        return context
    base = source['revision']['task']
    paper_row = store._fetch('papers', source['research']['paper_id'])
    paper = json.loads(paper_row['document'])
    verify_paper(paper, paper_row['pdf_path'])
    evidence = deepcopy(base['evidence'])
    for item in evidence:
        DraftEvidence.model_validate(item)
        match_evidence(Evidence(**item), paper)
    evidence_ids = {item['id'] for item in evidence}
    if len(evidence_ids) != len(evidence):
        raise ProviderContextError('Duplicate evidence identities.', code='SOURCE_INTEGRITY')
    hypotheses = [deepcopy(item) for item in base['hypotheses'] if item['attribution'] == 'paper_original']
    for hypothesis in hypotheses:
        DraftHypothesis.model_validate(hypothesis)
        if not set(hypothesis['evidence_ids']) <= evidence_ids:
            raise ProviderContextError('A maintained hypothesis has unknown evidence.', code='SOURCE_INTEGRITY')
    metadata = json.loads(store._fetch('datasets', source['research']['dataset_id'])['metadata'])
    if digest(metadata) != source['dataset_metadata_digest']:
        raise ProviderContextError('Dataset metadata changed during context creation.', code='SOURCE_CHANGED')
    projection = synthetic_daily_projection(metadata, base['evaluation'])
    value = {
        'schema_version': 1, 'context_version': CONTEXT_VERSION,
        'research_id': research_id, 'base_revision_id': base_revision_id,
        'paper_id': source['research']['paper_id'], 'dataset_id': source['research']['dataset_id'],
        'source_digest': digest(source), 'paper_sha256': source['paper_sha256'],
        'data_sha256': source['data_file_sha256'], 'evidence': evidence, 'hypotheses': hypotheses,
        **projection,
        'exposure_manifest': {'evidence_ids': [item['id'] for item in evidence],
            'hypothesis_ids': [item['id'] for item in hypotheses],
            'source_may_contain_formula_answers': True,
            'scope': 'Implementation from already maintained paper assertions; not independent alpha discovery.',
            'omitted': ['base_candidates', 'non_original_hypotheses', 'raw_market_rows', 'full_paper',
                        'final_test_bounds_rows_and_results', 'reviews_and_assessments', 'expected_and_oracle_labels',
                        'local_paths', 'research_history']},
        'semantic_fidelity': 'unverified',
    }
    clean = ProviderContext.model_validate(value).model_dump()
    if len(json_text(clean).encode('utf-8')) > MAX_CONTEXT_BYTES:
        raise ProviderContextError('Provider context exceeds 128 KiB; no evidence was truncated.', 413, code='CONTEXT_TOO_LARGE')
    if _provider_source(store, research_id, base_revision_id) != source:
        raise ProviderContextError('Source changed while building provider context.', code='SOURCE_CHANGED')
    return clean


def validate_proposal_output(store, request: ProposalCreate, context: dict,
                             output: ProposalModelOutput) -> dict:
    """Check frozen context, abstentions and drafts without creating jobs/runs.

    Proposal/attempt ID ownership is checked by the caller's attempt ledger.
    This function verifies the context identity and all domain references.
    """
    from .provider_schema import ProposalCreate, ProposalModelOutput
    from .research_draft_validation import validate_research_draft
    request = ProposalCreate.model_validate(request)
    output = ProposalModelOutput.model_validate(output)
    diagnostics, checks = [], []

    def problem(code, message):
        diagnostics.append({'code': code, 'message': message, 'semantic_fidelity': 'unverified'})

    try:
        if request.purpose != 'generate_from_evidence':
            raise ProviderContextError('Target-bound repair is not implemented in provider v1.', 422, code='NOT_IMPLEMENTED')
        context = ProviderContext.model_validate(context).model_dump()
        if (context['research_id'] != request.research_id
                or context['base_revision_id'] != request.base_revision_id
                or context['source_digest'] != request.expected_source_digest
                or output.context_digest != wire_digest(context)):
            raise ProviderContextError('Output does not match the frozen request context.', code='REQUEST_MISMATCH')
        current = build_provider_context(store, request.research_id, request.base_revision_id)
        if current != context:
            raise ProviderContextError('Provider context has changed since it was frozen.', code='SOURCE_CHANGED')
        evidence = {item['id']: item for item in context['evidence']}
        if output.outcome == 'abstain':
            if not set(output.abstention.evidence_ids) <= set(evidence):
                problem('EVIDENCE_NOT_EXPOSED', 'Abstention refers to evidence not sent in this request.')
            _validate_abstention(output.abstention.model_dump(), context, problem)
        else:
            draft = output.draft.model_dump()
            if len(draft['candidates']) > request.candidate_limit:
                problem('CANDIDATE_LIMIT', 'Draft exceeds the frozen candidate limit.')
            for item in draft['evidence']:
                if evidence.get(item['id']) != item:
                    problem('EVIDENCE_NOT_EXPOSED', 'Draft evidence must exactly match evidence sent in this request.')
            if (any(item['attribution'] == 'user_modification' or item['mechanism_attribution'] == 'user_modification'
                    for item in draft['hypotheses'])
                    or any(item['origin'] == 'user_modification' for item in draft['candidates'])):
                problem('ATTRIBUTION_INVALID', 'Provider v1 has no structured user-modification authority; model changes must be model_conjecture.')
            if not diagnostics:
                source = _provider_source(store, request.research_id, request.base_revision_id)
                state, error, result = validate_research_draft(store, source=source, draft=draft)
                checks = result.get('candidate_checks', [])
                if state != 'validated':
                    problem(error['code'], error['message'])
        if build_provider_context(store, request.research_id, request.base_revision_id) != context:
            problem('SOURCE_CHANGED', 'Provider source changed during output validation.')
    except (ValueError, KeyError, OSError) as exc:
        problem(getattr(exc, 'code', 'DOMAIN_INVALID'), store._safe_error(exc))
    return {'valid': not diagnostics, 'diagnostics': diagnostics, 'candidate_checks': checks}


def _validate_abstention(abstention, context, problem):
    """Check catalog contradictions only, never score semantic sufficiency."""
    from .provider_field_semantics import provider_catalog_names
    fields, operators = provider_catalog_names(context)
    for requirement in abstention['missing_requirements']:
        category, separator, name = requirement.strip().partition(':')
        category, name = category.strip(), name.strip()
        if not separator:
            if category in fields or category in operators:
                problem('ABSTENTION_CONTRADICTION', 'The stated missing requirement is available in the sent catalog.')
        elif ((category == 'field' and name in fields)
              or (category == 'operator' and name in operators)
              or (category == 'configuration' and name in context['development'])):
            problem('ABSTENTION_CONTRADICTION', 'The stated missing requirement is available in the sent catalog.')
