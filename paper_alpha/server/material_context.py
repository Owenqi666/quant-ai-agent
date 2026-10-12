"""Accepted paper material to a candidate-free, development-only Provider context."""
from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from ..author_panel_schema import AuthorModel
from ..evidence import sha256
from ..evaluation import AVAILABILITY, EXECUTION, FIELDS, MAX_FILE_BYTES, validate_config
from ..expressions import OPERATOR_SEMANTICS_VERSION, MAX_WINDOW, _FUNCTION_ARITY, _ALIASES
from ..storage import digest, json_text, read_json
from .paper_materials import PaperMaterials
from .paper_segments import MaterialError, validate_material_draft, verify_current_selection
from .provider_context import ProviderCapabilities, ProviderDevelopment
from .research_jobs_schema import DraftEvidence, DraftHypothesis
from .provider_catalog import synthetic_daily_projection


class MaterialMethodPolicy(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['daily_synthetic_v1']
    operator_semantics_version: str
    allow_field_substitution: Literal[False]
    evaluation_period: Literal['validation']

    @model_validator(mode='before')
    @classmethod
    def boolean_literal(cls, value):
        if isinstance(value, dict) and value.get('allow_field_substitution') is not False:
            raise ValueError('Field substitution requires literal false')
        return value

    @model_validator(mode='after')
    def engine(self):
        if self.operator_semantics_version != OPERATOR_SEMANTICS_VERSION or self.allow_field_substitution is not False:
            raise ValueError('Material method policy differs from the supported engine')
        return self


class RegisteredDailyMaterialMethodPolicy(AuthorModel):
    """Independent closed policy; legacy synthetic policy stays unchanged."""
    schema_version: Literal[2]
    kind: Literal['registered_daily_v2']
    allow_field_substitution: Literal[False]
    evaluation_period: Literal['validation']

    @model_validator(mode='before')
    @classmethod
    def literal_values(cls, value):
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 2 or value.get('allow_field_substitution') is not False:
            raise ValueError('Registered daily policy requires exact version and literal false')
        return value


def parse_material_method_policy(value):
    model = RegisteredDailyMaterialMethodPolicy if isinstance(value, dict) and value.get('kind') == 'registered_daily_v2' else MaterialMethodPolicy
    return model.model_validate(value).model_dump()


def default_material_method_policy():
    return {'schema_version': 1, 'kind': 'daily_synthetic_v1', 'operator_semantics_version': OPERATOR_SEMANTICS_VERSION,
            'allow_field_substitution': False, 'evaluation_period': 'validation'}


def build_material_projection(material_version):
    """Map only adopted content; an unknown mechanism remains a blocking unknown."""
    version = deepcopy(material_version)
    value = validate_material_draft(version['selection'], version['material'], accepted=True)['material']
    if version.get('actor') != 'user' or version.get('semantic_fidelity') != 'unverified':
        raise MaterialError('Candidate source requires an explicitly adopted material version', code='MATERIAL_NOT_ADOPTED')
    if value['unresolved_rules'] or any(hypothesis['unresolved_rules'] or hypothesis['claim_attribution'] == 'unresolved'
        or hypothesis['mechanism_attribution'] == 'unresolved' or any(hypothesis[name] is None for name in ('economic_mechanism', 'signal_direction', 'calculation')) for hypothesis in value['hypotheses']):
        raise MaterialError('Resolve required material rules before generating executable candidates', 422, code='MATERIAL_UNRESOLVED')
    evidence = [DraftEvidence.model_validate({key: item[key] for key in ('id', 'page', 'quote')}).model_dump() for item in value['evidence']]
    hypotheses = [DraftHypothesis.model_validate({
        'id': item['id'], 'claim': item['claim'], 'attribution': item['claim_attribution'], 'evidence_ids': item['evidence_ids'],
        'economic_mechanism': item['economic_mechanism'], 'mechanism_attribution': item['mechanism_attribution'],
        'signal_direction': item['signal_direction'], 'required_fields': [field['name'] for field in item['required_data']],
        'assumptions': item['conditions']}).model_dump() for item in value['hypotheses']]
    return {'evidence': evidence, 'hypotheses': hypotheses, 'unresolved_rules': [],
            'material_details': {'hypotheses': [{'id': item['id'], 'calculation': item['calculation'], 'required_data': item['required_data']} for item in value['hypotheses']],
                                 'limitations': value['limitations']},
            'origin': {'kind': 'accepted_material', 'material_id': version['material_id'], 'version_id': version['id'],
                       'material_digest': version['digest'], 'draft_digest': version['draft_digest'],
                       'selection_digest': version['selection']['selection_digest'],
                       'selected_pages': version['selection']['selected_pages'], 'omitted_pages': version['selection']['omitted_pages'],
                       'adoption_actor': 'user', 'semantic_fidelity': 'unverified'}}


def _dataset_source(store, dataset_id, evaluation):
    row = store._fetch('datasets', dataset_id)
    metadata_path, data_path = Path(row['metadata_path']), Path(row['data_path'])
    if not 0 < metadata_path.stat().st_size <= MAX_FILE_BYTES or not 0 < data_path.stat().st_size <= MAX_FILE_BYTES:
        raise MaterialError('Dataset files exceed supported bounds', code='SOURCE_INTEGRITY')
    metadata = json.loads(row['metadata']); data_digest = sha256(data_path)
    if read_json(metadata_path) != metadata or metadata.get('market_sha256') != data_digest or digest({'data': data_digest, 'metadata': metadata}) != row['sha256']:
        raise MaterialError('Dataset differs from its registered identity', code='SOURCE_INTEGRITY')
    if metadata.get('columns') != ['date', 'asset', *FIELDS] or metadata.get('field_availability') != AVAILABILITY or metadata.get('data_kind') != 'synthetic':
        raise MaterialError('Only the registered daily synthetic OHLCV contract is supported', 422, code='DATASET_UNSUPPORTED')
    with data_path.open(encoding='utf-8', newline='') as stream:
        header = stream.readline(4097)
    if len(header) > 4096 or next(csv.reader([header])) != metadata['columns']:
        raise MaterialError('Dataset header differs from registered fields', code='SOURCE_INTEGRITY')
    config = validate_config(evaluation, metadata['calendar_dates'])
    return {'dataset_id': dataset_id, 'dataset_sha256': row['sha256'], 'data_sha256': data_digest,
            'metadata_digest': digest(metadata), 'metadata_file_sha256': sha256(metadata_path), 'evaluation': config}


def build_material_context(store, material_id, version_id, *, dataset_id, evaluation, method_policy):
    policy = parse_material_method_policy(method_policy)
    if policy['kind'] == 'registered_daily_v2':
        from .daily_context_v2 import build_material_context as build_daily_material_context
        value = build_daily_material_context(store, material_id, version_id, dataset_id=dataset_id,
                                            evaluation=evaluation, method_policy=policy)
        value['method_policy'] = policy
        return value
    materials = PaperMaterials(store)
    version = materials.get_version(material_id, version_id)
    verify_current_selection(store, version['selection'])
    projection = build_material_projection(version)
    dataset = _dataset_source(store, dataset_id, evaluation)
    metadata = json.loads(store._fetch('datasets', dataset_id)['metadata'])
    if digest(metadata) != dataset['metadata_digest']:
        raise MaterialError('Dataset metadata changed during context creation', code='SOURCE_CHANGED')
    projection_values = synthetic_daily_projection(metadata, dataset['evaluation'])
    capabilities = ProviderCapabilities.model_validate(projection_values['capabilities']).model_dump()
    development = ProviderDevelopment.model_validate(projection_values['development']).model_dump()
    source = {'material_id': material_id, 'version_id': version_id, 'material_digest': version['digest'],
              'selection_digest': version['selection']['selection_digest'], 'paper_sha256': version['source']['paper_sha256'],
              'document_digest': version['source']['document_digest'], 'dataset': dataset, 'method_policy': policy}
    context = {'schema_version': 1, 'context_version': 'accepted-material-context-v1',
               'source_digest': digest(source), 'paper_id': version['source']['paper_id'], 'paper_sha256': version['source']['paper_sha256'],
               'dataset_id': dataset_id, 'data_sha256': dataset['data_sha256'], **projection,
               'capabilities': capabilities, 'development': development, 'method_policy': policy,
               'exposure_manifest': {'source_may_contain_formula_answers': True, 'scope': 'Implementation from explicitly adopted paper material; semantic fidelity remains unverified.',
                    'omitted': ['candidate_answers', 'raw_market_rows', 'unselected_paper_pages', 'final_test_bounds_rows_and_results', 'reviews_and_assessments', 'expected_and_oracle_labels', 'local_paths', 'research_history']},
               'semantic_fidelity': 'unverified'}
    if len(json_text(context).encode()) > 131072:
        raise MaterialError('Material context exceeds 128 KiB; no evidence was truncated', 413, code='CONTEXT_TOO_LARGE')
    if _dataset_source(store, dataset_id, evaluation) != dataset or materials.get_version(material_id, version_id) != version:
        raise MaterialError('Material/dataset changed during context creation', code='SOURCE_CHANGED')
    verify_current_selection(store, version['selection'])
    return context
