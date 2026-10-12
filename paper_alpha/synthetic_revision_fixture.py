"""Portable, opt-in Alpha#006 synthetic Provider transport fixture.

This standard-library entry point implements only the frozen fixture transport
protocol. It is not Codex CLI, an AI model, a research oracle, or execution
permission. The host retains all source, validation, correction and run checks.

Run ``python -B -m paper_alpha.synthetic_revision_fixture configure --out NEW``
after installing or relocating a package. The generated host-local paths are
not intended to survive relocation; the packaged implementation itself is.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys

CONFIG_ID = 'synthetic-alpha006-window-v1'
MODEL_ID = CONFIG_ID
CONTRACT_ID = 'research_provider_v2'
PROMPT_VERSION = 'bounded_research_v2_hints_1'
# CodexCliProvider requires this exact probe format even in fixture mode.
# It describes an emulated transport version, never an installed CLI identity.
TRANSPORT_VERSION = '0.145.0'
MAX_INPUT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_JSON_NODES = 20000
MAX_JSON_DEPTH = 32
MARKER = b'REQUEST_JSON follows:\n'
EXPRESSION = '-1 * ts_corr(open, volume, +10)'
FINAL_POLICY = 'Final-test data and evaluation are unavailable to the provider.'
_REQUEST_KEYS = frozenset({'schema_version', 'contract_id', 'prompt_version', 'operation',
    'proposal_id', 'attempt_id', 'context_digest', 'candidate_limit', 'instruction',
    'output_token_target', 'context'})
_CONTEXT_KEYS = frozenset({'schema_version', 'context_version', 'research_id',
    'base_revision_id', 'paper_id', 'dataset_id', 'source_digest', 'paper_sha256',
    'data_sha256', 'evidence', 'hypotheses', 'capabilities', 'development',
    'exposure_manifest', 'semantic_fidelity', 'operation', 'source_kind'})


class FixtureInputError(ValueError):
    """Bounded, non-sensitive failure code; no input text is reflected."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise FixtureInputError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FixtureInputError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def nonfinite(_):
        raise FixtureInputError('NONFINITE_JSON')

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            _require(count <= MAX_JSON_NODES and depth <= MAX_JSON_DEPTH, 'JSON_COMPLEXITY')
            if type(item) is dict:
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is float:
                _require(math.isfinite(item), 'NONFINITE_JSON')
        return value
    except FixtureInputError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise FixtureInputError('INVALID_JSON') from None


def _identifier(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def response_for_prompt(raw):
    """Return one synthetic draft from bounded host-projected bytes only.

    Context digest consistency is a local integrity check, not host/source
    authentication. No files, registry, database, model, or tools are accessed.
    This fixture never corrects its deliberate +10 representation itself.
    """
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_INPUT_BYTES, 'INPUT_LIMIT')
    _, marker, body = raw.partition(MARKER)
    _require(marker == MARKER and bool(body), 'PROMPT_MARKER_MISSING')
    request = _strict_json(body)
    _require(type(request) is dict and set(request) == _REQUEST_KEYS, 'REQUEST_SHAPE')
    _require(type(request['schema_version']) is int and request['schema_version'] == 2
             and request['contract_id'] == CONTRACT_ID
             and request['prompt_version'] == PROMPT_VERSION, 'CONTRACT_UNSUPPORTED')
    _require(request['operation'] == 'generate_candidates', 'OPERATION_UNSUPPORTED')
    _require(type(request['candidate_limit']) is int and request['candidate_limit'] == 1,
             'CANDIDATE_SCOPE_UNSUPPORTED')
    _require(type(request['instruction']) is str and len(request['instruction']) <= 4000,
             'REQUEST_SHAPE')
    _require(type(request['output_token_target']) is int and request['output_token_target'] > 0,
             'REQUEST_SHAPE')
    for key, pattern in (('proposal_id', r'proposal_[0-9a-f]{64}'),
                         ('attempt_id', r'proposal_attempt_[0-9a-f]{64}'),
                         ('context_digest', r'[0-9a-f]{64}')):
        _require(_identifier(request[key], pattern), 'REQUEST_IDENTITY')
    context = request['context']
    _require(type(context) is dict and set(context) == _CONTEXT_KEYS, 'CONTEXT_SHAPE')
    _require(hashlib.sha256(_canonical(context)).hexdigest() == request['context_digest'],
             'CONTEXT_DIGEST_MISMATCH')
    _require(type(context['schema_version']) is int and context['schema_version'] == 2
             and context['source_kind'] == 'research_revision'
             and context['context_version'] == 'research-provider-context-v2'
             and context['operation'] == 'generate_candidates'
             and context['semantic_fidelity'] == 'unverified', 'SOURCE_SCOPE_UNSUPPORTED')
    capabilities, development = context['capabilities'], context['development']
    _require(type(capabilities) is dict and capabilities.get('data_kind') == 'synthetic'
             and type(development) is dict and development.get('evaluation_period') == 'validation'
             and development.get('final_test_policy') == FINAL_POLICY, 'DATA_SCOPE_UNSUPPORTED')
    fields, operators = capabilities.get('fields'), capabilities.get('operators')
    _require(type(fields) is list and type(operators) is list
             and all(type(item) is dict for item in fields + operators), 'CAPABILITIES_UNSUPPORTED')
    field_names = [item.get('name') for item in fields]
    _require(all(type(name) is str for name in field_names)
             and len(field_names) == len(set(field_names))
             and {'open', 'volume'} <= set(field_names), 'FIELDS_UNAVAILABLE')
    _require(any(item.get('name') == 'ts_corr' and type(item.get('arity')) is int
                 and item['arity'] == 3 for item in operators), 'OPERATOR_UNAVAILABLE')
    evidence, hypotheses = context['evidence'], context['hypotheses']
    _require(type(evidence) is list and 1 <= len(evidence) <= 100
             and type(hypotheses) is list and 1 <= len(hypotheses) <= 100
             and all(type(item) is dict for item in evidence + hypotheses), 'ASSERTIONS_UNSUPPORTED')
    evidence_ids = [item.get('id') for item in evidence]
    hypothesis_ids = [item.get('id') for item in hypotheses]
    _require(all(type(value) is str for value in evidence_ids + hypothesis_ids)
             and len(evidence_ids) == len(set(evidence_ids))
             and len(hypothesis_ids) == len(set(hypothesis_ids)), 'ASSERTION_IDENTITIES')
    matching = [item for item in hypotheses if item['id'] == 'h-alpha006']
    _require(len(matching) == 1, 'ALPHA006_HYPOTHESIS_REQUIRED')
    hypothesis = matching[0]
    _require(hypothesis.get('attribution') == 'paper_original'
             and hypothesis.get('required_fields') == ['open', 'volume']
             and hypothesis.get('evidence_ids') == ['alpha006-formula'], 'ALPHA006_HYPOTHESIS_UNSUPPORTED')
    formula = [item for item in evidence if item['id'] == 'alpha006-formula']
    _require(len(formula) == 1 and formula[0].get('quote') == '(-1 * correlation(open, volume, 10))',
             'ALPHA006_EVIDENCE_REQUIRED')
    # Clone through plain JSON; no mutable object or file authority crosses.
    draft = {'evidence': _strict_json(_canonical(evidence)),
             'hypotheses': _strict_json(_canonical(hypotheses)),
             'candidates': [{'id': 'alpha006', 'hypothesis_id': 'h-alpha006',
                 'expression': EXPRESSION, 'origin': 'model_conjecture',
                 'changes': ['Engineering fixture deliberately emits unary +10 in the window; '
                             'no alpha discovery or economic-mechanism claim.']}]}
    result = {'schema_version': 2, 'contract_id': CONTRACT_ID, 'operation': 'generate_candidates',
              'proposal_id': request['proposal_id'], 'attempt_id': request['attempt_id'],
              'context_digest': request['context_digest'], 'outcome': 'draft',
              'material': None, 'draft': draft, 'decision': None, 'abstention': None}
    _require(len(_canonical(result)) + 1 <= MAX_OUTPUT_BYTES, 'OUTPUT_LIMIT')
    return result


def configure(output_directory):
    """Create new host-local shim and strict config; do not enable dispatch.

    Paths are resolved from this installed module and the current interpreter.
    Regenerate after relocation. The directory must be new, with an existing
    parent; no existing config, code, provider flags, or app workspace is changed.
    """
    target = Path(output_directory).expanduser()
    _require(bool(target.name), 'CONFIG_DIRECTORY_INVALID')
    parent = target.parent.resolve(strict=True)
    _require(parent.is_dir(), 'CONFIG_DIRECTORY_INVALID')
    target = parent / target.name
    source = Path(__file__).resolve(strict=True)
    interpreter = Path(sys.executable).resolve(strict=True)
    _require(source.is_file() and interpreter.is_file() and os.access(interpreter, os.X_OK),
             'CONFIG_EXECUTABLE_UNAVAILABLE')
    target.mkdir(mode=0o700, exist_ok=False)
    created = []
    try:
        shim = target / 'synthetic-alpha006-provider'
        config = target / 'provider.json'
        # Shell quoting only quotes verified local paths; exec forwards argv
        # unchanged and replaces the shim process, preserving group supervision.
        wrapper = ('#!/bin/sh\n# Synthetic fixture only; no Codex CLI or AI model.\nexec '
                   + shlex.quote(str(interpreter)) + ' -B ' + shlex.quote(str(source)) + ' "$@"\n').encode()
        values = ((shim, wrapper, 0o700),
                  (config, _canonical({'schema_version': 1, 'providers': [{
                      'config_id': CONFIG_ID, 'mode': 'fixture', 'enabled': True,
                      'executable': str(shim), 'model': MODEL_ID, 'reasoning_effort': 'low',
                      'expected_cli_version': TRANSPORT_VERSION,
                      'compatibility_verified': False, 'tool_boundary_verified': False,
                      'inference_verified': False}]}) + b'\n', 0o600))
        for path, content, mode in values:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            created.append(path)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
        return {'config_path': str(config), 'executable_path': str(shim),
                'provider_config_id': CONFIG_ID, 'fixture_model': MODEL_ID,
                'provider_permission': 'requires_explicit_allow_fixture_provider',
                'native_model_calls': 0}
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def _exec_arguments(argv):
    parser = argparse.ArgumentParser(prog='synthetic-alpha006-provider exec', add_help=False)
    parser.add_argument('stdin_source', choices=['-'])
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--color', choices=['never'], required=True)
    parser.add_argument('--output-schema', required=True)
    parser.add_argument('-o', '--output-last-message', required=True)
    parser.add_argument('--cd', required=True)
    parser.add_argument('--skip-git-repo-check', action='store_true')
    parser.add_argument('--sandbox', choices=['read-only'], required=True)
    parser.add_argument('--ephemeral', action='store_true')
    parser.add_argument('--ignore-user-config', action='store_true')
    parser.add_argument('--strict-config', action='store_true')
    parser.add_argument('--model', choices=[MODEL_ID], required=True)
    parser.add_argument('-c', action='append', default=[])
    args = parser.parse_args(argv)
    _require(all((args.json, args.skip_git_repo_check, args.ephemeral,
                  args.ignore_user_config, args.strict_config)), 'CLI_FLAGS_UNSUPPORTED')
    _require(4 <= len(args.c) <= 64 and all(type(value) is str and len(value) <= 256 for value in args.c),
             'CLI_CONFIG_UNSUPPORTED')
    base = {'approval_policy="never"', 'web_search="disabled"',
            'allow_login_shell=false', 'model_reasoning_effort="low"'}
    _require(base <= set(args.c) and len(args.c) == len(set(args.c))
             and all(value in base or re.fullmatch(r'features\.[a-z_]+=false', value)
                     for value in args.c), 'CLI_CONFIG_UNSUPPORTED')
    cwd = Path.cwd().resolve(strict=True)
    _require(Path(args.cd).is_absolute() and Path(args.cd).resolve(strict=True) == cwd,
             'CLI_DIRECTORY_UNSUPPORTED')
    for value, name in ((args.output_schema, 'output.schema.json'),
                        (args.output_last_message, 'response.raw.json')):
        path = Path(value)
        _require(path.is_absolute() and path.parent.resolve(strict=True) == cwd
                 and path.name == name, 'CLI_OUTPUT_LOCATION_UNSUPPORTED')
    return args


def main(argv=None):
    """Frozen fixture transport and explicit host-local configuration command."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--version']:
        print('codex-cli ' + TRANSPORT_VERSION)
        return 0
    if argv == ['exec', '--help']:
        print('Synthetic Alpha#006 fixture; not Codex CLI or a model. '
              '--json --output-schema --output-last-message --sandbox --ephemeral '
              '--ignore-user-config --skip-git-repo-check --cd --strict-config')
        return 0
    if argv and argv[0] == 'configure':
        parser = argparse.ArgumentParser(description='Create opt-in synthetic Alpha#006 host configuration.')
        parser.add_argument('--out', required=True, help='NEW directory under an existing parent')
        args = parser.parse_args(argv[1:])
        try:
            result = configure(args.out)
            print(_canonical(result).decode())
            return 0
        except (FixtureInputError, OSError, ValueError, TypeError):
            print('Synthetic configuration could not be created; use a new directory.', file=sys.stderr)
            return 2
    try:
        _require(bool(argv) and argv[0] == 'exec', 'CLI_OPERATION_UNSUPPORTED')
        args = _exec_arguments(argv[1:])
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        response = _canonical(response_for_prompt(raw)) + b'\n'
        descriptor = os.open(args.output_last_message,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(response)
        # No token accounting is supplied: the existing adapter records unknown
        # usage/incomplete supplier accounting, never invented zero/positive tokens.
        print('{"type":"turn.started"}')
        print('{"type":"turn.completed"}')
        return 0
    except (FixtureInputError, OSError, ValueError, TypeError, RecursionError) as exc:
        code = exc.code if isinstance(exc, FixtureInputError) else 'FIXTURE_IO_ERROR'
        print('Synthetic fixture refused the input: ' + code, file=sys.stderr)
        print('{"type":"turn.failed"}')
        return 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
