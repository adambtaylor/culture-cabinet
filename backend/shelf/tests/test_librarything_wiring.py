"""Consumer refusal for the released synthetic LibraryThing producer seam.

This is not a native LibraryThing integration or evidence of live custody.
Producer shape/normalization cases belong to Catalogue's adapter tests.
"""
from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase

from shelf.enhance_contract import EnhanceFault
from shelf.librarything_adapter import (librarything_use_decision,
    normalize_synthetic_librarything, synthetic_librarything_fixture)
from shelf.source_adapters import SOURCES, registered_source
from shelf.source_authority import NATIVE_SETUP
from shelf.source_collection import SourceCollection


class LibraryThingWiringTests(SimpleTestCase):
    def no_effects(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target in ('shelf.credential_store.open_store', 'shelf.credential_store.read_record',
                       'shelf.secret_leases.MemoryCredentials.describe',
                       'shelf.secret_leases.MemoryCredentials.ephemeral',
                       'shelf.librarything_smoke.run_live', 'shelf.librarything_smoke._send',
                       'socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen'):
            stack.enter_context(patch(target, side_effect=AssertionError('No credential or provider effect')))

    def test_synthetic_producer_cannot_enter_native_collection_or_model_pool(self):
        self.no_effects()
        sources_before = tuple(source.key for source in SOURCES)
        setup_before = dict(NATIVE_SETUP)
        reply = normalize_synthetic_librarything(synthetic_librarything_fixture())
        self.assertTrue(reply.suggestions)
        self.assertFalse(reply.model_eligible)
        self.assertEqual(reply.evidence_kind, 'synthetic_fixture_not_live')
        # A successful fixture normalization is deliberately not native source
        # authority. The real collection constructor rejects it before a backend
        # could look up credentials or issue a provider request.
        with self.assertRaises(EnhanceFault) as error:
            SourceCollection((('book', ('librarything',)),), {})
        self.assertEqual(error.exception.code, 'CAPABILITY_UNAVAILABLE')
        with self.assertRaises(EnhanceFault) as error:
            registered_source('librarything')
        self.assertEqual(error.exception.code, 'CAPABILITY_UNAVAILABLE')
        self.assertEqual(tuple(source.key for source in SOURCES), sources_before)
        self.assertEqual(NATIVE_SETUP, setup_before)
        self.assertNotIn('librarything', NATIVE_SETUP)

    def test_use_specific_holds_never_promote_normalized_suggestions(self):
        self.no_effects()
        reply = normalize_synthetic_librarything(synthetic_librarything_fixture())
        for use, code in (
            ('model_context', 'LIBRARYTHING_AI_WRITTEN_PERMISSION_REQUIRED'),
            ('grounding', 'LIBRARYTHING_AI_WRITTEN_PERMISSION_REQUIRED'),
            ('diagnostic', 'ACTUAL_DIAGNOSTIC_CUSTODY_AND_EXECUTION_UNPROVED'),
            ('native_collection', 'NATIVE_OPERATION_RIGHTS_AND_BINDING_UNAVAILABLE'),
            ('display', 'NATIVE_OPERATION_RIGHTS_AND_BINDING_UNAVAILABLE'),
        ):
            with self.subTest(use=use):
                decision = librarything_use_decision(use)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, code)
                self.assertFalse(reply.model_eligible)
        self.assertTrue(librarything_use_decision('synthetic_contract').allowed)
        self.assertFalse(reply.model_eligible)
