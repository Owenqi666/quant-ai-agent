"""Static business-contract dispatch, independent of provider process execution.

The legacy handler delegates to the original v1 functions so persisted input,
prompt and response bytes keep their existing meaning. Registration is code
owned; callers cannot supply schemas, imports or executable handler names.
"""
from __future__ import annotations

from .provider_contract import ProviderContractError


class EvidenceV1:
    contract_id = 'codex_cli_provider_v1'
    schema_version = 1

    @staticmethod
    def parse_request(value):
        from .provider_schema import ProposalCreate
        return ProposalCreate.model_validate(value)

    @staticmethod
    def build_context(store, request, *, enable_material_span_references=False, enable_grounded_material=False):
        from .provider_context import build_provider_context
        return build_provider_context(store, request.research_id, request.base_revision_id)

    @staticmethod
    def build_prompt(request, context, proposal_id, attempt_id):
        from .provider_contract import build_prompt
        return build_prompt(request, context, proposal_id, attempt_id)

    @staticmethod
    def output_schema(context=None):
        from .provider_contract import model_output_schema
        return model_output_schema()

    @staticmethod
    def parse_output(raw, **kwargs):
        from .provider_contract import parse_model_output
        kwargs.pop('request', None)
        kwargs.pop('context', None)
        return parse_model_output(raw, **kwargs)

    @staticmethod
    def validate_output(store, request, context, output):
        from .provider_context import validate_proposal_output
        return validate_proposal_output(store, request, context, output)


class ResearchV2:
    contract_id = 'research_provider_v2'
    schema_version = 2

    @staticmethod
    def parse_request(value):
        from .provider_research_schema import ResearchProposalCreate
        return ResearchProposalCreate.model_validate(value)

    @staticmethod
    def build_context(store, request, *, enable_material_span_references=False, enable_grounded_material=False):
        from .provider_correction_native import is_provider_correction, build_correction_context
        if is_provider_correction(request):
            if enable_material_span_references:
                raise ProviderContractError('CORRECTION_CONTEXT_MODE_INVALID', 'Correction does not enable material spans')
            return build_correction_context(store, request)
        from .provider_research_context import build_research_context
        return build_research_context(store, request, enable_material_span_references=enable_material_span_references,
                                      enable_grounded_material=enable_grounded_material)

    @staticmethod
    def build_prompt(request, context, proposal_id, attempt_id):
        from .provider_grounded_contract import mode_for_context
        if mode_for_context(context, ResearchV2.parse_request(request).operation):
            from .provider_grounded_contract import build_prompt
            return build_prompt(request, context, proposal_id, attempt_id)
        from .paper_material_spans import validate_context_mode
        request = ResearchV2.parse_request(request)
        if validate_context_mode(context, operation=request.operation):
            from .provider_material_spans_contract import build_prompt
        else:
            from .provider_research_contract import build_prompt
        return build_prompt(request, context, proposal_id, attempt_id)

    @staticmethod
    def output_schema(context=None):
        from .provider_grounded_contract import mode_for_context
        if context is not None and (mode := mode_for_context(context)):
            from .provider_grounded_contract import model_output_schema
            return model_output_schema(mode)
        from .paper_material_spans import validate_context_mode
        if context is not None and validate_context_mode(context):
            from .provider_material_spans_contract import model_output_schema
        else:
            from .provider_research_contract import model_output_schema
        return model_output_schema()

    @staticmethod
    def parse_output(raw, *, request, context=None, **kwargs):
        from .paper_material_spans import validate_context_mode
        operation = ResearchV2.parse_request(request).operation
        from .provider_grounded_contract import mode_for_context
        if context is not None and mode_for_context(context, operation):
            from .provider_grounded_contract import parse_model_output
            return parse_model_output(raw, context=context, operation=operation, **kwargs)
        if context is not None and validate_context_mode(context, operation=operation):
            from .provider_material_spans_contract import parse_model_output
            return parse_model_output(raw, context=context, operation=operation, **kwargs)
        from .provider_research_contract import parse_model_output
        return parse_model_output(raw, operation=operation, **kwargs)

    @staticmethod
    def validate_output(store, request, context, output):
        from .provider_correction_native import is_provider_correction, validate_correction_response
        if is_provider_correction(request):
            return validate_correction_response(store, request, context, output)
        from .provider_research_context import validate_research_output
        return validate_research_output(store, request, context, output)


_V1, _V2 = EvidenceV1(), ResearchV2()


def contract_handler(*, schema_version=1, contract_id=None):
    """Select only explicitly supported contracts; no discovery or fallback."""
    if type(schema_version) is int and schema_version == 1 and contract_id in (None, _V1.contract_id):
        return _V1
    if type(schema_version) is int and schema_version == 2 and contract_id == _V2.contract_id:
        return _V2
    raise ProviderContractError('CONTRACT_UNSUPPORTED', 'The requested provider business contract is not supported')


def request_handler(value):
    raw = value.model_dump() if hasattr(value, 'model_dump') else value
    if not isinstance(raw, dict):
        raise ProviderContractError('SCHEMA_MISMATCH', 'Provider request must be an object')
    return contract_handler(schema_version=raw.get('schema_version', 1), contract_id=raw.get('contract_id'))
