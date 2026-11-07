"""Pure source fixtures and an owned-file/fake-store provisioning adapter only."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import provision_local_acquisition as provision
from hydra_key_source import effective_key
from test_hydra_key_source import ASCII, UNICODE


class SourceTests(unittest.TestCase):
    def test_hydra_selects_only_main_apiKey_and_preserves_bytes(self):
        raw = f'main:\n  apiKey: "{UNICODE}"\nother:\n  apiKey: ignored\n'.encode()
        before = bytes(raw)
        self.assertEqual(provision.source_key('hydra', raw).decode(), effective_key(UNICODE))
        self.assertEqual(raw, before)

    def test_hydra_duplicate_placeholder_missing_invalid(self):
        for raw in (b'main: {apiKey: x, apiKey: y}', b'main: {apiKey: redacted}',
                    b'main: {apiKey: 42}', b'main: {}', b'main: {apiKey: "{OBF}!!!!"}',
                    b'main: {apiKey: x}\nmain: {apiKey: y}', b'!!python/object:foo {}',
                    b'x' * (provision.MAX_SOURCE_BYTES + 1)):
            with self.subTest(raw=raw[:100]), self.assertRaisesRegex(ValueError, '^Invalid credential source$'):
                provision.source_key('hydra', raw)

    def test_sab_preamble_quoted_add_only_and_no_admin_fallback(self):
        raw = b'__version__ = 19\n[misc]\napi_key = NEVER-SELECT-ADMIN\nnzb_key = "' + b'a'*32 + b'"\n[servers]\n[[fiction]]\nhost = fictional.invalid\n'
        self.assertEqual(provision.source_key('sab', raw), bytearray(b'a'*32))
        self.assertIn(b'NEVER-SELECT-ADMIN', raw)
        for value in (b'[misc]\napi_key = '+ b'a'*32,
                      b'[DEFAULT]\nnzb_key = '+b'a'*32+b'\n[misc]\napi_key = ignored',
                      b'[misc]\nNZB_KEY = '+b'a'*32,
                      b'[misc]\nnzb_key = redacted', b'[misc]\nnzb_key =',
                      b'[misc]\nnzb_key = '+b'a'*32+b'\nnzb_key = '+b'b'*32,
                      b'[misc]\nnzb_key = '+b'a'*32+b'\n[misc]\nnzb_key = '+b'b'*32):
            with self.assertRaises(ValueError):
                provision.source_key('sab', value)

    def test_provision_fake_store_preserves_source_and_zeroes_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'owned-synthetic.yml'
            raw = f'main:\n  apiKey: "{ASCII}"\n'.encode()
            source.write_bytes(raw)
            fake = Mock()
            captured = []
            def commit(handle, metadata, secret):
                self.assertEqual(bytes(secret), b'synthetic-caps-fixture')
                captured.append(secret)
                return provision.store.EntryReceipt('enter','stored','complete')
            with patch.object(provision.store,'open_store',return_value=fake), patch.object(provision.store,'lock_store'), patch.object(provision.store,'commit_entry',side_effect=commit):
                result = provision.provision(provider='hydra',source=source,account_id='synthetic',actor_id='11',profile_id='22',expires_at=provision.store.utc_now()+timedelta(days=1))
            self.assertEqual(result.outcome,'stored')
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(set(captured[0]), {0})
            fake.close.assert_called_once()

    def test_invalid_binding_expiry_never_reads_source(self):
        for actor, delta in (('0', 1), ('11',91), ('11',0)):
            with patch('builtins.open') as opened, self.assertRaises(provision.store.Refused):
                provision.provision(provider='hydra',source='never-open',account_id='synthetic',actor_id=actor,profile_id='22',expires_at=provision.store.utc_now()+timedelta(days=delta))
            opened.assert_not_called()
