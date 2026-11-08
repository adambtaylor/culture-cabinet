"""Exact personal-body evidence through the real dormant isolated composition."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from shelf import disclosure, inference_body, portable_disclosure as pd
from shelf.inference_adapters import HostedResponsesAdapter, MODEL, REASONING
from shelf.inference_transport import TransportFault
from shelf.models import PortableDisclosurePreview
from .test_disclosure import resign, source
from .test_profile_snapshot import SnapshotCase


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


class InferenceBodyTests(SnapshotCase):
    def record(self, key='source:book:a', *, facts=None):
        record = source(key, facts=facts)
        record['candidate'].update(observed_at=stamp(self.clock.wall - timedelta(seconds=1)),
                                   valid_until=stamp(self.expires))
        for binding in record['fields']:
            binding['observation'].update(observed_at=stamp(self.clock.wall - timedelta(seconds=1)),
                                          expires_at=stamp(self.expires))
            binding['rights'].update(checked_at=stamp(self.clock.wall - timedelta(seconds=1)),
                                     expires_at=stamp(self.expires))
            resign(record, binding)
        return record

    def collection(self, records=None, *, observations=None, policies=None, expires_at=None):
        return self.disclosure.fixture_collection([self.record()] if records is None else records,
            {'observation:1': {'revision': 7, 'digest': 'c' * 64}} if observations is None else observations,
            {'policy:1': {'revision': 3}} if policies is None else policies,
            expires_at=self.expires if expires_at is None else expires_at)

    def encode(self, collection, **overrides):
        view = self.view(self.profile_ref)
        from shelf.profile_snapshot import capture
        arguments = dict(snapshot=capture(view), grants=view.grants, context=view.context,
                         collection=collection, now=self.clock.wall)
        arguments.update(overrides)
        return inference_body.serialize(**arguments)

    def preview(self, collection):
        return pd.preview(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), collection_ref=collection.ref)

    def rejected(self, code, collection, **overrides):
        with self.assertRaises(disclosure.DisclosureError) as caught:
            self.encode(collection, **overrides)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_one_codec_yields_exact_preview_reconstruction_and_isolated_capture_bytes(self):
        self.event(choice='intrigued')
        collection = self.collection()
        encoded = self.encode(collection)
        receipt = self.preview(collection)
        self.assertEqual(receipt['body'], encoded.body)
        self.assertEqual(pd.revalidate(receipt['preview_ref'], self.authority, self.ref), encoded.body)
        self.assertEqual(pd.isolated_capture(receipt['preview_ref'], self.authority, self.ref), encoded.body)
        self.assertEqual(receipt['body_digest'], hashlib.sha256(encoded.body).hexdigest())
        self.assertEqual(encoded.serializer_version, inference_body.SERIALIZER_VERSION)
        self.assertFalse(receipt['run_ready'])
        self.assertEqual(self.fake.calls, 1)  # Sole neutral setup probe, never personal inference.

    def test_denied_note_changes_only_local_revision_and_stales_old_preview(self):
        self.event(choice='intrigued', note='DENIED-NOTE-CANARY-A')
        collection = self.collection()
        first = self.preview(collection)
        revision = self.snapshot()['profile_revision_digest']
        self.history.history[0]['note'] = 'DENIED-NOTE-CANARY-B with different length'
        self.history.save(update_fields=['history'])
        self.fault('PREVIEW_STALE', pd.isolated_capture, first['preview_ref'], self.authority, self.ref)
        second = self.preview(collection)
        self.assertEqual(first['body'], second['body'])
        self.assertNotEqual(revision, self.snapshot()['profile_revision_digest'])
        self.assertNotIn(b'DENIED-NOTE-CANARY', second['body'])
        self.assertIn('raw_notes', second['omitted_groups'])

    def test_granted_note_exact_text_and_fixed_provider_contract_are_preserved(self):
        self.allow_notes()
        text = '  e\u0301 / \u00e9\t\r\n\u96ea  '
        self.event(choice='intrigued', note=text)
        encoded = self.encode(self.collection())
        wire = json.loads(encoded.body)
        value = json.loads(wire['input'])
        self.assertEqual(value['profile']['raw_notes'][0]['text'], text)
        self.assertEqual(value['profile']['raw_notes'][0]['reaction'], 'intrigued')
        self.assertEqual((wire['model'], wire['reasoning']), (MODEL, {'effort': REASONING}))
        self.assertEqual((wire['store'], wire['stream'], wire['background'], wire['tools']), (False, False, False, []))
        self.assertEqual(wire['max_output_tokens'], 2048)
        self.assertTrue(wire['text']['format']['strict'])
        self.assertEqual(encoded.body, disclosure.canonical_bytes(wire))

    def test_private_observation_policy_and_record_refs_never_enter_body_or_storage(self):
        fact = {'id': 'private-fact-id', 'property': 'dimension', 'value': 'Public granted fact',
                'observation_ref': 'PRIVATE-OBSERVATION-CANARY'}
        record = self.record(facts=[fact])
        record['record_ref'] = record['candidate']['rights_ref'] = 'PRIVATE-RIGHTS-CANARY'
        record['candidate']['source_roots'] = ['PRIVATE-OBSERVATION-CANARY']
        for binding in record['fields']:
            binding['observation']['ref'] = 'PRIVATE-OBSERVATION-CANARY'
            binding['rights']['policy_ref'] = 'PRIVATE-POLICY-CANARY'
            resign(record, binding)
        collection = self.collection([record],
            observations={'PRIVATE-OBSERVATION-CANARY': {'revision': 7, 'digest': 'c' * 64}},
            policies={'PRIVATE-POLICY-CANARY': {'revision': 3}})
        receipt = self.preview(collection)
        view = self.view(self.profile_ref)
        for private in ('PRIVATE-OBSERVATION-CANARY', 'PRIVATE-RIGHTS-CANARY', 'PRIVATE-POLICY-CANARY',
                        'private-fact-id', view.installation_id, view.actor_ref, view.subject_ref,
                        view.profile_ref, view.profile_revision_digest, self.ref):
            self.assertNotIn(private.encode(), receipt['body'])
        projected = json.loads(json.loads(receipt['body'])['input'])['candidates'][0]
        self.assertEqual(projected['facts'][0]['id'], 'fact-1-1')
        self.assertEqual(projected['facts'][0]['observation_ref'], 'evidence-1')
        row = PortableDisclosurePreview.objects.get()
        stored = json.dumps({'refs': row.input_refs, 'bindings': row.bindings, 'groups': row.omitted_groups})
        self.assertNotIn('Public granted fact', stored)
        self.assertNotIn('PRIVATE-OBSERVATION-CANARY', stored)

    def test_zero_signals_are_broad_but_empty_or_fully_denied_candidates_are_errors(self):
        collection = self.collection()
        value = json.loads(json.loads(self.encode(collection).body)['input'])
        self.assertEqual(value['profile']['signals'], [])
        self.assertEqual(value['profile']['mode'], 'broad')
        self.assertEqual(len(value['candidates']), 1)
        self.rejected('NO_GROUNDED_CANDIDATES', self.collection([]))
        record = self.record()
        record['fields'][0]['rights']['disposition'] = 'unknown'
        self.rejected('NO_GROUNDED_CANDIDATES', self.collection([record]))

    def test_expired_stale_or_corrupt_collection_never_yields_a_capture(self):
        collection = self.collection()
        self.rejected('PREVIEW_STALE', replace(collection, expires_at=self.clock.wall))
        self.rejected('CONTRACT_INVALID', replace(collection, collection_digest='0' * 64))
        self.rejected('CONTRACT_INVALID', replace(collection, _material='not-bytes'))
        duplicate = b'{"records":[],"records":[],"observations":{},"policies":{}}'
        self.rejected('CONTRACT_INVALID', replace(collection, _material=duplicate,
            collection_digest=hashlib.sha256(duplicate).hexdigest()))
        stale = self.collection(observations={'observation:1': {'revision': 9, 'digest': 'c' * 64}})
        self.rejected('PREVIEW_STALE', stale)
        record = self.record()
        record['fields'][0]['rights']['expires_at'] = stamp(self.clock.wall)
        self.rejected('NO_GROUNDED_CANDIDATES', self.collection([record]))
        self.assertFalse(PortableDisclosurePreview.objects.exists())

    def test_tainted_pool_unknown_ancestry_and_foreign_scope_are_refused(self):
        self.event(choice='intrigued', note='DENIED-QUERY-CANARY')
        note = next(n for n in self.snapshot()['evidence'] if n['basis'] == 'feedback_note')
        record = self.record()
        record['direct_parent_ids'] = [note['id']]
        self.rejected('AUTHORITY_DENIED', self.collection([record]))
        record['direct_parent_ids'] = ['unknown-node']
        self.rejected('AUTHORITY_DENIED', self.collection([record]))
        snapshot = self.snapshot()
        snapshot['installation_id'] = '00000000-0000-0000-0000-000000000099'
        self.rejected('AUTHORITY_DENIED', self.collection(), snapshot=snapshot)

    def test_normal_or_unsealed_collection_and_wrong_provider_config_are_unavailable(self):
        collection = self.collection()
        self.rejected('CAPABILITY_UNAVAILABLE', {'records': collection.records})
        context = self.view(self.profile_ref).context
        context['isolated_fixture'] = False
        self.rejected('CAPABILITY_UNAVAILABLE', collection, context=context)
        context['isolated_fixture'] = True
        context['adapter_version'] = 'different-build'
        self.rejected('AUTHORITY_DENIED', collection, context=context)
        self.assertEqual(self.fake.calls, 1)

    def test_category_and_actual_wire_byte_caps_are_checked_without_truncation(self):
        self.rejected('LIMIT_EXCEEDED', self.collection([self.record(f'source:book:{i}') for i in range(21)]))
        self.allow_notes()
        for i in range(40):
            self.event(choice='intrigued', note='"' * 499 + chr(0x410 + i),
                       card=self.card(f'source:book:note-{i}'))
        facts = [{'id': f'f{i}', 'property': 'quoted', 'value': '"' * 1200,
                  'observation_ref': 'observation:1'} for i in range(40)]
        self.rejected('LIMIT_EXCEEDED', self.collection([self.record(facts=facts)]))
        self.assertFalse(PortableDisclosurePreview.objects.exists())

    def test_personal_codec_does_not_widen_neutral_transport_or_persist_prompt(self):
        self.event(choice='intrigued')
        encoded = self.encode(self.collection())
        adapter = HostedResponsesAdapter()
        with patch.object(adapter.transport, 'send') as send:
            with self.assertRaises(TransportFault):
                adapter.generate(encoded.body, None)
        send.assert_not_called()
        self.assertFalse(PortableDisclosurePreview.objects.exists())
        self.assertEqual(self.fake.calls, 1)

    def test_catalogue_codec_binds_current_purpose_and_all_categories_without_denied_notes(self):
        from shelf import grant_authority as ga
        from shelf.enhance_contract import EnhanceBindings, RegistryView, digest
        from shelf.profile_snapshot import capture
        self.consent(self.profile_ref, purpose='catalogue_lookup', classes=self.classes)
        self.event(choice='intrigued', note='DENIED-CATALOGUE-NOTE-A')
        registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings'))
        def current_plan():
            view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
                connection_ref=str(self.config.pk), purpose='catalogue_lookup')
            snapshot = capture(view)
            bindings = EnhanceBindings(view.installation_id, view.actor_ref, view.profile_ref,
                view.profile_revision_digest, view.policy_epoch, view.restriction_watermark,
                registry.revision, view.context['config_revision'], digest(disclosure.canonical_bytes(view.grants)),
                digest(disclosure.canonical_bytes(view.connection_binding)))
            plan = inference_body.serialize_catalogue(snapshot, view.grants, view.context, registry,
                now=self.clock.wall, plan_ref='server-query-plan', bindings=bindings)
            return plan, view, snapshot, bindings
        first, _, _, _ = current_plan()
        self.history.history[0]['note'] = 'DENIED-CATALOGUE-NOTE-B'
        self.history.save(update_fields=['history'])
        second, view, snapshot, bindings = current_plan()
        self.assertEqual(first.body, second.body)
        self.assertNotIn(b'DENIED-CATALOGUE', second.body)
        self.assertEqual(tuple(row['category'] for row in json.loads(second.body)['queries']), registry.categories)
        self.assertNotEqual(first.bindings.snapshot_revision, second.bindings.snapshot_revision)
        wrong_purpose = view.context
        wrong_purpose['purpose'] = 'inference'
        with self.assertRaises(disclosure.DisclosureError) as caught:
            inference_body.serialize_catalogue(snapshot, view.grants, wrong_purpose, registry,
                now=self.clock.wall, plan_ref='server-query-plan', bindings=bindings)
        self.assertEqual(caught.exception.code, 'AUTHORITY_DENIED')


class NormalBodyAuthorityTests(SnapshotCase):
    def test_normal_context_cannot_use_fixture_collection_or_a_caller_flag(self):
        from shelf.profile_snapshot import capture
        view = self.view(self.profile_ref)
        collection = self.disclosure.fixture_collection([], {}, {}, expires_at=self.expires)
        context = dict(view.context, isolated_fixture=False)
        with self.assertRaises(disclosure.DisclosureError) as caught:
            inference_body.serialize(capture(view), view.grants, context, collection, now=self.clock.wall)
        self.assertEqual(caught.exception.code, 'CAPABILITY_UNAVAILABLE')
