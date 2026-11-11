"""Finite source configuration, never a test that declares live readiness."""
from unittest import TestCase
from unittest.mock import patch
from shelf import event_source_registry as registry, discovery_transport, event_scraping
from shelf.discovery_sources import SourceError
from shelf.tests.test_discovery_transport import NOW, event_document
import json


class EventSourceRegistryTests(TestCase):
    def test_finite35_origins_metadata_only_and_no_unimplemented_acquisition(self):
        self.assertEqual(len(registry.SOURCES), 35)
        controls = registry.source_controls()
        self.assertEqual(len(controls), 35)
        self.assertTrue(all(not callable(value) for row in controls for value in row.values()))
        self.assertEqual(registry.SOURCES['triple_j'].origin_groups,
                         registry.SOURCES['double_j'].origin_groups)
        with self.assertRaises(TypeError): registry.SOURCES['arbitrary'] = None
        with patch.object(discovery_transport, '_send') as send:
            for source in ('halftix', 'la_sirene', 'hamer_hall', 'ngv'):
                result = event_scraping.collect(source, now=NOW)
                self.assertEqual(result.candidates, ())
                self.assertIn(result.status, ('unavailable', 'permission-required'))
            send.assert_not_called()
        for source in ('https://evil.invalid', 'arbitrary', [], None):
            with self.subTest(source=source), self.assertRaises(SourceError): registry.get_source(source)

    def test_legacy_and_generic_cache_keys_are_identical(self):
        for alias, source in registry.LEGACY_ALIASES.items():
            self.assertEqual(registry.source_cache_key(source), (alias, '', ''))

    def test_moshtix_reuses_one_row_normal_query_and_rejects_excess(self):
        from shelf.discovery_sources import fetch_public
        raw = json.dumps(event_document()).encode()
        with patch.object(discovery_transport, 'events', return_value=(200, raw)) as send:
            generic = event_scraping.collect('moshtix', now=NOW)
            legacy = fetch_public('events', '', '', NOW)
        self.assertEqual(generic.candidates, legacy.candidates)
        self.assertEqual(len(generic.candidates), 1)
        self.assertEqual(send.call_args.args, (NOW,))
        document = event_document()
        document['data']['viewer']['getEvents']['items'] *= 2
        with patch.object(discovery_transport, 'events', return_value=(200,json.dumps(document).encode())):
            self.assertEqual(event_scraping.collect('moshtix', now=NOW).status, 'invalid')
