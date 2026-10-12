"""Frozen host provider configuration; no inference or CLI probes on reads."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
from typing import Literal

from pydantic import Field

from ..author_panel_schema import AuthorModel
from .provider_contract import digest as wire_digest, strict_json
from .provider_schema import ProviderConfig
from .service import ServiceError

CONFIG_ENV = 'PAPER_ALPHA_PROVIDER_CONFIG'
FIXTURE_ENV = 'PAPER_ALPHA_ALLOW_FIXTURE_PROVIDER'
REAL_ENV = 'PAPER_ALPHA_ALLOW_REAL_PROVIDER'
CONFIG_DIGEST_ENV = 'PAPER_ALPHA_PROVIDER_CONFIG_DIGEST'
MAX_CONFIG_BYTES = 64 * 1024


class ProviderRuntimeError(ServiceError):
    def __init__(self, message, status=503, *, code):
        super().__init__(message, status)
        self.code = code
        self.retryable = False


class ProviderHostConfig(AuthorModel):
    schema_version: Literal[1]
    providers: list[ProviderConfig] = Field(max_length=16)


class ProviderRegistry:
    """Read-only snapshots of trusted host configs, never browser configuration.

    Host permission allows dispatch; the native adapter still verifies the
    execution boundary and authentication on every attempt. Status reads do
    not establish inference compatibility or trust configuration assertions.
    """
    def __init__(self, configs=None, *, allow_fixture=False, allow_real=False):
        if (type(allow_fixture) is not bool or type(allow_real) is not bool
                or (configs is not None and not isinstance(configs, dict))):
            raise ProviderRuntimeError('Invalid provider registry settings.', 422, code='CONFIGURATION_INVALID')
        if len(configs or {}) > 16:
            raise ProviderRuntimeError('Provider registry exceeds sixteen configurations.', 422, code='CONFIGURATION_INVALID')
        self.allow_fixture = allow_fixture
        self.allow_real = allow_real
        self._configs = {key: ProviderConfig.model_validate(value).model_copy(deep=True)
                         for key, value in (configs or {}).items()}
        if any(key != value.config_id for key, value in self._configs.items()):
            raise ProviderRuntimeError('Provider configuration key differs from config_id.', 422, code='CONFIGURATION_INVALID')
        # Resolve a command alias once, before API requests freeze its config.
        # No executable is launched and no workspace is created here.
        for config in self._configs.values():
            if config.mode == 'codex_cli':
                located = shutil.which(str(Path(config.executable).expanduser()))
                if located is not None:
                    config.executable = str(Path(located).resolve())

    @property
    def configs(self) -> dict[str, ProviderConfig]:
        return {key: value.model_copy(deep=True) for key, value in self._configs.items()}

    def ensure_dispatchable(self, config_id: str) -> ProviderConfig:
        config = self._configs.get(config_id)
        if config is None or not config.enabled:
            raise ProviderRuntimeError('Provider is not configured and enabled.', code='PROVIDER_DISABLED')
        if config.mode == 'codex_cli':
            if not self.allow_real:
                raise ProviderRuntimeError('Real provider dispatch requires explicit host authorization.', code='REAL_DISPATCH_DISABLED')
            if (config.model != 'gpt-5.5' or config.reasoning_effort != 'low'
                    or config.expected_cli_version != '0.145.0'):
                raise ProviderRuntimeError('The native adapter supports only the pinned CLI/model profile.', code='NATIVE_PROFILE_UNSUPPORTED')
            executable = Path(config.executable)
            if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                raise ProviderRuntimeError('The configured native executable is unavailable.', code='CLI_UNAVAILABLE')
            return config.model_copy(deep=True)
        if not self.allow_fixture:
            raise ProviderRuntimeError('Fixture dispatch requires the explicit host development flag.', code='FIXTURE_DISABLED')
        if not config.model or not config.model.strip():
            raise ProviderRuntimeError('Fixture model identity must be explicitly configured.', code='CONFIGURATION_INVALID')
        executable = Path(config.executable)
        installed = shutil.which('codex')
        if (not executable.is_absolute() or executable.name == 'codex'
                or (installed and executable.resolve() == Path(installed).resolve())):
            raise ProviderRuntimeError('Fixture mode requires an explicit test executable, not the installed Codex CLI.', code='FIXTURE_EXECUTABLE_INVALID')
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ProviderRuntimeError('The configured fixture executable is unavailable.', code='CLI_UNAVAILABLE')
        return config.model_copy(deep=True)

    def research_authority(self, store):
        from .provider_authority_router import ResearchAuthorityRouter
        return ResearchAuthorityRouter(store, self)

    def proposal_requests(self, store):
        from .proposal_requests import ProposalRequests
        return ProposalRequests(store, configs=self.configs, research_authority=self.research_authority(store))

    def create_provider(self, config_id: str, *, contract_id=None, research_authority=None):
        """A new adapter per attempt; no proposal or subprocess on creation."""
        config = self.ensure_dispatchable(config_id)
        if config.mode == 'codex_cli':
            from .provider_native import NativeResearchProvider
            from .provider_schema import CONTRACT_ID
            if contract_id is None and research_authority is None:
                return NativeResearchProvider(config, host_authorized=True)
            return NativeResearchProvider(config, host_authorized=True, contract_id=contract_id or CONTRACT_ID,
                                          research_authorization=research_authority)
        from .codex_cli_provider import CodexCliProvider
        return CodexCliProvider(config)

    def dispatch_config_digests(self) -> dict[str, str]:
        result = {}
        for key in self._configs:
            try:
                result[key] = wire_digest(self.ensure_dispatchable(key).model_dump())
            except ProviderRuntimeError:
                continue
        return result

    def snapshot(self) -> dict:
        """Trusted local material for launcher children; never an HTTP response."""
        return {'schema_version': 1, 'providers': [self._configs[key].model_dump()
                                                  for key in sorted(self._configs)]}

    def startup_digest(self) -> str:
        """Freeze both config identity and host authorization for child services."""
        return wire_digest({'config': self.snapshot(), 'authorization': {
            'allow_fixture': self.allow_fixture, 'allow_real': self.allow_real}})

    def public_status(self) -> dict:
        providers = []
        for key, config in sorted(self._configs.items()):
            error = None
            try:
                self.ensure_dispatchable(key)
            except ProviderRuntimeError as exc:
                error = {'code': exc.code, 'message': str(exc), 'retryable': False}
            providers.append({
                'config_id': key, 'mode': config.mode, 'enabled': config.enabled,
                'dispatchable': error is None, 'model': config.model,
                'reasoning_effort': config.reasoning_effort, 'expected_cli_version': config.expected_cli_version,
                # File booleans are declarations, not verified probe evidence.
                'compatibility_verified': False, 'tool_boundary_verified': False,
                'inference_verified': False, 'readiness': None, 'error': error,
            })
        return {'schema_version': 1, 'allow_fixture': self.allow_fixture, 'allow_real': self.allow_real,
                'real_dispatch_enabled': any(item['mode'] == 'codex_cli' and item['dispatchable'] for item in providers),
                'providers': providers}


def load_provider_registry(path=None, *, allow_fixture=False, allow_real=False) -> ProviderRegistry:
    """Read one closed, bounded host JSON document; do not reload on API GET.

    An explicit registry constructor never reads environment settings. The host
    loader reads host permission flags; an explicit path selects the config
    file instead of CONFIG_ENV. Child startup digests bind the flags too.
    """
    from_environment = path is None
    if type(allow_fixture) is not bool or type(allow_real) is not bool:
        raise ProviderRuntimeError('Provider authorization flags must be booleans.', 422, code='CONFIGURATION_INVALID')
    fixture_flag, real_flag = os.environ.get(FIXTURE_ENV, '0'), os.environ.get(REAL_ENV, '0')
    if fixture_flag not in {'0', '1'} or real_flag not in {'0', '1'}:
        raise ProviderRuntimeError('Provider environment flags must be 0 or 1.', 422, code='CONFIGURATION_INVALID')
    allow_fixture, allow_real = allow_fixture or fixture_flag == '1', allow_real or real_flag == '1'
    if from_environment:
        path = os.environ.get(CONFIG_ENV)
    expected = os.environ.get(CONFIG_DIGEST_ENV) if from_environment else None
    if path is None or path == '':
        registry = ProviderRegistry(allow_fixture=allow_fixture, allow_real=allow_real)
        if expected and registry.startup_digest() != expected:
            raise ProviderRuntimeError('Frozen host configuration changed.', 422, code='CONFIGURATION_INVALID')
        return registry
    try:
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            entry = os.fstat(stream.fileno())
            if not stat.S_ISREG(entry.st_mode) or entry.st_size > MAX_CONFIG_BYTES:
                raise ValueError('Configuration must be a bounded regular file')
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        parsed = ProviderHostConfig.model_validate(strict_json(raw, max_bytes=MAX_CONFIG_BYTES))
        configs = {item.config_id: item for item in parsed.providers}
        if len(configs) != len(parsed.providers):
            raise ValueError('Duplicate provider config_id')
        registry = ProviderRegistry(configs, allow_fixture=allow_fixture, allow_real=allow_real)
        if expected and registry.startup_digest() != expected:
            raise ValueError('Frozen host configuration changed')
        return registry
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ProviderRuntimeError):
            raise
        raise ProviderRuntimeError('Provider configuration failed closed validation.', 422, code='CONFIGURATION_INVALID') from exc
