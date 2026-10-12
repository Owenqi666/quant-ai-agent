"""Bounded local consistency evidence; never a grant to invoke a real model.

Hashes detect changed local inputs and code. They are not signatures and cannot
defend against someone who can replace both the code and the complete bundle.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from .provider_contract import canonical_json, digest, strict_json
from .provider_schema import DIGEST_PATTERN

MAX_PROFILE_BYTES = 512 * 1024
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 4096
_FILES = {'profile.json', 'observation.json', 'manifest.json'}
_SOURCE_FILES = ('provider_cli_probe.py', 'provider_qualification.py')


class ProviderQualificationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Diagnostic(AuthorModel):
    code: str = Field(pattern=r'^[A-Z0-9_]{1,80}$')
    message: str = Field(min_length=1, max_length=1000)


class SourceDigests(AuthorModel):
    provider_cli_probe: str = Field(pattern=DIGEST_PATTERN)
    provider_qualification: str = Field(pattern=DIGEST_PATTERN)


class VersionedEvidence(AuthorModel):
    @model_validator(mode='before')
    @classmethod
    def integer_versions(cls, value):
        if isinstance(value, dict):
            for key in ('evidence_version', 'qualification_version'):
                if key in value and type(value[key]) is not int:
                    raise ValueError('Evidence versions require integers')
        return value


class EvidenceManifest(VersionedEvidence):
    evidence_version: Literal[1]
    evidence_kind: Literal['fixture', 'local_cli_probe']
    profile_digest: str = Field(pattern=DIGEST_PATTERN)
    profile_sha256: str = Field(pattern=DIGEST_PATTERN)
    observation_sha256: str = Field(pattern=DIGEST_PATTERN)
    source_digests: SourceDigests


class VerifiedEvidence(VersionedEvidence):
    evidence_version: Literal[1] = 1
    valid: bool
    evidence_kind: Literal['fixture', 'local_cli_probe'] | None = None
    profile_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    # A's closed model is validated separately to keep module imports acyclic.
    observation: dict | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list, max_length=30)


class QualificationStages(AuthorModel):
    static_checks: Literal['passed', 'blocked']
    local_cli_request: Literal['observed', 'unverified', 'blocked']
    remote_schema_acceptance: Literal['unverified'] = 'unverified'
    production_tool_boundary: Literal['unverified'] = 'unverified'
    real_inference: Literal['unverified'] = 'unverified'


class Qualification(VersionedEvidence):
    qualification_version: Literal[1] = 1
    profile_digest: str = Field(pattern=DIGEST_PATTERN)
    evidence_kind: Literal['fixture', 'local_cli_probe'] | None = None
    evidence_verified: bool
    stages: QualificationStages
    compatibility_verified: Literal[False] = False
    tool_boundary_verified: Literal[False] = False
    inference_verified: Literal[False] = False
    real_dispatch_enabled: Literal[False] = False
    blockers: list[Diagnostic] = Field(max_length=64)


def _diagnostic(code, message):
    return {'code': code, 'message': message}


def _bounded_value(value, limit):
    try:
        raw = canonical_json(value).encode('utf-8')
        return strict_json(raw, max_bytes=limit)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProviderQualificationError('EVIDENCE_INVALID', 'Evidence is not bounded strict JSON.') from exc


def _profile(value):
    from .provider_cli_profile import validate_profile
    try:
        return validate_profile(_bounded_value(value, MAX_PROFILE_BYTES))
    except (ValueError, TypeError) as exc:
        raise ProviderQualificationError('PROFILE_INVALID', 'Execution profile failed closed validation.') from exc


def _observation(value, profile):
    from .provider_cli_probe import LocalCliObservation
    try:
        observation = LocalCliObservation.model_validate(
            _bounded_value(value, MAX_OBSERVATION_BYTES)).model_dump()
    except (ValueError, TypeError) as exc:
        raise ProviderQualificationError('OBSERVATION_INVALID', 'Probe observation failed closed validation.') from exc
    if observation['profile_digest'] != profile['profile_digest']:
        raise ProviderQualificationError('PROFILE_IDENTITY_MISMATCH', 'Observation belongs to another execution profile.')
    expected_kind = 'fixture' if profile['mode'] == 'fixture' else 'local_cli_probe'
    if observation['evidence_kind'] != expected_kind:
        raise ProviderQualificationError('EVIDENCE_KIND_MISMATCH', 'Fixture and local CLI evidence identities disagree.')
    if observation['schema_observed']:
        matches = observation['schema_digest'] == profile['wire_schema_digest']
        if observation['schema_matches'] is not matches:
            raise ProviderQualificationError('OBSERVATION_INCONSISTENT', 'Reported schema comparison differs from its digest.')
    elif observation['schema_digest'] is not None or observation['schema_matches'] is not None:
        raise ProviderQualificationError('OBSERVATION_INCONSISTENT', 'Unobserved schema has a claimed comparison.')
    return observation


def _source_digests():
    result = {}
    for name in _SOURCE_FILES:
        path = Path(__file__).with_name(name)
        # These are fixed local sources, never paths read from an evidence file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            entry = os.fstat(stream.fileno())
            if not stat.S_ISREG(entry.st_mode) or entry.st_size > 1024 * 1024:
                raise ProviderQualificationError('SOURCE_UNAVAILABLE', 'Evidence implementation source is unavailable.')
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProviderQualificationError('SOURCE_UNAVAILABLE', 'Evidence implementation source exceeds its limit.')
        result[Path(name).stem] = hashlib.sha256(raw).hexdigest()
    return result


def _bytes(value):
    return (canonical_json(value) + '\n').encode('utf-8')


def _reject_symlink_ancestors(directory):
    absolute = Path(directory).absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise ProviderQualificationError('EVIDENCE_PATH_INVALID', 'Evidence paths may not traverse symbolic links.')


def _read_file(directory_fd, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ProviderQualificationError('EVIDENCE_FILE_INVALID', 'Evidence must contain private bounded regular files.')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ProviderQualificationError('EVIDENCE_FILE_INVALID', 'Evidence changed while being read or exceeds its limit.')
    return raw


def write_evidence(output_dir, profile, observation):
    """Create a new three-file local bundle; never overwrite existing evidence."""
    profile = _profile(profile)
    observation = _observation(observation, profile)
    profile_bytes, observation_bytes = _bytes(profile), _bytes(observation)
    manifest = EvidenceManifest(evidence_version=1, evidence_kind=observation['evidence_kind'],
        profile_digest=profile['profile_digest'],
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        observation_sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source_digests=SourceDigests.model_validate(_source_digests())).model_dump()
    directory = Path(output_dir)
    _reject_symlink_ancestors(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, raw in (('profile.json', profile_bytes), ('observation.json', observation_bytes),
                      ('manifest.json', _bytes(manifest))):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return verify_evidence(directory, profile)


def verify_evidence(evidence_dir, current_profile):
    """Re-read all allowlisted bytes and compare current identity and sources.

    ``valid`` means local consistency, including an honestly recorded failed
    probe. It never means that real inference or production tools are approved.
    """
    try:
        current = _profile(current_profile)
        _reject_symlink_ancestors(evidence_dir)
        directory_fd = os.open(Path(evidence_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if set(os.listdir(directory_fd)) != _FILES:
                raise ProviderQualificationError('EVIDENCE_FILES_INVALID', 'Evidence file inventory differs from the closed bundle.')
            manifest_raw = _read_file(directory_fd, 'manifest.json', MAX_MANIFEST_BYTES)
            manifest = EvidenceManifest.model_validate(strict_json(manifest_raw, max_bytes=MAX_MANIFEST_BYTES)).model_dump()
            profile_raw = _read_file(directory_fd, 'profile.json', MAX_PROFILE_BYTES)
            observation_raw = _read_file(directory_fd, 'observation.json', MAX_OBSERVATION_BYTES)
        finally:
            os.close(directory_fd)
        for key, raw in (('profile', profile_raw), ('observation', observation_raw)):
            if hashlib.sha256(raw).hexdigest() != manifest[key + '_sha256']:
                raise ProviderQualificationError('EVIDENCE_DIGEST_MISMATCH', 'Evidence file digest differs from its manifest.')
        archived = _profile(strict_json(profile_raw, max_bytes=MAX_PROFILE_BYTES))
        if archived['profile_digest'] != manifest['profile_digest']:
            raise ProviderQualificationError('PROFILE_IDENTITY_MISMATCH', 'Manifest and archived execution profile disagree.')
        if archived['contract_id'] != current['contract_id']:
            raise ProviderQualificationError('CONTRACT_MISMATCH', 'Evidence belongs to another business contract.')
        if archived['source_digests'] != current['source_digests'] or manifest['source_digests'] != _source_digests():
            raise ProviderQualificationError('SOURCE_DRIFT', 'Implementation source differs from the recorded evidence.')
        if archived != current:
            raise ProviderQualificationError('PROFILE_DRIFT', 'Executable, configuration or execution contract changed.')
        observation = _observation(strict_json(observation_raw, max_bytes=MAX_OBSERVATION_BYTES), archived)
        if observation['evidence_kind'] != manifest['evidence_kind']:
            raise ProviderQualificationError('EVIDENCE_KIND_MISMATCH', 'Manifest and observation provenance disagree.')
        return VerifiedEvidence(valid=True, evidence_kind=manifest['evidence_kind'],
            profile_digest=archived['profile_digest'], observation=observation).model_dump()
    except ProviderQualificationError as exc:
        diagnostic = _diagnostic(exc.code, str(exc))
    except (OSError, ValueError, TypeError, RecursionError):
        diagnostic = _diagnostic('EVIDENCE_INVALID', 'Evidence is missing, unreadable or violates its closed contract.')
    return VerifiedEvidence(valid=False, diagnostics=[diagnostic]).model_dump()


def assess_qualification(profile, verified_evidence=None):
    """Derive bounded stage diagnostics. This release has no real dispatch grant."""
    profile = _profile(profile)
    blockers = []
    static_passed = profile['static_checks']['passed']
    if not static_passed:
        blockers.append(_diagnostic('STATIC_CHECKS_BLOCKED', 'Execution profile has unresolved static checks.'))
    evidence, observation = None, None
    if verified_evidence is None:
        blockers.append(_diagnostic('EVIDENCE_MISSING', 'No locally verified probe evidence was supplied.'))
    else:
        try:
            evidence = VerifiedEvidence.model_validate(_bounded_value(verified_evidence, MAX_PROFILE_BYTES)).model_dump()
            if evidence['valid']:
                if evidence['profile_digest'] != profile['profile_digest']:
                    raise ProviderQualificationError('PROFILE_DRIFT', 'Verified evidence belongs to another execution profile.')
                observation = _observation(evidence['observation'], profile)
                if evidence['evidence_kind'] != observation['evidence_kind'] or evidence['diagnostics']:
                    raise ProviderQualificationError('EVIDENCE_INVALID', 'Verified evidence has inconsistent provenance or diagnostics.')
            else:
                blockers.extend(evidence['diagnostics'] or [_diagnostic('EVIDENCE_INVALID', 'Evidence verification did not pass.')])
        except (ValueError, TypeError) as exc:
            blockers.append(_diagnostic(getattr(exc, 'code', 'EVIDENCE_INVALID'),
                str(exc) if isinstance(exc, ProviderQualificationError) else 'Verified evidence violates its closed contract.'))
            evidence, observation = None, None
    local_stage = 'unverified'
    if observation is not None:
        for code in observation['blockers']:
            blockers.append(_diagnostic(code, 'The local CLI observation reported ' + code + '.'))
        structural_blockers = set(observation['blockers']) - {
            'REMOTE_BACKEND_UNVERIFIED', 'PRODUCTION_BOUNDARY_UNVERIFIED',
            'REMOTE_UNVERIFIED', 'FORMAL_BOUNDARY_UNVERIFIED',
            'TOOLS_EXPOSED', 'WIRE_SCHEMA_MISMATCH',
        }
        if observation['evidence_kind'] == 'fixture':
            blockers.append(_diagnostic('FIXTURE_NOT_REAL_EVIDENCE', 'Fixture results do not verify the installed real CLI.'))
        elif not observation['process_stopped'] or structural_blockers or observation['status'] != 'blocked':
            local_stage = 'blocked'
        elif observation['request_observed']:
            local_stage = 'observed'
        else:
            local_stage = 'blocked'
            blockers.append(_diagnostic('REQUEST_NOT_OBSERVED', 'No bounded local CLI request was observed.'))
        if not observation['process_stopped']:
            blockers.append(_diagnostic('PROCESS_STOP_UNCONFIRMED', 'Probe process termination remains unconfirmed.'))
        if observation['transport_error'] and observation['transport_error'] not in {'LOCAL_STUB_REJECTED', 'CLI_FAILED'}:
            code = observation['transport_error']
            blockers.append(_diagnostic(code, 'The local CLI transport reported ' + code + '.'))
        if observation['cli_version'] != profile['expected_cli_version']:
            blockers.append(_diagnostic('CLI_VERSION_UNVERIFIED', 'Observed CLI version does not match the execution profile.'))
        if observation['request_observed'] and observation['requested_model'] != profile['model']:
            blockers.append(_diagnostic('MODEL_IDENTITY_MISMATCH', 'The observed request model differs from the execution profile.'))
        if observation['tools']:
            blockers.append(_diagnostic('TOOLS_EXPOSED', 'The local request exposed one or more tools.'))
        if observation['schema_matches'] is not True:
            blockers.append(_diagnostic('WIRE_SCHEMA_UNVERIFIED', 'The local request did not confirm the frozen wire schema.'))
    blockers.extend([
        _diagnostic('REMOTE_SCHEMA_UNVERIFIED', 'Local observations cannot verify schema acceptance by the real backend.'),
        _diagnostic('PRODUCTION_TOOL_BOUNDARY_UNVERIFIED', 'The production tool boundary has not been verified.'),
        _diagnostic('REAL_INFERENCE_UNVERIFIED', 'No real model inference has been verified.'),
        _diagnostic('REAL_DISPATCH_DISABLED', 'This release does not issue real dispatch authorization.'),
    ])
    blockers = list({item['code']: item for item in blockers}.values())
    return Qualification(profile_digest=profile['profile_digest'],
        evidence_kind=observation['evidence_kind'] if observation else None,
        evidence_verified=observation is not None,
        stages=QualificationStages(static_checks='passed' if static_passed else 'blocked', local_cli_request=local_stage),
        blockers=blockers).model_dump()
