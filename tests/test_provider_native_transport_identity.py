"""Pure transport selection/accessor checks; no Native/auth/model/network."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from paper_alpha.storage import digest
from paper_alpha.server.provider_schema import ProviderConfig
from paper_alpha.server.provider_native_transport_identity import (
    native_transport, current_native_identity, native_profile_digest,
    authority_profile_digest, app_server_operation_supported,
)


class NativeTransportIdentityTests(unittest.TestCase):
    def config(self, **updates):
        return ProviderConfig(config_id='identity_test', executable='/private/tmp/fake-native',
            model='gpt-5.5', **updates)

    def test_exec_default_serialization_preserves_historical_shape(self):
        value = self.config()
        self.assertEqual(native_transport(value), 'exec')
        self.assertNotIn('native_transport', value.model_dump())
        self.assertEqual(self.config(native_transport='exec').model_dump(), value.model_dump())

    def test_app_server_selection_is_explicit_in_config_digest(self):
        value = self.config(native_transport='app_server')
        self.assertEqual(native_transport(value), 'app_server')
        self.assertEqual(value.model_dump()['native_transport'], 'app_server')
        self.assertNotEqual(digest(value.model_dump()), digest(self.config().model_dump()))

    def test_unknown_or_non_string_transport_and_fixture_adoption_rejected(self):
        for value in (None, True, 1, 'sdk', 'APP_SERVER'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native_transport({'mode': 'codex_cli', 'native_transport': value})
        with self.assertRaises(ValueError):
            native_transport({'mode': 'fixture', 'native_transport': 'app_server'})

    def test_exec_uses_original_identity_implementation(self):
        result = ({'exec': 'identity'}, {'exec': 'profile'})
        with patch('paper_alpha.server.provider_native._identity', return_value=result) as original:
            self.assertEqual(current_native_identity(self.config(), contract_id='research_provider_v2'), result)
        original.assert_called_once()
        self.assertEqual(original.call_args.kwargs, {'contract_id': 'research_provider_v2'})

    def test_app_server_rejects_v1_contract_before_business_identity(self):
        with self.assertRaises(ValueError):
            current_native_identity(self.config(native_transport='app_server'), contract_id='codex_cli_provider_v1')

    def test_fixture_cannot_request_a_current_native_identity(self):
        with self.assertRaises(ValueError):
            current_native_identity(self.config(mode='fixture'), contract_id='research_provider_v2')

    def test_exec_digest_requires_original_inner_template_and_exact_digest(self):
        value = {'native_inner_argv': {'template_version': 'native_inner_argv_v1'}, 'profile_digest': 'a'*64}
        self.assertEqual(native_profile_digest(value), 'a'*64)
        for changed in ({}, {**value, 'profile_digest': None}, {**value, 'profile_digest': True},
                        {**value, 'native_inner_argv': {'template_version': 'new'}},
                        {**value, 'profile_digest': 'A'*64}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                native_profile_digest(changed)

    def test_nested_app_server_digest_not_shadowed_by_top_level_fallback(self):
        value = {'identity_version': 2, 'transport_version': 'codex_app_server_business_stdio_v2',
                 'profile_digest': 'a'*64,
                 'profile': {'profile_version': 9, 'transport_version': 'codex_app_server_business_stdio_v2',
                             'profile_digest': 'b'*64}}
        with patch('paper_alpha.server.provider_app_server_business_identity.BUSINESS_DISPATCH_TRANSPORT_VERSION',
                   'codex_app_server_business_stdio_v2', create=True):
            self.assertEqual(native_profile_digest(value), 'b'*64)
            for key, replacement in (('identity_version', 1), ('identity_version', True),
                                     ('transport_version', 'codex_app_server_business_stdio_v1'),
                                     ('profile', {'profile_version': 9, 'transport_version': value['transport_version']})):
                changed = deepcopy(value); changed[key] = replacement
                with self.subTest(key=key, replacement=replacement), self.assertRaises(ValueError):
                    native_profile_digest(changed)
            changed = deepcopy(value); changed['profile']['profile_version'] = True
            with self.assertRaises(ValueError): native_profile_digest(changed)
            changed = deepcopy(value); changed['profile']['profile_version'] = 3
            with self.assertRaises(ValueError): native_profile_digest(changed)
            changed = deepcopy(value); changed['profile']['profile_version'] = 4
            with self.assertRaises(ValueError): native_profile_digest(changed)

    def test_fixture_digest_is_explicit_and_native_has_no_fallback(self):
        value = {'native_identity': {'contract_id': 'research_provider_v2', 'executable_sha256': 'a'*64}}
        self.assertEqual(authority_profile_digest(value, {'mode': 'fixture'}), digest(value))
        with self.assertRaises(ValueError):
            authority_profile_digest(value, {'mode': 'codex_cli'})

    def test_material_and_generation_scope_is_closed_only_for_new_transport(self):
        new = self.config(native_transport='app_server')
        for operation in ('extract_material', 'generate_candidates'):
            self.assertTrue(app_server_operation_supported(new, operation))
        for operation in ('repair_candidate', 'decide_next_action', 'unknown', None):
            self.assertFalse(app_server_operation_supported(new, operation))
            self.assertTrue(app_server_operation_supported(self.config(), operation))
