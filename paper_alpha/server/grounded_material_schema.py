"""Additive model-declared support bindings; exact citations are not semantic review."""
from __future__ import annotations

import json
import re
from typing import Literal
from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from .paper_materials_schema import (ID, MaterialDraft, AcceptedMaterialHypothesis)


def validate_variable_data(material):
    """Reject registered function notation only under an explicit new task policy.

    Saved Grounded/Accepted model classes remain unchanged. Logical data names
    need not be identifiers in the executable expression language.
    """
    from ..expressions import _ALIASES, _FUNCTION_ARITY
    value = material.model_dump() if hasattr(material, 'model_dump') else material
    for hypothesis in value['hypotheses']:
        for data in hypothesis['required_data']:
            match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)', data['name'].strip(), re.S)
            if match:
                name = match[1].lower()
                if _ALIASES.get(name, name) in _FUNCTION_ARITY:
                    raise ValueError('Registered operator ' + data['name']
                                     + ' belongs in calculation support, not required_data')


class MaterialSupportBinding(AuthorModel):
    hypothesis_id: str = Field(pattern=ID)
    component: str = Field(min_length=1, max_length=116)
    evidence_ids: list[str] = Field(max_length=30)
    status: Literal['direct', 'inference', 'unresolved']
    explanation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def bounded(self):
        if not self.explanation.strip() or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Support explanation and unique evidence IDs are required')
        if (self.status == 'unresolved') != (not self.evidence_ids):
            raise ValueError('Direct/inferred support needs evidence; unresolved support has no evidence IDs')
        return self


class PaperStatistic(AuthorModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    attribution: Literal['paper_original']

    @model_validator(mode='after')
    def bounded(self):
        if not self.label.strip() or not self.value.strip() or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Paper statistic requires bounded text and unique source evidence')
        return self


class GroundedMaterialDraft(MaterialDraft):
    grounding_version: Literal[1]
    support_bindings: list[MaterialSupportBinding] = Field(min_length=1, max_length=512)
    paper_statistics: list[PaperStatistic] = Field(max_length=30)

    @model_validator(mode='before')
    @classmethod
    def grounding_literal(cls, value):
        if isinstance(value, dict) and type(value.get('grounding_version')) is not int:
            raise ValueError('grounding_version requires an integer')
        return value

    @model_validator(mode='after')
    def component_support(self):
        evidence = {item.id for item in self.evidence}
        hypotheses = {item.id: item for item in self.hypotheses}
        components, required = {}, set()
        accepted = isinstance(self, AcceptedGroundedMaterialDraft)
        from .research_jobs_schema import ENGINEERING_ONLY_ASSUMPTION
        for hypothesis in self.hypotheses:
            values = {'claim': hypothesis.claim, 'economic_mechanism': hypothesis.economic_mechanism,
                      'signal_direction': hypothesis.signal_direction, 'calculation': hypothesis.calculation}
            values.update({'data:' + item.name: item.semantics for item in hypothesis.required_data})
            for index, condition in enumerate(hypothesis.conditions):
                if accepted and (condition.startswith('Project configuration:') or condition == ENGINEERING_ONLY_ASSUMPTION):
                    continue
                values['condition:' + str(index)] = condition
            for name, value in values.items():
                key = hypothesis.id, name
                components[key] = value
                if value is not None:
                    required.add(key)
        seen = set()
        for binding in self.support_bindings:
            key = binding.hypothesis_id, binding.component
            if key in seen or key not in components:
                raise ValueError('Support bindings must name each existing component at most once')
            seen.add(key)
            hypothesis = hypotheses[binding.hypothesis_id]
            if not set(binding.evidence_ids) <= evidence or not set(binding.evidence_ids) <= set(hypothesis.evidence_ids):
                raise ValueError('Support must cite evidence exposed by its hypothesis')
            if (components[key] is None) != (binding.status == 'unresolved'):
                raise ValueError('Unknown components remain null/unresolved; existing content requires support')
            if binding.component.startswith('data:'):
                data = next(item for item in hypothesis.required_data if 'data:' + item.name == binding.component)
                if binding.evidence_ids != data.evidence_ids:
                    raise ValueError('Data support evidence must equal its declared semantic evidence IDs')
            if binding.status == 'inference' and binding.component in {'claim', 'economic_mechanism'}:
                attribution = hypothesis.claim_attribution if binding.component == 'claim' else hypothesis.mechanism_attribution
                if attribution not in {'model_conjecture', 'user_modification'}:
                    raise ValueError('An inferred assertion cannot claim original-paper attribution')
        if not required <= seen:
            raise ValueError('Every nonnull hypothesis component requires a support binding')
        for statistic in self.paper_statistics:
            if not set(statistic.evidence_ids) <= evidence:
                raise ValueError('Paper statistic references unavailable evidence')
        return self


class AcceptedGroundedMaterialHypothesis(AcceptedMaterialHypothesis):
    """An adopted host disposition record can exceed one ordinary rule's limit."""
    @model_validator(mode='after')
    def shape(self):
        # Keep the source hypothesis checks. Only a canonical host-created
        # disposition record receives the already bounded material's room.
        if not self.claim.strip():
            raise ValueError('Claim may not be blank')
        if self.economic_mechanism is None and self.mechanism_attribution != 'unresolved':
            raise ValueError('Unknown mechanism requires unresolved attribution')
        for value in (self.economic_mechanism, self.signal_direction, self.calculation):
            if value is not None and not value.strip():
                raise ValueError('Use null for unknown content, not blank text')
        for value in self.unresolved_rules:
            if not value.strip() or len(value) > 2000:
                raise ValueError('Material rules must be bounded nonblank text')
        for value in self.conditions:
            if not value.strip():
                raise ValueError('Material rules must be bounded nonblank text')
            if len(value) > 2000:
                from .material_context import DISPOSITIONS_PREFIX, engineering_disposition_condition
                if not value.startswith(DISPOSITIONS_PREFIX):
                    raise ValueError('Material rules must be bounded nonblank text')
                if engineering_disposition_condition(json.loads(value[len(DISPOSITIONS_PREFIX):])) != value:
                    raise ValueError('Long disposition conditions require the exact canonical host record')
        if len({item.name for item in self.required_data}) != len(self.required_data):
            raise ValueError('Data semantic names must be unique')
        return self


class AcceptedGroundedMaterialDraft(GroundedMaterialDraft):
    hypotheses: list[AcceptedGroundedMaterialHypothesis] = Field(min_length=1, max_length=10)


def material_model(value, *, accepted=False):
    """Return a closed model class from saved data; no default upgrades old material."""
    from .paper_materials_schema import MaterialDraft, AcceptedMaterialDraft
    raw = value.model_dump() if hasattr(value, 'model_dump') else value
    if not isinstance(raw, dict):
        raise ValueError('Material must be an object')
    if 'grounding_version' not in raw:
        return AcceptedMaterialDraft if accepted else MaterialDraft
    if type(raw['grounding_version']) is not int or raw['grounding_version'] != 1:
        raise ValueError('Unsupported grounding version')
    return AcceptedGroundedMaterialDraft if accepted else GroundedMaterialDraft
