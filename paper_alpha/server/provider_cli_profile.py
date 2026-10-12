"""Reproducible CLI policy identity; static checks never authorize inference.

Each statically supported business contract keeps its own schema and prompt
identity. Feature flags request a policy, not an effective tool boundary.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from .provider_contract import PROMPT_TEMPLATE, PROMPT_VERSION, canonical_json, digest, model_output_schema, strict_json
from .provider_schema import CONTRACT_ID, DIGEST_PATTERN, ProviderConfig

PROFILE_VERSION = 1
POLICY_VERSION = 'codex_cli_policy_v2'
RESEARCH_CONTRACT_ID = 'research_provider_v2'
SUPPORTED_CONTRACT_IDS = (CONTRACT_ID, RESEARCH_CONTRACT_ID)
MAX_PROFILE_BYTES = 512 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_NAMES = (
    'paper_alpha/server/provider_outer_sandbox.py',
    'paper_alpha/server/provider_cli_profile.py',
    'paper_alpha/server/provider_contract.py',
    'paper_alpha/server/provider_contract_registry.py',
    'paper_alpha/server/provider_schema.py',
    'paper_alpha/server/codex_cli_provider.py',
    'paper_alpha/server/provider_supervisor.py',
    'paper_alpha/server/proposal_worker.py',
    'paper_alpha/server/provider_cli_probe.py',
    'paper_alpha/server/provider_qualification.py',
    'paper_alpha/server/provider_supervision_journal.py',
)
_RESEARCH_SOURCE_NAMES = (
    # This is an explicit runtime boundary, not module discovery or a caller-
    # supplied file list. Historical subsets remain readable; a current profile
    # must fingerprint every listed file before it can pass static checks.
    'paper_alpha/author_panel_schema.py',
    'paper_alpha/contracts.py',
    'paper_alpha/evidence.py',
    'paper_alpha/evaluation.py',
    'paper_alpha/expressions.py',
    'paper_alpha/market_diagnostics.py',
    'paper_alpha/provider_evaluation_v2_schema.py',
    'paper_alpha/provider_evaluation_v2.py',
    'paper_alpha/storage.py',
    'paper_alpha/vendor/operators.py',
    'paper_alpha/workflow.py',
    'paper_alpha/server/db.py',
    'paper_alpha/server/execution_lifecycle.py',
    'paper_alpha/server/maintenance.py',
    'paper_alpha/server/material_context.py',
    'paper_alpha/server/mutations.py',
    'paper_alpha/server/paper_materials.py',
    'paper_alpha/server/paper_segments.py',
    'paper_alpha/server/paper_material_spans.py',
    'paper_alpha/server/provider_material_spans_contract.py',
    'contracts/research_material_spans_v1/model_output.schema.json',
    'contracts/research_material_spans_v1/span_catalog.schema.json',
    'contracts/research_material_spans_v1/prompt.txt',
    'contracts/research_material_spans_v1/manifest.json',
    'paper_alpha/server/proposal_requests.py',
    'paper_alpha/server/provider_canary_result.py',
    'paper_alpha/server/provider_cli_state.py',
    'paper_alpha/server/provider_context.py',
    'paper_alpha/server/provider_evaluation_runs.py',
    'paper_alpha/server/provider_host_authority.py',
    'paper_alpha/server/provider_host_schema.py',
    'paper_alpha/server/provider_experiment_api.py',
    'paper_alpha/server/provider_experiment_api_schema.py',
    'paper_alpha/server/provider_experiment_results.py',
    'paper_alpha/server/provider_experiment_source.py',
    'paper_alpha/server/provider_loop_api.py',
    'paper_alpha/server/provider_loop_api_schema.py',
    'paper_alpha/server/provider_material_bridge.py',
    'paper_alpha/server/provider_native.py',
    'paper_alpha/server/provider_output_policy.py',
    'paper_alpha/server/provider_read_boundary.py',
    'paper_alpha/server/provider_read_probe.py',
    'paper_alpha/server/provider_repair_semantics.py',
    'paper_alpha/server/provider_research_capabilities.py',
    'paper_alpha/server/provider_research_api.py',
    'paper_alpha/server/provider_research_api_schema.py',
    'paper_alpha/server/provider_research_context.py',
    'paper_alpha/server/provider_research_contract.py',
    'paper_alpha/server/provider_research_qualification.py',
    'paper_alpha/server/provider_research_review.py',
    'paper_alpha/server/provider_research_schema.py',
    'paper_alpha/server/provider_research_source_schema.py',
    'paper_alpha/server/provider_research_targets.py',
    'paper_alpha/server/provider_runtime.py',
    'paper_alpha/server/provider_runtime_status.py',
    'paper_alpha/server/provider_write_probe.py',
    'paper_alpha/server/paper_materials_schema.py',
    'paper_alpha/server/regression.py',
    'paper_alpha/server/research_assessments.py',
    'paper_alpha/server/research_draft_validation.py',
    'paper_alpha/server/research_jobs.py',
    'paper_alpha/server/research_jobs_schema.py',
    'paper_alpha/server/research_loops.py',
    'paper_alpha/server/research_loops_schema.py',
    'paper_alpha/server/research_session_context.py',
    'paper_alpha/server/research_session_execution.py',
    'paper_alpha/server/research_session_experiments.py',
    'paper_alpha/server/research_session_experiments_schema.py',
    'paper_alpha/server/research_sessions.py',
    'paper_alpha/server/research_sessions_schema.py',
    'paper_alpha/server/research_tools.py',
    'paper_alpha/server/research_tools_schema.py',
    'paper_alpha/server/runner.py',
    'paper_alpha/server/service.py',
    'contracts/research_workflow_g3b_v1/capabilities.json',
    'contracts/research_output_policy_v1/policy.json',
    'contracts/research_output_policy_v1/manifest.json',
)
_DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'apps', 'hooks', 'multi_agent',
    'plugins', 'remote_plugin', 'browser_use', 'browser_use_external',
    'computer_use', 'image_generation', 'memories', 'chronicle', 'goals',
    'skill_search', 'workspace_dependencies',
)


class ProfileDiagnostic(AuthorModel):
    code: str = Field(min_length=1, max_length=80)
    path: str = Field(max_length=4096)
    message: str = Field(min_length=1, max_length=4000)
    severity: Literal['error', 'warning']


class ExecutableIdentity(AuthorModel):
    configured: str = Field(min_length=1, max_length=4096)
    resolved: str | None = Field(max_length=4096)
    sha256: str | None = Field(pattern=DIGEST_PATTERN)
    size_bytes: int | None = Field(ge=0, le=MAX_EXECUTABLE_BYTES)
    kind: Literal['native_binary', 'script', 'unknown']
    # A hash of one executable is not full dependency-chain verification.
    chain_verified: Literal[False]


class ProfileCheckSet(AuthorModel):
    root_object: bool
    closed_objects: bool
    required_coverage: bool
    resolvable_references: bool
    no_defaults: bool
    executable_available: bool
    contract_matches_frozen: bool


class ProfileStaticChecks(AuthorModel):
    passed: bool
    checks: ProfileCheckSet
    diagnostics: list[ProfileDiagnostic] = Field(max_length=1000)

    @model_validator(mode='after')
    def consistent_result(self):
        expected = all(self.checks.model_dump().values()) and not any(
            item.severity == 'error' for item in self.diagnostics)
        if self.passed != expected:
            raise ValueError('Static result differs from its checks and diagnostics')
        return self


class CliProfile(AuthorModel):
    profile_version: Literal[1]
    # Saved v1 evidence remains readable; current profiles always use v2.
    policy_version: Literal['codex_cli_policy_v1', 'codex_cli_policy_v2']
    contract_id: Literal['codex_cli_provider_v1', 'research_provider_v2']
    config_id: str = Field(min_length=1, max_length=128)
    mode: Literal['codex_cli', 'fixture']
    config_digest: str = Field(pattern=DIGEST_PATTERN)
    executable: ExecutableIdentity
    expected_cli_version: str = Field(min_length=1, max_length=128)
    model: str | None = Field(max_length=128)
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh']
    argv_template: list[str] = Field(min_length=1, max_length=128)
    argv_template_digest: str = Field(pattern=DIGEST_PATTERN)
    wire_schema: dict
    wire_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    business_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    source_digests: dict[str, str]
    static_checks: ProfileStaticChecks
    limitations: list[str] = Field(max_length=30)
    profile_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode='before')
    @classmethod
    def literal_integer(cls, value):
        if isinstance(value, dict) and type(value.get('profile_version')) is not int:
            raise ValueError('profile_version requires an integer')
        return value

    @model_validator(mode='after')
    def bound_digests(self):
        if digest(self.argv_template) != self.argv_template_digest:
            raise ValueError('CLI argument template digest differs')
        if digest(self.wire_schema) != self.wire_schema_digest:
            raise ValueError('Wire schema digest differs')
        if self.business_schema_digest != self.wire_schema_digest:
            raise ValueError('This profile version has no wire schema transformation')
        properties = self.wire_schema.get('properties', {})
        if not isinstance(properties, dict) or not isinstance(properties.get('schema_version'), dict):
            raise ValueError('Business schema is missing its declared version')
        version = properties['schema_version'].get('const')
        expected_version = 1 if self.contract_id == CONTRACT_ID else 2
        if type(version) is not int or version != expected_version:
            raise ValueError('Business contract identity differs from its schema version')
        if (self.contract_id == RESEARCH_CONTRACT_ID and
                (not isinstance(properties.get('contract_id'), dict) or
                 properties['contract_id'].get('const') != RESEARCH_CONTRACT_ID)):
            raise ValueError('Research contract identity differs from its wire schema')
        source_names = _SOURCE_NAMES + (_RESEARCH_SOURCE_NAMES if expected_version == 2 else ())
        if (not self.source_digests or set(self.source_digests) - set(source_names)
                or any(not isinstance(value, str) or len(value) != 64 or
                       any(char not in '0123456789abcdef' for char in value)
                       for value in self.source_digests.values())):
            raise ValueError('Invalid source identity map')
        value = self.model_dump(mode='json')
        if digest({key: item for key, item in value.items() if key != 'profile_digest'}) != self.profile_digest:
            raise ValueError('Profile digest differs')
        return self


def validate_profile(value) -> dict:
    """Validate a closed bounded profile and its self/child digest consistency.

    This checks saved identity, not current executable identity or authenticity.
    Evidence verification must compare it with a newly built current profile.
    """
    if len(canonical_json(value).encode('utf-8')) > MAX_PROFILE_BYTES:
        raise ValueError('CLI profile exceeds the byte limit')
    return CliProfile.model_validate(value).model_dump(mode='json')


def _resolve_executable(configured: str) -> Path | None:
    try:
        candidate = Path(configured)
        if not candidate.is_absolute():
            found = shutil.which(configured)
            if not found:
                return None
            candidate = Path(found)
        return candidate.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None


def _read_regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Not a bounded regular file')
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(content) > limit or len(content) != before.st_size or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError('Identity file changed during reading')
    return content


def _executable_identity(config: ProviderConfig) -> tuple[dict, bool, list[dict]]:
    resolved = _resolve_executable(config.executable)
    value = {'configured': config.executable, 'resolved': str(resolved) if resolved else None,
             'sha256': None, 'size_bytes': None, 'kind': 'unknown', 'chain_verified': False}
    diagnostics = []
    try:
        if resolved is None:
            raise ValueError('Executable could not be resolved')
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.X_OK)
                    or before.st_size <= 0 or before.st_size > MAX_EXECUTABLE_BYTES):
                raise ValueError('Not a bounded executable regular file')
            fingerprint = hashlib.sha256()
            prefix = stream.read(64)
            fingerprint.update(prefix)
            total = len(prefix)
            while chunk := stream.read(65536):
                total += len(chunk)
                if total > MAX_EXECUTABLE_BYTES:
                    raise ValueError('Executable exceeds the byte limit')
                fingerprint.update(chunk)
            after = os.fstat(stream.fileno())
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or total != before.st_size):
            raise ValueError('Executable changed during hashing')
        magic = prefix[:4]
        if prefix.startswith(b'#!'):
            value['kind'] = 'script'
            diagnostics.append({'code': 'EXECUTABLE_CHAIN_UNVERIFIED', 'path': 'executable',
                'message': 'Only this script was hashed; its interpreter and downstream binary chain remain unverified.',
                'severity': 'warning'})
        elif magic in {b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                       b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                       b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                       b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'} or prefix.startswith(b'MZ'):
            value['kind'] = 'native_binary'
        value['sha256'], value['size_bytes'] = fingerprint.hexdigest(), total
        return value, True, diagnostics
    except (OSError, ValueError):
        diagnostics.append({'code': 'EXECUTABLE_UNAVAILABLE', 'path': 'executable',
            'message': 'Executable identity could not be obtained from a stable bounded executable regular file.',
            'severity': 'error'})
        return value, False, diagnostics


def build_cli_argv(config, cwd, schema, response) -> list[str]:
    """Build shell-free argv from one fixed policy and explicit host paths."""
    config = ProviderConfig.model_validate(config)
    resolved = _resolve_executable(config.executable)
    return render_cli_argv(config, cwd, schema, response, executable=str(resolved) if resolved else config.executable)


def render_cli_argv(config, cwd, schema, response, *, executable):
    """Pure fixed argv rendering; saved-work verification never resolves paths."""
    config = ProviderConfig.model_validate(config)
    result = [str(executable), 'exec', '-', '--json', '--color', 'never',
              '--output-schema', str(schema), '-o', str(response), '--cd', str(cwd),
              '--skip-git-repo-check', '--sandbox', 'read-only', '--ephemeral',
              '--ignore-user-config', '--strict-config', '--model', config.model or '',
              '-c', 'approval_policy="never"', '-c', 'web_search="disabled"']
    for feature in _DISABLED_FEATURES:
        result.extend(['-c', f'features.{feature}=false'])
    result.extend(['-c', 'allow_login_shell=false',
                   '-c', f'model_reasoning_effort={canonical_json(config.reasoning_effort)}'])
    return result


def _schema_checks(schema: dict) -> tuple[dict, list[dict]]:
    checks = {'root_object': schema.get('type') == 'object', 'closed_objects': True,
              'required_coverage': True, 'resolvable_references': True, 'no_defaults': True}
    diagnostics = []

    def fail(check, code, path, message):
        checks[check] = False
        diagnostics.append({'code': code, 'path': path, 'message': message, 'severity': 'error'})

    if not checks['root_object']:
        fail('root_object', 'SCHEMA_ROOT_TYPE', '$', 'The output schema must have an object root.')
    pending = [(schema, '$')]
    while pending:
        node, path = pending.pop()
        if not isinstance(node, dict):
            continue
        if 'default' in node:
            fail('no_defaults', 'SCHEMA_DEFAULT', path, 'Defaults are not part of this wire profile.')
        if node.get('type') == 'object' or 'properties' in node:
            if node.get('additionalProperties') is not False:
                fail('closed_objects', 'SCHEMA_OPEN_OBJECT', path, 'Object properties must be closed.')
            properties, required = node.get('properties', {}), node.get('required')
            if (not isinstance(properties, dict) or not isinstance(required, list)
                    or any(not isinstance(key, str) for key in required)
                    or len(required) != len(set(required)) or set(required) != set(properties)):
                fail('required_coverage', 'SCHEMA_REQUIRED_COVERAGE', path,
                     'Every declared object property must be required exactly once.')
        if '$ref' in node:
            reference = node['$ref']
            target = schema
            try:
                if not isinstance(reference, str) or not reference.startswith('#/'):
                    raise ValueError('Only local schema references are supported')
                for segment in reference[2:].split('/'):
                    target = target[segment.replace('~1', '/').replace('~0', '~')]
                if not isinstance(target, dict):
                    raise ValueError('Reference does not resolve to a schema object')
            except (KeyError, TypeError, ValueError):
                fail('resolvable_references', 'SCHEMA_REFERENCE', path,
                     'Every schema reference must resolve within this schema.')
        for key in ('$defs', 'properties', 'patternProperties', 'dependentSchemas'):
            if isinstance(node.get(key), dict):
                pending.extend((child, f'{path}/{key}/{name}') for name, child in node[key].items())
        for key in ('items', 'additionalProperties', 'not', 'if', 'then', 'else'):
            if isinstance(node.get(key), dict):
                pending.append((node[key], f'{path}/{key}'))
        for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if isinstance(node.get(key), list):
                pending.extend((child, f'{path}/{key}/{index}') for index, child in enumerate(node[key]))
    return checks, diagnostics


def _contract_definition(contract_id):
    if contract_id == CONTRACT_ID:
        return model_output_schema(), PROMPT_VERSION, PROMPT_TEMPLATE, _SOURCE_NAMES
    if contract_id == RESEARCH_CONTRACT_ID:
        from . import provider_research_contract as research
        return (research.model_output_schema(), research.PROMPT_VERSION,
                research.PROMPT_TEMPLATE, _SOURCE_NAMES + _RESEARCH_SOURCE_NAMES)
    raise ValueError('Unsupported business contract identity')


def contract_id_for_schema(schema: dict) -> str:
    """Match exact code-owned schemas; missing or unknown fields never fall back."""
    schema_digest = digest(schema)
    for contract_id in SUPPORTED_CONTRACT_IDS:
        if schema_digest == digest(_contract_definition(contract_id)[0]):
            return contract_id
    # A fixed compact citation transport maps into the unchanged v2 business
    # output. Its prepared schema/context remain independently bound by the
    # host authority; an arbitrary caller schema never acquires this identity.
    from .provider_material_spans_contract import model_output_schema as span_schema, check_frozen_contract
    if schema_digest == digest(span_schema()):
        check_frozen_contract()
        return RESEARCH_CONTRACT_ID
    from .provider_grounded_contract import model_output_schema as grounded_schema, check_frozen_contract as check_grounded
    for mode in ('grounded_extract_material', 'formula_engineering_generate_candidates'):
        if schema_digest == digest(grounded_schema(mode)):
            check_grounded(mode)
            return RESEARCH_CONTRACT_ID
    raise ValueError('Output schema does not match a supported business contract')


def build_profile(config, *, contract_id=CONTRACT_ID) -> dict:
    """Build a JSON identity without probes, credentials, or model invocation."""
    config = ProviderConfig.model_validate(config)
    schema, prompt_version, prompt_template, source_names = _contract_definition(contract_id)
    checks, diagnostics = _schema_checks(schema)
    executable, available, executable_diagnostics = _executable_identity(config)
    checks['executable_available'] = available
    diagnostics.extend(executable_diagnostics)
    checks['contract_matches_frozen'] = False
    try:
        frozen_root = _ROOT / 'contracts' / contract_id
        frozen = strict_json(_read_regular(frozen_root / 'model_output.schema.json', 65536),
                             max_bytes=65536)
        checks['contract_matches_frozen'] = digest(frozen) == digest(schema)
        if contract_id == RESEARCH_CONTRACT_ID:
            from .provider_material_spans_contract import check_frozen_contract
            check_frozen_contract()
            checks['contract_matches_frozen'] = checks['contract_matches_frozen'] and (
                _read_regular(frozen_root / 'prompt.txt', 65536) == prompt_template.encode('utf-8'))
            from .provider_research_capabilities import expected_capabilities
            capability = _ROOT / 'contracts/research_workflow_g3b_v1/capabilities.json'
            checks['contract_matches_frozen'] = checks['contract_matches_frozen'] and (
                _read_regular(capability, 65536) == (canonical_json(expected_capabilities()) + '\n').encode())
        if not checks['contract_matches_frozen']:
            raise ValueError('Business schema differs from the frozen contract')
    except (OSError, ValueError, TypeError):
        checks['contract_matches_frozen'] = False
        diagnostics.append({'code': 'FROZEN_CONTRACT_MISMATCH', 'path': 'wire_schema',
            'message': 'The current schema and required prompt could not be matched with their frozen business contract.',
            'severity': 'error'})
    sources = {}
    for name in source_names:
        try:
            sources[name] = hashlib.sha256(_read_regular(_ROOT / name, 2 * 1024 * 1024)).hexdigest()
        except (OSError, ValueError):
            diagnostics.append({'code': 'SOURCE_IDENTITY_UNAVAILABLE', 'path': name,
                'message': 'A required execution boundary source could not be fingerprinted.', 'severity': 'error'})
    if not config.model or not config.model.strip():
        diagnostics.append({'code': 'MODEL_UNSPECIFIED', 'path': 'model',
            'message': 'An explicit model identity is required before CLI probing.', 'severity': 'error'})
    if not config.expected_cli_version.strip():
        diagnostics.append({'code': 'CLI_VERSION_UNSPECIFIED', 'path': 'expected_cli_version',
            'message': 'An explicit expected CLI version is required.', 'severity': 'error'})
    template = build_cli_argv(config, '<cwd>', '<schema>', '<response>')
    value = {'profile_version': PROFILE_VERSION, 'policy_version': POLICY_VERSION, 'contract_id': contract_id,
        'config_id': config.config_id, 'mode': config.mode, 'config_digest': digest(config.model_dump()),
        'executable': executable, 'expected_cli_version': config.expected_cli_version,
        'model': config.model, 'reasoning_effort': config.reasoning_effort,
        'argv_template': template, 'argv_template_digest': digest(template),
        'wire_schema': schema, 'wire_schema_digest': digest(schema), 'business_schema_digest': digest(schema),
        'prompt_version': prompt_version, 'prompt_digest': hashlib.sha256(prompt_template.encode('utf-8')).hexdigest(),
        'source_digests': sources,
        'static_checks': {'passed': all(checks.values()) and not any(item['severity'] == 'error' for item in diagnostics),
                          'checks': checks, 'diagnostics': diagnostics},
        'limitations': [
            'Static schema checks do not establish real backend schema compatibility or model permission.',
            'Feature flags request a policy; they do not establish an effective tool or reading boundary.',
            'CLI 0.145.0 rejects tools.view_image; observed builtin tools remain exposed under this policy.',
            'Executable hashing does not verify its full interpreter, package, or dynamic dependency chain.',
            'Expected CLI version is a configured identity; no actual CLI version was probed.',
            'Legacy verified configuration booleans are declarations and grant no authority.',
            'Local digests check consistency and do not attest authenticity against an equal-privilege local writer.',
        ]}
    value['profile_digest'] = digest(value)
    return validate_profile(value)
