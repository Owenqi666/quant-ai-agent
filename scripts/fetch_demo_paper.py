"""Explicitly fetch the fixed Alpha101 example from its original arXiv URL.

No API key, model or arbitrary URL/path input. Matching existing bytes are reused;
different existing bytes are never overwritten. A hash identifies bytes, not
authorship, redistribution permission or paper fidelity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PAPER_ID = 'arxiv:1601.00991v3'
URL = 'https://arxiv.org/pdf/1601.00991v3'
SHA256 = '1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9'
RELATIVE_TARGET = 'examples/alpha101/paper.pdf'
JSON_SHA256 = '2e958978f23f7359f8f1e18e90f41db4f836216e69c717c3dcc1d2b4b761d7cd'
JSON_TARGET = 'examples/alpha101/paper.json'
MAX_BYTES = 8 * 1024 * 1024
SOCKET_TIMEOUT = 20
TOTAL_SECONDS = 45


class FetchError(ValueError):
    def __init__(self, code, *, cause_type=None, http_status=None):
        super().__init__(code)
        self.cause_type = cause_type
        self.http_status = http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise FetchError('redirect_rejected')


def _safe_target(root):
    root = Path(root)
    if root.is_symlink():
        raise FetchError('linked_root_rejected')
    root = root.resolve(strict=True)
    target = root / RELATIVE_TARGET
    for part in (root / 'examples', root / 'examples/alpha101', target, root / JSON_TARGET):
        if part.is_symlink():
            raise FetchError('linked_target_rejected')
    target.parent.mkdir(parents=True, exist_ok=True)
    return root, target


def _verify(body, kind='pdf'):
    if len(body) > MAX_BYTES or kind == 'pdf' and not body.startswith(b'%PDF-'):
        raise FetchError('invalid_or_oversized_' + kind)
    expected = SHA256 if kind == 'pdf' else JSON_SHA256
    if hashlib.sha256(body).hexdigest() != expected:
        raise FetchError('fixed_paper_' + kind + '_sha256_mismatch')


def _existing(target, kind='pdf'):
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise FetchError('existing_target_not_regular_or_bounded')
    with target.open('rb') as stream:
        body = stream.read(MAX_BYTES + 1)
    _verify(body, kind)
    return body


def _publish(target, body, kind='pdf'):
    # Exclusive hard-link publication gives an atomic final name without replacing
    # an existing file. The temporary name is private to this invocation.
    descriptor, name = tempfile.mkstemp(prefix='.paper-bootstrap-', suffix='.tmp', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            # A concurrent bootstrap may have published the same fixed bytes.
            _existing(target, kind)
        temporary.unlink()
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _existing(target, kind)
    finally:
        if temporary.exists():
            temporary.unlink()


def _paper_json(body):
    try:
        import pypdf
    except ImportError:
        raise FetchError('locked_pypdf_install_required') from None
    try:
        pages = [{'page': i + 1, 'text': page.extract_text() or ''}
                 for i, page in enumerate(pypdf.PdfReader(io.BytesIO(body)).pages)]
        value = {'id': PAPER_ID, 'title': '101 Formulaic Alphas', 'authors': ['Zura Kakushadze'],
                 'url': 'https://arxiv.org/abs/1601.00991v3', 'pdf_url': URL, 'version': 'v3',
                 'document_sha256': SHA256,
                 'extraction': {'tool': 'pypdf', 'page_numbering': '1-based physical PDF pages'},
                 'pages': pages}
        result = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        _verify(result, 'json')
        return result, pypdf.__version__
    except FetchError:
        raise
    except Exception:
        raise FetchError('fixed_paper_extraction_failed') from None


def _complete(root, body, receipt, existing_json):
    if existing_json is not None:
        return {**receipt, 'json_operation': 'verified_existing', 'json_sha256': JSON_SHA256,
                'json_bytes': len(existing_json), 'extractor_version': None}
    extracted, version = _paper_json(body)
    _verify(extracted, 'json')
    _publish(root / JSON_TARGET, extracted, 'json')
    return {**receipt, 'json_operation': 'extracted_and_verified', 'json_sha256': JSON_SHA256,
            'json_bytes': len(extracted), 'extractor_version': version}


def _ssl_context():
    # Transport remains stdlib. The already locked web dependency supplies public
    # CA roots on Python installations whose default trust files are absent.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context(), 'system_default'
    return ssl.create_default_context(cafile=certifi.where()), 'locked_certifi_bundle'


def fetch(root=ROOT, *, opener=None):
    """Return a bounded receipt; injectable transport is explicitly test-only."""
    root, target = _safe_target(root)
    existing_json = _existing(root / JSON_TARGET, 'json')
    body = _existing(target)
    receipt = {'schema_version': 1, 'paper_id': PAPER_ID, 'requested_url': URL,
               'relative_target': RELATIVE_TARGET, 'expected_sha256': SHA256,
               'checked_at': datetime.now(timezone.utc).isoformat(),
               'redistribution_permission': 'not_verified', 'authorship_authenticated': False,
               'json_relative_target': JSON_TARGET, 'expected_json_sha256': JSON_SHA256,
               'scope': 'Fixed PDF and exact extracted example bytes only; no model call or research judgment.'}
    if body is not None:
        return _complete(root, body, {**receipt, 'passed': True, 'operation': 'verified_existing', 'sha256': SHA256,
                'bytes': len(body), 'transport': 'not_used', 'tls_verified': None,
                'trust_store': None, 'retrieved_at': None, 'final_url': None, 'last_modified': None, 'content_type': None}, existing_json)
    injected = opener is not None
    trust_store = 'injected_test_transport' if injected else None
    if opener is None:
        context, trust_store = _ssl_context()
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(URL, headers={'User-Agent': 'Paper-to-Alpha-example-bootstrap/1', 'Accept': 'application/pdf'})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=SOCKET_TIMEOUT) as response:
            if response.status != 200 or response.geturl() != URL:
                raise FetchError('unexpected_status_or_final_url')
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/pdf':
                raise FetchError('unexpected_content_type')
            declared = response.headers.get('Content-Length')
            if declared is not None:
                if not declared.isdecimal() or len(declared) > 10 or int(declared) > MAX_BYTES:
                    raise FetchError('invalid_or_oversized_content_length')
                declared = int(declared)
            parts, length = [], 0
            while True:
                if time.monotonic() - started > TOTAL_SECONDS:
                    raise FetchError('download_time_budget_exceeded')
                chunk = response.read(min(65536, MAX_BYTES + 1 - length))
                if not chunk:
                    break
                length += len(chunk)
                if length > MAX_BYTES:
                    raise FetchError('download_size_limit')
                parts.append(chunk)
            body = b''.join(parts)
            if declared is not None and declared != length:
                raise FetchError('truncated_response')
            _verify(body)
            fields = {'final_url': response.geturl(), 'content_type': content_type,
                      'last_modified': response.headers.get('Last-Modified')}
            if fields['last_modified'] is not None and len(fields['last_modified']) > 256:
                raise FetchError('oversized_header')
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        raise FetchError('download_failed_or_incomplete', cause_type='HTTPError', http_status=error.code) from None
    except urllib.error.URLError as error:
        reason = error.reason
        code = 'tls_certificate_verification_failed' if isinstance(reason, ssl.SSLCertVerificationError) else 'download_failed_or_incomplete'
        raise FetchError(code, cause_type=type(reason).__name__) from None
    except (OSError, http.client.HTTPException) as error:
        raise FetchError('download_failed_or_incomplete', cause_type=type(error).__name__) from None
    _publish(target, body)
    return _complete(root, body, {**receipt, **fields, 'passed': True, 'operation': 'downloaded_and_verified',
            'sha256': SHA256, 'bytes': len(body), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'transport': 'injected_test_transport' if injected else 'stdlib_https_no_redirect',
            'tls_verified': False if injected else True, 'trust_store': trust_store}, existing_json)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Project root; destination and URL are fixed')
    parser.add_argument('--out', type=Path, required=True, help='New receipt directory, never overwritten')
    args = parser.parse_args()
    try:
        args.out.mkdir(parents=True, exist_ok=False)
    except OSError:
        print(json.dumps({'passed': False, 'error': 'new_evidence_directory_required'}))
        return 1
    try:
        receipt = fetch(args.root)
    except (FetchError, OSError) as error:
        receipt = {'schema_version': 1, 'passed': False, 'error': str(error) if isinstance(error, FetchError) else 'file_operation_failed',
                   'paper_id': PAPER_ID, 'requested_url': URL, 'expected_sha256': SHA256,
                   'redistribution_permission': 'not_verified', 'model_execution': 'not_run',
                   'cause_type': getattr(error, 'cause_type', None), 'http_status': getattr(error, 'http_status', None)}
    (args.out / 'result.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': receipt['passed'], 'operation': receipt.get('operation'), 'error': receipt.get('error')}))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
