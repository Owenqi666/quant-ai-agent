"""Frozen source material and explicit declarations, independent of server/scripts.

Membership and hashes verify the material version, not the semantic truth of a
proposal or a declared reviewer. Only bundled, fixed source files are opened.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .author_panel_schema import AuthorModel
from .evidence import sha256
from .storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = ROOT / 'evaluation_suites/v018/semantic_cases.json'
MANIFEST = MATERIAL.with_name('manifest.json')
MATERIAL_SHA256 = '16db3c42232452d889aa5874ee22fb87d44d4456a089e42fb8d6bf7e5fcff5c5'
DIMENSIONS = ('evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution',
              'field_semantics', 'implementation_alignment')
SOURCE_FILES = {
    'alpha101_paper': ('examples/alpha101/paper.pdf',
                       '1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9', 'application/pdf'),
    'momentum_source_registry': ('docs/research/momentum_sources.json',
                                 '58f9620d7727b575d8f0574e0b113938d8d411ed658110e3f194aba424e22992', 'application/json'),
}
SOURCE_IDS = {value[0]: key for key, value in SOURCE_FILES.items()}


def parse_timestamp(value):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('Use an ISO datetime with T and an explicit timezone')
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError('Timestamp requires an explicit timezone')
        return timestamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Timestamp requires a valid ISO datetime with an explicit timezone') from exc


class DimensionDeclaration(AuthorModel):
    outcome: Literal['passed', 'failed', 'not_assessed', 'not_applicable']
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError('Assessment reasons must not be blank')
        return value


class AssessmentDimensions(AuthorModel):
    evidence_accuracy: DimensionDeclaration
    hypothesis_fidelity: DimensionDeclaration
    mechanism_attribution: DimensionDeclaration
    field_semantics: DimensionDeclaration
    implementation_alignment: DimensionDeclaration


class Annotation(AuthorModel):
    case_id: str = Field(min_length=1, max_length=120)
    reviewer: str = Field(min_length=1, max_length=120)
    confirmed_at: str = Field(min_length=20, max_length=40)
    dimensions: AssessmentDimensions

    @field_validator('case_id', 'reviewer')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Declared identity must not be blank')
        return value

    @field_validator('confirmed_at')
    @classmethod
    def aware_not_future(cls, value):
        if parse_timestamp(value) > datetime.now(timezone.utc):
            raise ValueError('A human declaration cannot be dated in the future')
        return value


class HumanDeclarations(AuthorModel):
    schema_version: Literal[1]
    material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    declaration: Literal['human_annotation']
    annotations: list[Annotation] = Field(min_length=1, max_length=7)

    @model_validator(mode='after')
    def unique_cases(self):
        if len({item.case_id for item in self.annotations}) != len(self.annotations):
            raise ValueError('Each semantic material case may be annotated once per submitted artifact')
        return self


def _safe_file(root, relative, expected):
    root = Path(root).resolve()
    path = root / relative
    # A symlink anywhere under the bundle cannot silently change source scope.
    if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
        raise ValueError('Bundled semantic source cannot be a symlink')
    if not path.resolve().is_relative_to(root) or not path.is_file() or sha256(path) != expected:
        raise ValueError('Bundled semantic material/source failed integrity verification')
    return path


def source_file(source_id, *, root=None):
    root = ROOT if root is None else root
    if source_id not in SOURCE_FILES:
        raise ValueError('Unknown bundled semantic source identity')
    relative, expected, media_type = SOURCE_FILES[source_id]
    return _safe_file(root, relative, expected), media_type


def load_material(*, root=None):
    root = ROOT if root is None else root
    root = Path(root).resolve()
    path = _safe_file(root, 'evaluation_suites/v018/semantic_cases.json', MATERIAL_SHA256)
    manifest_path = root / 'evaluation_suites/v018/manifest.json'
    if manifest_path.is_symlink() or not manifest_path.resolve().is_relative_to(root):
        raise ValueError('Semantic manifest source scope differs')
    manifest = read_json(manifest_path)
    if manifest['semantic_material_digest'] != MATERIAL_SHA256:
        raise ValueError('Frozen semantic manifest material digest differs')
    material = read_json(path)
    if len(material['cases']) != 7 or len({case['id'] for case in material['cases']}) != 7:
        raise ValueError('Fixed semantic cases differ')
    for source in material['sources']:
        source_id = SOURCE_IDS.get(source['path'])
        if source_id is None or SOURCE_FILES[source_id][1] != source['sha256']:
            raise ValueError('Material references an unregistered source')
        source_file(source_id, root=root)
    # The Momentum source is the checked registry. The raw PDF is intentionally
    # not claimed to have been reverified by this kit or required by portable builds.
    registry = read_json(root / SOURCE_FILES['momentum_source_registry'][0])
    for evidence in material['evidence']:
        if evidence['source'] == SOURCE_FILES['momentum_source_registry'][0]:
            matches = [item for item in registry['quote_anchors'] if item['id'] == evidence['id']]
            if len(matches) != 1 or any(matches[0].get(key) != evidence.get(key)
                                        for key in ('paper_id', 'document_sha256', 'page', 'quote', 'normalized_start', 'normalized_end')):
                raise ValueError('Semantic registry anchor does not match frozen material')
    if sha256(path) != MATERIAL_SHA256:
        raise ValueError('Semantic material changed while reading')
    return deepcopy(material)


def declared_status(dimensions):
    outcomes = [dimensions[name]['outcome'] for name in DIMENSIONS]
    return 'failed' if 'failed' in outcomes else 'passed' if all(x == 'passed' for x in outcomes) else 'incomplete'


def validate_annotation(material_sha256, annotation, *, root=None):
    root = ROOT if root is None else root
    normalized = Annotation.model_validate(annotation).model_dump()
    material = load_material(root=root)
    if material_sha256 != MATERIAL_SHA256:
        raise ValueError('Human declaration must name the exact frozen semantic material SHA256')
    source = next((item for item in material['cases'] if item['id'] == normalized['case_id']), None)
    if source is None:
        raise ValueError('Human declaration names an unknown semantic case')
    return {**normalized, 'material_case_digest': digest(source),
            'declared_semantic_status': declared_status(normalized['dimensions']),
            'identity_scope': 'local_declaration_not_authenticated', 'original_result_approval': False}


def validate_declarations(value, *, root=None):
    root = ROOT if root is None else root
    request = HumanDeclarations.model_validate(value).model_dump()
    records = [validate_annotation(request['material_sha256'], annotation, root=root)
               for annotation in request['annotations']]
    return {'schema_version': 1, 'material_sha256': MATERIAL_SHA256, 'declaration': 'human_annotation', 'annotations': records,
            'scope': 'Source-material semantic annotations only; no experiment or generated claim approval.',
            'identity_verified': False, 'software_verified_semantic_truth': False}
