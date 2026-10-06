"""GET-only exact-claim reading bundle export; never submit human judgments."""
import argparse
import hashlib
from http.client import HTTPException
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper_alpha.review_material_packet import (IMPLEMENTATION_FILES, MAX_BUNDLE, MAX_JSON, MAX_SOURCE,
    blank_judgments, decode, load_bundle, render_report, require, safe_read, verify_packet)
from paper_alpha.server.response_schemas import HealthResponse
from paper_alpha.storage import digest, json_text


def normalize_url(value):
    parts = urlsplit(value)
    try:
        address = ipaddress.ip_address(parts.hostname or '')
        port = parts.port
    except ValueError as exc:
        raise ValueError('Use an explicit numeric loopback HTTP host and port') from exc
    require(parts.scheme == 'http' and address.is_loopback and port is not None
            and parts.username is None and parts.password is None and parts.path in ('', '/')
            and not parts.query and not parts.fragment, 'Only an explicit local HTTP origin is supported')
    host = '[' + str(address) + ']' if address.version == 6 else str(address)
    return 'http://' + host + ':' + str(port)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Review export redirects are forbidden')


class Client:
    def __init__(self, base_url, timeout=10, get_retries=1):
        self.base_url = normalize_url(base_url)
        require(type(timeout) in (int, float) and 1 <= timeout <= 30, 'Timeout must be 1..30 seconds')
        require(type(get_retries) is int and 0 <= get_retries <= 2, 'GET retries must be 0..2')
        self.timeout, self.get_retries = timeout, get_retries
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.calls, self.deadline = 0, time.monotonic() + 180

    @staticmethod
    def allowed(method, path):
        if method != 'GET':
            return False
        route = urlsplit(path)
        if route.scheme or route.netloc or route.fragment:
            return False
        if route.path == '/api/health':
            return not route.query
        if re.fullmatch(r'/api/claim-review-targets/research_claims_[0-9a-f]{64}', route.path):
            pairs = parse_qsl(route.query, keep_blank_values=True)
            return len(pairs) == 1 and pairs[0][0] == 'claim_id' and 0 < len(pairs[0][1]) <= 120
        if not re.fullmatch(r'/api/review-materials/research_claims_[0-9a-f]{64}(?:/sources/(?:alpha101-paper|industry-paper|industry-method|industry-config|industry-author-(?:main|SetupDataA|SetupDataB|Table1|Table8A|Table8B)))?', route.path):
            return False
        pairs = parse_qsl(route.query, keep_blank_values=True)
        fields = dict(pairs)
        return len(pairs) == 2 and set(fields) == {'claim_id', 'expected_target_digest'} and 0 < len(fields['claim_id']) <= 120 and bool(re.fullmatch(r'[0-9a-f]{64}', fields['expected_target_digest']))

    def request(self, method, path, *, binary=False, media_type=None):
        require(self.allowed(method, path), 'Request is outside the GET-only material whitelist')
        limit = MAX_SOURCE if binary else MAX_JSON
        for attempt in range(self.get_retries + 1):
            self.calls += 1
            remaining = self.deadline - time.monotonic()
            require(self.calls <= 96 and remaining > 0, 'Material export HTTP budget exhausted')
            request = Request(self.base_url + path, method='GET', headers={'Accept': media_type or 'application/json'})
            try:
                with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                    require(response.geturl() == self.base_url + path, 'Response origin/path changed')
                    if media_type is not None:
                        require(response.headers.get_content_type() == media_type, 'Source media type differs from descriptor')
                    elif not binary:
                        require(response.headers.get_content_type() == 'application/json', 'Expected JSON material response')
                    length = response.headers.get('Content-Length')
                    require(length is None or length.isdigit() and int(length) <= limit, 'Invalid or oversized Content-Length')
                    chunks, size = [], 0
                    while True:
                        require(time.monotonic() <= self.deadline, 'Material export wall-time budget exhausted')
                        block = response.read1(min(65536, limit + 1 - size))
                        if not block:
                            break
                        size += len(block); chunks.append(block)
                        require(size <= limit, 'HTTP body exceeds its byte bound')
                    value = b''.join(chunks)
                    if length is not None and len(value) != int(length):
                        raise HTTPException('Incomplete GET response')
                    return value if binary else decode(value)
            except HTTPError as exc:
                raise ValueError('Material GET returned HTTP ' + str(exc.code)) from exc
            except (URLError, OSError, TimeoutError, ConnectionError, HTTPException) as exc:
                if attempt == self.get_retries:
                    raise ValueError('Material GET transport failed; no business write was sent') from exc
                time.sleep(.05 * (attempt + 1))


def _health(value, workspace_id):
    require(HealthResponse.model_validate(value).model_dump() == value, 'Health response differs from contract')
    require(value['workspace_id'] == workspace_id and value['ai_enabled'] is False
            and (value['version'], value['database_schema']) in {('0.21.0', 17), ('0.22.0', 17)}, 'Wrong workspace, unsupported version/schema or unexpected AI state')


def _write(root, name, value):
    data = value if isinstance(value, bytes) else json_text(value).encode('utf8')
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    require(path.parent.resolve() == path.parent.absolute(), 'Output parent became a link')
    with path.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    require(safe_read(path) == data, 'Output bytes changed during publication')
    return {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def prepare(base_url, workspace_id, claims_id, claim_id, out, *, expected_target_digest=None, timeout=10, get_retries=1):
    require(isinstance(workspace_id, str) and bool(re.fullmatch(r'[0-9a-f]{64}', workspace_id)), 'Explicit workspace identity is required')
    require(isinstance(claims_id, str) and bool(re.fullmatch(r'research_claims_[0-9a-f]{64}', claims_id)), 'Invalid saved claim batch identity')
    require(isinstance(claim_id, str) and 0 < len(claim_id) <= 120 and claim_id.strip(), 'Invalid exact claim identity')
    client = Client(base_url, timeout, get_retries)
    require(expected_target_digest is None or isinstance(expected_target_digest, str) and bool(re.fullmatch(r'[0-9a-f]{64}', expected_target_digest)), 'Invalid expected target digest')
    root = Path(out).absolute()
    require(root.parent.resolve() == root.parent.absolute(), 'Output directory traverses a symbolic link')
    root.mkdir(parents=False, exist_ok=False)
    invocation = {'schema_version': 1, 'kind': 'exact_claim_review_export_invocation',
        'request': {'base_url': client.base_url, 'workspace_id': workspace_id, 'claims_id': claims_id,
                    'claim_id': claim_id, 'expected_target_digest': expected_target_digest,
                    'timeout': timeout, 'get_retries': get_retries},
        'business_writes': 0, 'human_judgments_written': 0}
    files = {'invocation.json': _write(root, 'invocation.json', invocation)}
    phase, health, packet, contents = 'health', None, None, {}
    try:
        health = client.request('GET', '/api/health'); _health(health, workspace_id)
        phase = 'target'
        if expected_target_digest is None:
            target = client.request('GET', '/api/claim-review-targets/' + claims_id + '?' + urlencode({'claim_id': claim_id}))
            expected_target_digest = target['target_digest']
        require(isinstance(expected_target_digest, str) and bool(re.fullmatch(r'[0-9a-f]{64}', expected_target_digest)), 'Invalid expected target digest')
        phase = 'materials'
        route = '/api/review-materials/' + claims_id + '?' + urlencode({'claim_id': claim_id, 'expected_target_digest': expected_target_digest})
        packet = client.request('GET', route)
        require((packet['target']['claims_id'], packet['target']['claim_id'], packet['target']['target_digest']) ==
                (claims_id, claim_id, expected_target_digest), 'Response returned another claim or target')
        verify_packet(packet)
        phase = 'sources'
        for source in packet['sources']:
            contents[source['id']] = client.request('GET', source['download_url'], binary=True, media_type=source['media_type'])
        verify_packet(packet, contents)
        phase = 'final_get_fence'
        require(client.request('GET', route) == packet, 'Source or review history changed during export; restart in a new directory')
        current_health = client.request('GET', '/api/health'); _health(current_health, workspace_id)
        require((current_health['version'], current_health['database_schema']) == (health['version'], health['database_schema']), 'Runtime version changed during export')
        implementation = {name: safe_read(ROOT / name, MAX_JSON) for name in IMPLEMENTATION_FILES}
        phase = 'publish'
        files['packet.json'] = _write(root, 'packet.json', packet)
        files['report.md'] = _write(root, 'report.md', render_report(packet).encode('utf8'))
        files['blank-judgments.json'] = _write(root, 'blank-judgments.json', blank_judgments(packet))
        for source in packet['sources']:
            name = 'sources/' + source['id'] + '/' + source['filename']
            files[name] = _write(root, name, contents[source['id']])
        for name, value in implementation.items():
            files['verifier-source/' + name] = _write(root, 'verifier-source/' + name, value)
        status = {'schema_version': 1, 'kind': 'exact_claim_review_export_status', 'passed': True, 'phase': 'completed',
                  'materials_digest': packet['digest'], 'target_digest': expected_target_digest,
                  'http_calls': client.calls, 'business_writes': 0, 'human_judgments_written': 0}
        files['status.json'] = _write(root, 'status.json', status)
        require(sum(entry['size'] for entry in files.values()) <= MAX_BUNDLE, 'Material bundle exceeds total byte limit')
        config = {'base_url': client.base_url, 'workspace_id': workspace_id, 'claims_id': claims_id,
                  'claim_id': claim_id, 'expected_target_digest': expected_target_digest}
        body = {'schema_version': 1, 'kind': 'exact_claim_review_bundle', 'materials_digest': packet['digest'],
                'target_digest': expected_target_digest, 'config': config, 'health': health,
                'client_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in implementation.items()}, 'files': files}
        manifest = body | {'digest': digest(body)}
        _write(root, 'manifest.json', manifest)
        phase = 'offline_verify'
        result = verify(root)
        return result | {'out': str(root), 'http_calls': client.calls, 'business_writes': 0}
    except (ValueError, OSError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        # A failed attempt remains inspectable, and never becomes a valid bundle.
        # No human name, source, confirmation time or outcomes are inferred.
        failure = {'schema_version': 1, 'kind': 'exact_claim_review_export_failure', 'passed': False,
                   'phase': phase, 'error': type(exc).__name__ + ': ' + str(exc)[:1000],
                   'http_calls': client.calls, 'business_writes': 0, 'human_judgments_written': 0,
                   'workspace_id_observed': health.get('workspace_id') if isinstance(health, dict) else None,
                   'target_digest_observed': expected_target_digest,
                   'materials_digest_observed': packet.get('digest') if isinstance(packet, dict) else None,
                   'source_bytes_received': {key: {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)} for key, value in contents.items()}}
        try:
            _write(root, 'failure.json', failure)
        except (ValueError, OSError):
            pass  # Never follow changed output links or overwrite any file.
        raise


def verify(out):
    packet = load_bundle(out)
    root = Path(out).absolute()
    if root.name == 'packet.json':
        root = root.parent
    manifest = decode(safe_read(root / 'manifest.json', MAX_JSON))
    return {'passed': True, 'materials_digest': packet['digest'], 'target_digest': packet['target']['target_digest'],
            'manifest_digest': manifest['digest'], 'source_files_verified': True,
            'human_records': packet['review_status']['human_records'], 'semantic_quality_score': None,
            'llm_api_called': False, 'verification_scope': 'offline_local_bytes_and_snapshot_links_not_authenticated_semantics'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    get = commands.add_parser('prepare')
    get.add_argument('--base-url', required=True); get.add_argument('--workspace-id', required=True)
    get.add_argument('--claims-id', required=True); get.add_argument('--claim-id', required=True)
    get.add_argument('--out', type=Path, required=True); get.add_argument('--expected-target-digest')
    get.add_argument('--timeout', type=float, default=10); get.add_argument('--get-retries', type=int, default=1)
    check = commands.add_parser('verify'); check.add_argument('out', type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify(args.out) if args.command == 'verify' else prepare(args.base_url, args.workspace_id,
            args.claims_id, args.claim_id, args.out, expected_target_digest=args.expected_target_digest,
            timeout=args.timeout, get_retries=args.get_retries)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False)); return 0
    except (ValueError, OSError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        print(json.dumps({'passed': False, 'error': type(exc).__name__ + ': ' + str(exc)[:1000],
                          'business_writes': 0, 'human_judgments_written': 0}, ensure_ascii=False)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
