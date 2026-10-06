"""Isolated synthetic transport tests; fixtures are never the original paper."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts import fetch_demo_paper as tool


PDF_FIXTURE = b'%PDF-1.7\nexplicitly synthetic fixture, not an arXiv document\n'
JSON_FIXTURE = b'{"fixture":"synthetic extracted text, not original paper"}\n'


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=PDF_FIXTURE, *, headers=None, url=tool.URL):
        super().__init__(body)
        self.headers = headers if headers is not None else {'Content-Type': 'application/pdf', 'Content-Length': str(len(body)), 'Last-Modified': 'fixture'}
        self.url = url

    def geturl(self):
        return self.url


class Transport:
    def __init__(self, response=None):
        self.response = response if response is not None else Response()
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        return self.response


class FetchDemoPaperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pdf = self.root / tool.RELATIVE_TARGET
        self.extracted = self.root / tool.JSON_TARGET
        self.patches = [patch.object(tool, 'SHA256', hashlib.sha256(PDF_FIXTURE).hexdigest()),
                        patch.object(tool, 'JSON_SHA256', hashlib.sha256(JSON_FIXTURE).hexdigest()),
                        patch.object(tool, '_paper_json', return_value=(JSON_FIXTURE, 'synthetic-test-extractor'))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_download_and_exact_extraction_publish_are_explicit_test_transport(self):
        transport = Transport()
        result = tool.fetch(self.root, opener=transport)
        self.assertTrue(result['passed'])
        self.assertEqual(self.pdf.read_bytes(), PDF_FIXTURE)
        self.assertEqual(self.extracted.read_bytes(), JSON_FIXTURE)
        self.assertEqual(transport.requests, [(tool.URL, tool.SOCKET_TIMEOUT)])
        self.assertEqual(result['transport'], 'injected_test_transport')
        self.assertFalse(result['tls_verified'])
        self.assertFalse(result['authorship_authenticated'])
        self.assertEqual(result['redistribution_permission'], 'not_verified')
        self.assertFalse(list(self.pdf.parent.glob('.paper-bootstrap-*.tmp')))

    def test_matching_existing_files_do_not_use_network(self):
        tool.fetch(self.root, opener=Transport())
        transport = Transport()
        result = tool.fetch(self.root, opener=transport)
        self.assertEqual(result['operation'], 'verified_existing')
        self.assertEqual(result['json_operation'], 'verified_existing')
        self.assertIsNone(result['tls_verified'])
        self.assertEqual(transport.requests, [])

    def test_wrong_existing_pdf_rejected_without_overwrite_or_network(self):
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b'%PDF- existing wrong fixture')
        transport = Transport()
        with self.assertRaises(tool.FetchError):
            tool.fetch(self.root, opener=transport)
        self.assertEqual(self.pdf.read_bytes(), b'%PDF- existing wrong fixture')
        self.assertEqual(transport.requests, [])

    def test_wrong_existing_json_rejected_before_download(self):
        self.extracted.parent.mkdir(parents=True)
        self.extracted.write_bytes(b'{"wrong":true}')
        transport = Transport()
        with self.assertRaises(tool.FetchError):
            tool.fetch(self.root, opener=transport)
        self.assertEqual(transport.requests, [])
        self.assertFalse(self.pdf.exists())

    def test_existing_matching_pdf_allows_missing_json_recovery(self):
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(PDF_FIXTURE)
        transport = Transport()
        result = tool.fetch(self.root, opener=transport)
        self.assertEqual(result['json_operation'], 'extracted_and_verified')
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.extracted.read_bytes(), JSON_FIXTURE)

    def test_wrong_download_or_prefix_never_published(self):
        for body in (b'%PDF- wrong synthetic bytes', b'not a pdf'):
            with self.subTest(body=body):
                with self.assertRaises(tool.FetchError):
                    tool.fetch(self.root, opener=Transport(Response(body)))
                self.assertFalse(self.pdf.exists())

    def test_wrong_extraction_sha_never_published_and_recovery_reuses_pdf(self):
        # Failure occurs after correct PDF publication, before extracted final name.
        with patch.object(tool, '_paper_json', side_effect=tool.FetchError('fixed_paper_json_sha256_mismatch')):
            with self.assertRaises(tool.FetchError):
                tool.fetch(self.root, opener=Transport())
        self.assertEqual(self.pdf.read_bytes(), PDF_FIXTURE)
        self.assertFalse(self.extracted.exists())
        self.assertTrue(tool.fetch(self.root, opener=Transport())['passed'])

    def test_defensive_final_extraction_check_rejects_wrong_helper_bytes(self):
        with patch.object(tool, '_paper_json', return_value=(b'wrong fixture', 'test-only')):
            with self.assertRaisesRegex(tool.FetchError, 'json_sha256_mismatch'):
                tool.fetch(self.root, opener=Transport())
        self.assertFalse(self.extracted.exists())

    def test_bad_status_url_type_and_lengths_rejected(self):
        responses = [Response(url='https://example.invalid/paper.pdf'),
                     Response(headers={'Content-Type': 'text/html'}),
                     Response(headers={'Content-Type': 'application/pdf', 'Content-Length': '-1'}),
                     Response(headers={'Content-Type': 'application/pdf', 'Content-Length': str(tool.MAX_BYTES + 1)}),
                     Response(headers={'Content-Type': 'application/pdf', 'Content-Length': str(len(PDF_FIXTURE) + 1)})]
        status = Response(); status.status = 302; responses.append(status)
        for response in responses:
            with self.subTest(url=response.url, headers=response.headers):
                with self.assertRaises(tool.FetchError):
                    tool.fetch(self.root, opener=Transport(response))
                self.assertFalse(self.pdf.exists())

    def test_stream_size_and_time_budgets_rejected(self):
        with patch.object(tool, 'MAX_BYTES', 8):
            with self.assertRaises(tool.FetchError):
                tool.fetch(self.root, opener=Transport(Response(headers={'Content-Type': 'application/pdf'})))
        with patch.object(tool.time, 'monotonic', side_effect=[0, tool.TOTAL_SECONDS + 1]):
            with self.assertRaises(tool.FetchError):
                tool.fetch(self.root, opener=Transport())
        self.assertFalse(self.pdf.exists())

    def test_incomplete_read_is_controlled_failure(self):
        response = Response()
        response.read = lambda _: (_ for _ in ()).throw(http.client.IncompleteRead(b'partial', 20))
        with self.assertRaisesRegex(tool.FetchError, 'download_failed_or_incomplete'):
            tool.fetch(self.root, opener=Transport(response))
        self.assertFalse(self.pdf.exists())

    def test_safe_tls_diagnostics_and_locked_roots_do_not_disable_validation(self):
        import ssl
        context, name = tool._ssl_context()
        self.assertEqual(name, 'locked_certifi_bundle')
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        transport = Transport()
        transport.open = lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError(ssl.SSLCertVerificationError('test-only')))
        with self.assertRaisesRegex(tool.FetchError, 'tls_certificate_verification_failed') as caught:
            tool.fetch(self.root, opener=transport)
        self.assertEqual(caught.exception.cause_type, 'SSLCertVerificationError')
        self.assertFalse(self.pdf.exists())

    def test_redirects_rejected(self):
        with self.assertRaisesRegex(tool.FetchError, 'redirect_rejected'):
            tool.NoRedirect().redirect_request(None, None, 301, '', {}, tool.URL)

    def test_linked_or_hardlinked_target_rejected(self):
        self.pdf.parent.mkdir(parents=True)
        other = self.root / 'fixture.pdf'; other.write_bytes(PDF_FIXTURE)
        self.pdf.symlink_to(other)
        with self.assertRaises(tool.FetchError):
            tool.fetch(self.root, opener=Transport())
        self.pdf.unlink()
        import os
        os.link(other, self.pdf)
        with self.assertRaises(tool.FetchError):
            tool.fetch(self.root, opener=Transport())

    def test_extraction_implementation_uses_frozen_metadata_and_checks_json_hash(self):
        # Fake reader exercises construction, not PDF semantics or original source.
        class Page:
            def extract_text(self):
                return 'explicitly synthetic page'
        reader = type('Reader', (), {'pages': [Page()]})()
        self.patches[2].stop()
        try:
            with patch('pypdf.PdfReader', return_value=reader), patch.object(tool, 'JSON_SHA256', '0' * 64):
                with self.assertRaisesRegex(tool.FetchError, 'json_sha256_mismatch'):
                    tool._paper_json(PDF_FIXTURE)
        finally:
            self.patches[2].start()


if __name__ == '__main__':
    unittest.main()
