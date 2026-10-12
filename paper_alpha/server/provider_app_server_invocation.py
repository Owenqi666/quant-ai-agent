"""Fixed Codex 0.145.0 stdio launch contract; caller cannot supply RPCs/flags.

ExternalSandbox describes enforcement already applied by provider_outer_sandbox.
It is never permission to launch an unwrapped child. Only the synthetic probe
currently calls this renderer; no business provider is qualified by this module.
"""
from pathlib import Path
import re

from .provider_cli_profile import _DISABLED_FEATURES
from .provider_contract import canonical_json, digest

TRANSPORT_VERSION = 'codex_app_server_stdio_v1'
RPC_POLICY = {
    'version': TRANSPORT_VERSION, 'cli_version': '0.145.0', 'approval': 'never',
    'thread': 'ephemeral', 'sandbox': {'type': 'externalSandbox', 'networkAccess': 'restricted'},
    'methods': ['initialize', 'initialized', 'thread/start', 'turn/start'],
    'arbitrary_rpc': False, 'reconnect': False, 'automatic_retry': False,
    'isolated_state_and_home': True,
}
RPC_POLICY_DIGEST = digest(RPC_POLICY)


def render_synthetic_app_server_argv(config, *, port, nonce, write=False):
    if (config.expected_cli_version != '0.145.0' or config.model != 'gpt-5.5'
            or config.reasoning_effort != 'low' or not Path(config.executable).is_absolute()
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(nonce, str) or re.fullmatch('[a-f0-9]{32}', nonce) is None
            or type(write) is not bool):
        raise ValueError('Invalid fixed App Server synthetic launch identity')
    provider = 'paper_alpha_write_challenge' if write else 'paper_alpha_challenge'
    route = 'write-challenge-' if write else 'challenge-'
    name = 'Synthetic local write challenge' if write else 'Synthetic local challenge'
    overrides = [
        'model=' + canonical_json(config.model), 'model_reasoning_effort="low"',
        'approval_policy="never"', 'sandbox_mode="read-only"', 'web_search="disabled"',
        'allow_login_shell=false', 'project_doc_max_bytes=0',
        *('features.' + feature + '=false' for feature in _DISABLED_FEATURES),
        'features.enable_request_compression=false',
        'model_provider=' + canonical_json(provider),
        f'model_providers.{provider}.name=' + canonical_json(name),
        f'model_providers.{provider}.base_url="http://127.0.0.1:{port}/{route}{nonce}/v1"',
        f'model_providers.{provider}.wire_api="responses"',
        f'model_providers.{provider}.requires_openai_auth=false',
        f'model_providers.{provider}.request_max_retries=0',
        f'model_providers.{provider}.stream_max_retries=0',
    ]
    # These are root config flags. App Server has neither exec's -o/--json nor
    # its ignore-user-config loader flag. HOME/CODEX_HOME are isolated outside.
    argv = [config.executable]
    for override in overrides:
        argv += ['-c', override]
    return argv + ['app-server', '--stdio', '--strict-config']


def synthetic_plan(config, work, prompt, schema, *, write=False):
    return {'protocol_version': 1, 'cwd': str(work), 'model': config.model,
            'effort': config.reasoning_effort, 'prompt': prompt, 'output_schema': schema,
            'allow_file_changes': write}


# Separate pure business preparation. Nothing below launches, resolves a
# binary, reads authentication, grants dispatch, or changes the synthetic lane.
import copy
import json
import hashlib

from .provider_schema import ProviderConfig
from .provider_research_contract import (
    PROMPT_TEMPLATE as _BUSINESS_PROMPT_TEMPLATE,
    PROMPT_VERSION as _BUSINESS_PROMPT_VERSION,
    model_output_schema as _business_output_schema,
)

BUSINESS_TRANSPORT_VERSION = 'codex_app_server_business_stdio_v1'
BUSINESS_OFFICIAL_PROVIDER_ID = 'paper_alpha_business_openai'
BUSINESS_OFFICIAL_PROVIDER_NAME = 'OpenAI'
BUSINESS_OFFICIAL_BASE_URL = 'https://chatgpt.com/backend-api/codex'
BUSINESS_CHATGPT_BASE_URL = 'https://chatgpt.com/backend-api/'
BUSINESS_MAX_PLAN_BYTES = 131072
BUSINESS_MAX_JSON_DEPTH = 64
_BUSINESS_SCHEMA_JSON = canonical_json(_business_output_schema())
_BUSINESS_SCHEMA_DIGEST = digest(_business_output_schema())
_BUSINESS_REQUEST_ID_PATTERNS = {key: json.loads(_BUSINESS_SCHEMA_JSON)['properties'][key]['pattern']
    for key in ('proposal_id', 'attempt_id')}
_BUSINESS_PROMPT_KEYS = {
    'schema_version', 'contract_id', 'prompt_version', 'operation', 'proposal_id',
    'attempt_id', 'context_digest', 'candidate_limit', 'instruction',
    'output_token_target', 'context',
}
BUSINESS_RPC_POLICY = {
    'version': BUSINESS_TRANSPORT_VERSION, 'policy_version': 'codex_app_server_business_policy_v1',
    'cli_version': '0.145.0', 'model': 'gpt-5.5', 'effort': 'low',
    'contract_id': 'research_provider_v2', 'operation': 'generate_candidates',
    'approval': 'never', 'thread': 'ephemeral',
    'sandbox': {'type': 'externalSandbox', 'networkAccess': 'restricted'},
    'methods': ['initialize', 'initialized', 'thread/start', 'turn/start'],
    'allow_file_changes': False, 'arbitrary_rpc': False, 'reconnect': False,
    'automatic_retry': False, 'automatic_retry_scope': 'host_rpc',
    'native_auth_recovery_disabled': False,
    'actual_backend_model_calls_known': False,
    'isolated_state_and_home': True,
    'model_provider': BUSINESS_OFFICIAL_PROVIDER_ID,
    'model_provider_name': BUSINESS_OFFICIAL_PROVIDER_NAME,
    'official_base_url': BUSINESS_OFFICIAL_BASE_URL,
    'chatgpt_base_url': BUSINESS_CHATGPT_BASE_URL,
    'wire_api': 'responses', 'requires_openai_auth': True,
    'request_max_retries': 0, 'stream_max_retries': 0, 'supports_websockets': False,
    'mcp_configuration': 'empty_map_requested',
    # rust-v0.145.0 config/src/merge.rs recursively merges tables. The valid
    # mcp_servers={} override cannot prove that lower-layer servers vanished.
    'mcp_lower_layers_cleared': False, 'effective_tool_boundary_verified': False,
    'schema_digest': _BUSINESS_SCHEMA_DIGEST,
    'prompt_version': _BUSINESS_PROMPT_VERSION,
    'max_plan_bytes': BUSINESS_MAX_PLAN_BYTES, 'max_json_depth': BUSINESS_MAX_JSON_DEPTH,
}
BUSINESS_RPC_POLICY_DIGEST = digest(BUSINESS_RPC_POLICY)


def _business_config(config):
    if isinstance(config, ProviderConfig):
        raw = config.model_dump(mode='python')
    elif type(config) is dict:
        raw = config
    else:
        raise ValueError('BUSINESS_APP_SERVER_CONFIG_INVALID')
    for key in ('enabled', 'compatibility_verified', 'tool_boundary_verified', 'inference_verified'):
        if key in raw and type(raw[key]) is not bool:
            raise ValueError('BUSINESS_APP_SERVER_CONFIG_TYPE_INVALID')
    try:
        checked = ProviderConfig.model_validate(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('BUSINESS_APP_SERVER_CONFIG_INVALID') from exc
    if (checked.mode != 'codex_cli' or checked.expected_cli_version != '0.145.0'
            or checked.model != 'gpt-5.5' or checked.reasoning_effort != 'low'):
        raise ValueError('BUSINESS_APP_SERVER_CONFIG_UNSUPPORTED')
    return checked


def _business_path(value):
    if isinstance(value, Path):
        value = str(value)
    if (type(value) is not str or not value or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('BUSINESS_APP_SERVER_PATH_INVALID')
    path = Path(value)
    if (not path.is_absolute() or value.startswith('//') or path == Path('/')
            or str(path) != value or '..' in path.parts):
        raise ValueError('BUSINESS_APP_SERVER_PATH_INVALID')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError('BUSINESS_APP_SERVER_PATH_INVALID') from exc
    return value


def _business_json_bytes(value):
    # Validate before serializer recursion and before any filesystem identity.
    pending = [(value, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > BUSINESS_MAX_JSON_DEPTH or visited > BUSINESS_MAX_PLAN_BYTES:
            raise ValueError('BUSINESS_APP_SERVER_PLAN_LIMIT')
        if type(node) is dict:
            if any(type(key) is not str for key in node):
                raise ValueError('BUSINESS_APP_SERVER_PLAN_TYPE_INVALID')
            pending.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node)
        elif type(node) not in (str, int, float, bool, type(None)):
            raise ValueError('BUSINESS_APP_SERVER_PLAN_TYPE_INVALID')
    try:
        raw = canonical_json(value).encode('utf-8', errors='strict')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('BUSINESS_APP_SERVER_PLAN_JSON_INVALID') from exc
    if len(raw) > BUSINESS_MAX_PLAN_BYTES:
        raise ValueError('BUSINESS_APP_SERVER_PLAN_LIMIT')
    return raw


def _business_prompt(prompt, *, template=_BUSINESS_PROMPT_TEMPLATE,
                     prompt_version=_BUSINESS_PROMPT_VERSION, operation='generate_candidates'):
    if type(prompt) is not str or not prompt.startswith(template) or not prompt.endswith('\n') or '\0' in prompt:
        raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    suffix = prompt[len(template):-1]
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('Duplicate prompt key')
                result[key] = value
            return result
        payload = json.loads(suffix, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite prompt value')))
        if canonical_json(payload) != suffix:
            raise ValueError('Noncanonical prompt')
        _business_json_bytes(payload)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID') from exc
    if (type(payload) is not dict or set(payload) != _BUSINESS_PROMPT_KEYS
            or type(payload['schema_version']) is not int or payload['schema_version'] != 2
            or payload['contract_id'] != 'research_provider_v2'
            or payload['prompt_version'] != prompt_version
            or payload['operation'] != operation
            or any(type(payload[key]) is not str or re.fullmatch(_BUSINESS_REQUEST_ID_PATTERNS[key], payload[key]) is None
                   for key in ('proposal_id', 'attempt_id'))
            or type(payload['context']) is not dict or payload['context_digest'] != digest(payload['context'])
            or type(payload['candidate_limit']) is not int or not 1 <= payload['candidate_limit'] <= 3
            or type(payload['output_token_target']) is not int or not 1 <= payload['output_token_target'] <= 2000
            or type(payload['instruction']) is not str or len(payload['instruction']) > 4000):
        raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    return payload


def render_business_app_server_argv(config, *, executable):
    """Pure fixed official ChatGPT transport; not a permission/dispatch grant."""
    config = _business_config(config)
    executable = _business_path(executable)
    overrides = [
        'model="gpt-5.5"', 'model_reasoning_effort="low"',
        'approval_policy="never"', 'sandbox_mode="read-only"', 'web_search="disabled"',
        'allow_login_shell=false', 'project_doc_max_bytes=0',
        *('features.' + feature + '=false' for feature in _DISABLED_FEATURES),
        'features.enable_request_compression=false', 'mcp_servers={}',
        # Fixed 0.145.0 built-in provider keys use entry.or_insert; overrides of
        # model_providers.openai are ignored. The code-owned alias is the same
        # official target/auth route with configurable zero retry settings.
        'model_provider=' + canonical_json(BUSINESS_OFFICIAL_PROVIDER_ID),
        'chatgpt_base_url=' + canonical_json(BUSINESS_CHATGPT_BASE_URL),
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.name=' + canonical_json(BUSINESS_OFFICIAL_PROVIDER_NAME),
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.base_url=' + canonical_json(BUSINESS_OFFICIAL_BASE_URL),
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.wire_api="responses"',
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.requires_openai_auth=true',
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.request_max_retries=0',
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.stream_max_retries=0',
        f'model_providers.{BUSINESS_OFFICIAL_PROVIDER_ID}.supports_websockets=false',
    ]
    argv = [executable]
    for override in overrides:
        argv += ['-c', override]
    return argv + ['app-server', '--stdio', '--strict-config']


def business_plan(config, work, prompt, schema):
    """Validate the entire bounded original generation contract before identity IO."""
    config = _business_config(config)
    cwd = str(work) if isinstance(work, Path) else work
    plan = {'protocol_version': 1, 'cwd': cwd, 'model': config.model,
        'effort': config.reasoning_effort, 'prompt': prompt, 'output_schema': schema,
        'allow_file_changes': False}
    # Order matters: reject oversized/deep/non-JSON/invalid UTF-8 envelopes
    # before interpreting schema, payload, or any path/native/auth identity.
    _business_json_bytes(plan)
    _business_path(cwd)
    if type(schema) is not dict or canonical_json(schema) != _BUSINESS_SCHEMA_JSON:
        raise ValueError('BUSINESS_APP_SERVER_SCHEMA_INVALID')
    _business_prompt(prompt)
    return copy.deepcopy(plan)


# Controlled B2 measurement is independently versioned. The original
# synthetic renderer and its historical argv reconstruction stay unchanged.
CONTROLLED_TRANSPORT_VERSION = 'codex_app_server_controlled_stdio_v2'
CONTROLLED_RPC_POLICY = {**RPC_POLICY, 'version': CONTROLLED_TRANSPORT_VERSION,
    'context_policy_version': 'codex_app_server_isolated_context_v2',
    'bundled_skills_enabled': False, 'business_context_equivalence_verified': False}
CONTROLLED_RPC_POLICY_DIGEST = digest(CONTROLLED_RPC_POLICY)


def render_controlled_synthetic_app_server_argv(config, *, port, nonce, write=False):
    original = render_synthetic_app_server_argv(config, port=port, nonce=nonce, write=write)
    return [*original[:-3], '-c', 'skills.bundled.enabled=false', *original[-3:]]


# Business dispatch is a distinct contract, never a relabelled B1 preparation.
# Keep both older renderers above byte-for-byte semantically independent.
BUSINESS_RUNTIME_TRANSPORT_VERSION = 'codex_app_server_business_stdio_v11'
BUSINESS_RUNTIME_POLICY_VERSION = 'codex_app_server_business_policy_v11'
BUSINESS_CONTEXT_POLICY_VERSION = 'codex_app_server_business_context_v2'
def business_operation_contracts():
    """Two code-owned wire contracts; no arbitrary schema or operation admission."""
    from .provider_material_spans_contract import (
        PROMPT_TEMPLATE, PROMPT_VERSION, model_output_schema)
    result = {
        'generate_candidates': {'operation': 'generate_candidates', 'prompt_version': _BUSINESS_PROMPT_VERSION,
            'prompt_digest': hashlib.sha256(_BUSINESS_PROMPT_TEMPLATE.encode()).hexdigest(),
            'schema_digest': _BUSINESS_SCHEMA_DIGEST},
        'extract_material': {'operation': 'extract_material', 'prompt_version': PROMPT_VERSION,
            'prompt_digest': hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest(),
            'schema_digest': digest(model_output_schema())},
    }
    from .provider_grounded_contract import prompt_contract, model_output_schema as grounded_schema
    for mode in ('grounded_extract_material', 'formula_engineering_generate_candidates'):
        template, version, operation, contract = prompt_contract(mode)
        result[mode] = {'operation': operation, 'wire_contract_id': contract,
            'prompt_version': version, 'prompt_digest': hashlib.sha256(template.encode()).hexdigest(),
            'schema_digest': digest(grounded_schema(mode))}
    return result


BUSINESS_RUNTIME_RPC_POLICY = {
    **BUSINESS_RPC_POLICY,
    'version': BUSINESS_RUNTIME_TRANSPORT_VERSION,
    'policy_version': BUSINESS_RUNTIME_POLICY_VERSION,
    'context_policy_version': BUSINESS_CONTEXT_POLICY_VERSION,
    'bundled_skills_enabled': False,
    'global_context_loaded': False,
    'native_auth_access': 'metadata_checked_exact_link_only',
    'business_dispatch_authority': 'original_host_controller_only',
    'effective_tool_boundary_verified': False,
    'operation': 'bounded_material_and_candidate_operations',
    'operation_contracts': business_operation_contracts(),
    'grounding_version': 1,
    'paper_statistics': 'forbidden',
}
BUSINESS_RUNTIME_RPC_POLICY_DIGEST = digest(BUSINESS_RUNTIME_RPC_POLICY)


_HISTORICAL_RUNTIME_POLICY_JSON = '{"codex_app_server_business_stdio_v3":{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"generate_candidates","policy_version":"codex_app_server_business_policy_v3","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v3","wire_api":"responses"},"codex_app_server_business_stdio_v4":{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"generate_candidates","policy_version":"codex_app_server_business_policy_v4","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v4","wire_api":"responses"},"codex_app_server_business_stdio_v5":{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"generate_candidates","policy_version":"codex_app_server_business_policy_v5","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v5","wire_api":"responses"},"codex_app_server_business_stdio_v6":{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"generate_candidates","policy_version":"codex_app_server_business_policy_v6","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v6","wire_api":"responses"}}'

BUSINESS_GENERATION_V7_RPC_POLICY = json.loads('{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"generate_candidates","policy_version":"codex_app_server_business_policy_v7","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v7","wire_api":"responses"}')

BUSINESS_V8_RPC_POLICY = json.loads('{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"bounded_material_and_candidate_operations","operation_contracts":{"extract_material":{"prompt_digest":"c60fff9cedb73a68d193c0473d97349a0ea6891f1f639a6a247259fa14b2ef67","prompt_version":"bounded-material-span-ids-v1","schema_digest":"cddb30afdc4cb4cf2599eca338e0e7ad3973edb64b9887276e9bc851b41c9aa5"},"generate_candidates":{"prompt_digest":"2d6ece1423e8b7ff391cc362b347e2b169600f392a7f50516c912c228c22743f","prompt_version":"bounded_research_v2_hints_1","schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f"}},"policy_version":"codex_app_server_business_policy_v8","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v8","wire_api":"responses"}')


BUSINESS_V9_RPC_POLICY = json.loads('{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"grounding_version":1,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"bounded_material_and_candidate_operations","operation_contracts":{"extract_material":{"operation":"extract_material","prompt_digest":"c60fff9cedb73a68d193c0473d97349a0ea6891f1f639a6a247259fa14b2ef67","prompt_version":"bounded-material-span-ids-v1","schema_digest":"cddb30afdc4cb4cf2599eca338e0e7ad3973edb64b9887276e9bc851b41c9aa5"},"formula_engineering_generate_candidates":{"operation":"generate_candidates","prompt_digest":"22318be24f49d8ddf6c77c99bd7cc4e3d58eb96ed3b2bdcda73cad013b992cd6","prompt_version":"bounded-formula-engineering-v1","schema_digest":"97ae8f5cc2249a003b895b47da4e72f97be566a4be7c3b6b0d6af911796cb8ff","wire_contract_id":"research_formula_engineering_v1"},"generate_candidates":{"operation":"generate_candidates","prompt_digest":"2d6ece1423e8b7ff391cc362b347e2b169600f392a7f50516c912c228c22743f","prompt_version":"bounded_research_v2_hints_1","schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f"},"grounded_extract_material":{"operation":"extract_material","prompt_digest":"90e7748e855e02f448ac08c3fec01b386af9bfa674a0b58d795a2337b09b8bf8","prompt_version":"bounded-grounded-material-v1","schema_digest":"5341a333b8c730ed495c98fee3ac76fae6559a3110859432a934f6a39598a969","wire_contract_id":"research_grounded_material_v1"}},"paper_statistics":"forbidden","policy_version":"codex_app_server_business_policy_v9","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v9","wire_api":"responses"}')


BUSINESS_V10_RPC_POLICY = json.loads('{"actual_backend_model_calls_known":false,"allow_file_changes":false,"approval":"never","arbitrary_rpc":false,"automatic_retry":false,"automatic_retry_scope":"host_rpc","bundled_skills_enabled":false,"business_dispatch_authority":"original_host_controller_only","chatgpt_base_url":"https://chatgpt.com/backend-api/","cli_version":"0.145.0","context_policy_version":"codex_app_server_business_context_v2","contract_id":"research_provider_v2","effective_tool_boundary_verified":false,"effort":"low","global_context_loaded":false,"grounding_version":1,"isolated_state_and_home":true,"max_json_depth":64,"max_plan_bytes":131072,"mcp_configuration":"empty_map_requested","mcp_lower_layers_cleared":false,"methods":["initialize","initialized","thread/start","turn/start"],"model":"gpt-5.5","model_provider":"paper_alpha_business_openai","model_provider_name":"OpenAI","native_auth_access":"metadata_checked_exact_link_only","native_auth_recovery_disabled":false,"official_base_url":"https://chatgpt.com/backend-api/codex","operation":"bounded_material_and_candidate_operations","operation_contracts":{"extract_material":{"operation":"extract_material","prompt_digest":"c60fff9cedb73a68d193c0473d97349a0ea6891f1f639a6a247259fa14b2ef67","prompt_version":"bounded-material-span-ids-v1","schema_digest":"cddb30afdc4cb4cf2599eca338e0e7ad3973edb64b9887276e9bc851b41c9aa5"},"formula_engineering_generate_candidates":{"operation":"generate_candidates","prompt_digest":"22318be24f49d8ddf6c77c99bd7cc4e3d58eb96ed3b2bdcda73cad013b992cd6","prompt_version":"bounded-formula-engineering-v1","schema_digest":"97ae8f5cc2249a003b895b47da4e72f97be566a4be7c3b6b0d6af911796cb8ff","wire_contract_id":"research_formula_engineering_v1"},"generate_candidates":{"operation":"generate_candidates","prompt_digest":"2d6ece1423e8b7ff391cc362b347e2b169600f392a7f50516c912c228c22743f","prompt_version":"bounded_research_v2_hints_1","schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f"},"grounded_extract_material":{"operation":"extract_material","prompt_digest":"90e7748e855e02f448ac08c3fec01b386af9bfa674a0b58d795a2337b09b8bf8","prompt_version":"bounded-grounded-material-v1","schema_digest":"5341a333b8c730ed495c98fee3ac76fae6559a3110859432a934f6a39598a969","wire_contract_id":"research_grounded_material_v1"}},"paper_statistics":"forbidden","policy_version":"codex_app_server_business_policy_v10","prompt_version":"bounded_research_v2_hints_1","reconnect":false,"request_max_retries":0,"requires_openai_auth":true,"sandbox":{"networkAccess":"restricted","type":"externalSandbox"},"schema_digest":"b6613e5fd3f6194b9fc683b988bade4ebf728d16256246be5e05bf958ed0a69f","stream_max_retries":0,"supports_websockets":false,"thread":"ephemeral","version":"codex_app_server_business_stdio_v10","wire_api":"responses"}')

def _runtime_rpc_contract(transport, *, historical=False):
    """Closed original V3-V10 replay or current V11; never dispatch authority."""
    if type(historical) is not bool:
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_INVALID')
    if historical:
        saved = json.loads(_HISTORICAL_RUNTIME_POLICY_JSON)
        saved['codex_app_server_business_stdio_v7'] = BUSINESS_GENERATION_V7_RPC_POLICY
        saved['codex_app_server_business_stdio_v8'] = BUSINESS_V8_RPC_POLICY
        saved['codex_app_server_business_stdio_v9'] = BUSINESS_V9_RPC_POLICY
        saved['codex_app_server_business_stdio_v10'] = BUSINESS_V10_RPC_POLICY
        if type(transport) is not str or transport not in saved:
            raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_INVALID')
        policy = saved[transport]
    else:
        if transport != BUSINESS_RUNTIME_TRANSPORT_VERSION:
            raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_INVALID')
        policy = copy.deepcopy(BUSINESS_RUNTIME_RPC_POLICY)
    return policy, digest(policy)


def _business_runtime_config(config):
    checked = _business_config(config)
    if getattr(checked, 'native_transport', 'exec') != 'app_server':
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_TRANSPORT_REQUIRED')
    if type(checked.executable) is not str or _business_path(checked.executable) != checked.executable:
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_EXECUTABLE_REQUIRED')
    return checked


def render_business_runtime_app_server_argv(config, *, executable):
    checked = _business_runtime_config(config)
    original = render_business_app_server_argv(checked, executable=executable)
    return [*original[:-3], '-c', 'skills.bundled.enabled=false', *original[-3:]]


def business_runtime_plan(config, work, prompt, schema):
    # Each operation must use its exact code-owned prompt and schema. Legacy
    # business_plan stays generation-only for historical evidence replay.
    checked = _business_runtime_config(config)
    cwd = str(work) if isinstance(work, Path) else work
    plan = {'protocol_version': 1, 'cwd': cwd, 'model': checked.model,
        'effort': checked.reasoning_effort, 'prompt': prompt, 'output_schema': schema,
        'allow_file_changes': False}
    _business_json_bytes(plan)
    _business_path(cwd)
    business_runtime_operation(prompt, schema)
    return copy.deepcopy(plan)


def business_runtime_operation(prompt, schema):
    return business_operation_contracts()[business_runtime_contract(prompt, schema)]['operation']


def business_runtime_contract(prompt, schema):
    if type(schema) is not dict:
        raise ValueError('BUSINESS_APP_SERVER_SCHEMA_INVALID')
    if canonical_json(schema) == _BUSINESS_SCHEMA_JSON:
        payload = _business_prompt(prompt)
        from .provider_grounded_contract import mode_for_context
        if mode_for_context(payload['context'], 'generate_candidates') is not None:
            raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
        return 'generate_candidates'
    from .provider_grounded_contract import (prompt_contract, model_output_schema as grounded_schema,
        mode_for_context, check_frozen_contract as check_grounded)
    for mode in ('grounded_extract_material', 'formula_engineering_generate_candidates'):
        if canonical_json(schema) == canonical_json(grounded_schema(mode)):
            check_grounded(mode)
            template, version, operation, _ = prompt_contract(mode)
            payload = _business_prompt(prompt, template=template, prompt_version=version, operation=operation)
            if mode_for_context(payload['context'], operation) != mode:
                raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
            return mode
    from .provider_material_spans_contract import (
        PROMPT_TEMPLATE, PROMPT_VERSION, model_output_schema, check_frozen_contract)
    if canonical_json(schema) != canonical_json(model_output_schema()):
        raise ValueError('BUSINESS_APP_SERVER_SCHEMA_INVALID')
    check_frozen_contract()
    payload = _business_prompt(prompt, template=PROMPT_TEMPLATE,
        prompt_version=PROMPT_VERSION, operation='extract_material')
    from .paper_material_spans import validate_context_mode
    if (payload['context'].get('source_kind') != 'paper_selection'
            or not validate_context_mode(payload['context'], 'extract_material')
            or mode_for_context(payload['context'], 'extract_material') is not None):
        raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    return 'extract_material'


def business_runtime_v8_plan(config, work, prompt, schema):
    """Pure exact saved V8 replay: no current codec, resources, state or authority."""
    checked = _business_runtime_config(config)
    cwd = str(work) if isinstance(work, Path) else work
    plan = {'protocol_version': 1, 'cwd': cwd, 'model': checked.model,
        'effort': checked.reasoning_effort, 'prompt': prompt, 'output_schema': schema, 'allow_file_changes': False}
    _business_json_bytes(plan)
    _business_path(cwd)
    schemas = BUSINESS_V8_RPC_POLICY['operation_contracts']
    if digest(schema) == schemas['generate_candidates']['schema_digest']:
        payload = _business_prompt(prompt)
    elif digest(schema) == schemas['extract_material']['schema_digest']:
        from .provider_material_spans_contract import PROMPT_TEMPLATE, PROMPT_VERSION
        payload = _business_prompt(prompt, template=PROMPT_TEMPLATE,
            prompt_version=PROMPT_VERSION, operation='extract_material')
        from .paper_material_spans import validate_context_mode
        if payload['context'].get('source_kind') != 'paper_selection' or not validate_context_mode(payload['context'], 'extract_material'):
            raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    else:
        raise ValueError('BUSINESS_APP_SERVER_SCHEMA_INVALID')
    if ('grounding_policy' in payload['context']
            or payload['context'].get('method_policy', {}).get('kind') == 'daily_formula_engineering_v1'):
        raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    return copy.deepcopy(plan)


def business_runtime_v9_plan(config, work, prompt, schema):
    """Read-only reconstruction of the original V9 lanes; no dispatch permission."""
    plan = business_runtime_plan(config, work, prompt, schema)
    lane = business_runtime_contract(prompt, schema)
    if digest(schema) != BUSINESS_V9_RPC_POLICY['operation_contracts'][lane]['schema_digest']:
        raise ValueError('BUSINESS_APP_SERVER_SCHEMA_INVALID')
    from .provider_grounded_contract import prompt_contract
    if lane in {'grounded_extract_material', 'formula_engineering_generate_candidates'}:
        template, version, operation, _ = prompt_contract(lane)
        payload = _business_prompt(prompt, template=template, prompt_version=version, operation=operation)
        if payload['context'].get('grounding_policy', {}).get('operator_definitions') is not None:
            raise ValueError('BUSINESS_APP_SERVER_PROMPT_INVALID')
    return plan


def business_runtime_v10_plan(config, work, prompt, schema):
    """Read-only V10 reconstruction; never grants current dispatch authority."""
    plan = business_runtime_plan(config, work, prompt, schema)
    lane = business_runtime_contract(prompt, schema)
    if digest(schema) != BUSINESS_V10_RPC_POLICY["operation_contracts"][lane]["schema_digest"]:
        raise ValueError("BUSINESS_APP_SERVER_SCHEMA_INVALID")
    return plan
