"""Three native Enhance rounds; only external wire responses are synthetic."""
import uuid
import traceback
from shelf.portable_enhance_service import _DurablePorts
from urllib.parse import urlencode
from unittest.mock import patch

from shelf.category_registry import KEYS
from shelf.disclosure import canonical_bytes, read_json
from shelf.inference_adapters import HostedResponsesAdapter
from shelf.inference_transport import CheckedTransport, ProbePermit, PersonalPermit
from shelf.models import SynthesisRun, ProfileCabinetState
from shelf.portable_feedback_views import card_feedback_context, feedback_context
from shelf.source_authority import MUSICBRAINZ_ARTIST
from .test_inference_adapters import response_document
from .test_portable_feedback_views import NativeFeedbackCase
from . import test_portable_source_views as source_cases


class NativeThreeRoundTests(NativeFeedbackCase):
    # One uninterrupted synthetic session: do not renew authority between rounds.
    bound_socket_lifetime = 25
    compose_enhance = source_cases.NativeArtistSourceTests.compose_enhance
    policies = source_cases.NativeArtistSourceTests.policies
    configure = source_cases.NativeArtistSourceTests.configure
    grant = source_cases.NativeArtistSourceTests.grant

    def action(self, path, fields):
        response = self.http.post(path, urlencode({'csrfmiddlewaretoken': self.csrf(), **fields}),
            content_type='application/x-www-form-urlencoded', HTTP_ACCEPT='application/json',
            HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_three_native_rounds_feedback_undo_exact_input_and_persistent_publications(self):
        bodies, previews, trace = [], [], []

        def model_reply(transport, body, permit):
            if type(permit) is ProbePermit:
                permit.authorize(body)
                return canonical_bytes(response_document())
            self.assertIs(type(permit), PersonalPermit)
            with permit.authorize(body):
                self.assertEqual(body, previews[-1])
                submitted = read_json(read_json(body)['input'].encode())
                bodies.append(submitted)
                trace.append('inference')
                keys = [row['key'] for row in submitted['profile']['signals']]
                result = dict(schema='cabinet-inference-result/v1', associations=None,
                    candidate_assessments=[dict(candidate_key=row['key'], decision='retain',
                        signal_keys=keys, evidence_refs=row['source_roots'],
                        hypothesis='Synthetic association for handler validation.')
                        for row in submitted['candidates']])
                return canonical_bytes(response_document(result))

        def source_reply(operation, *, revalidate, **bounds):
            self.assertTrue(revalidate())
            self.assertEqual(operation.artist_name, 'Synthetic deliberate artist')
            trace.append(operation.source)
            row = {'id': '81000000-0000-4000-8000-000000000001'}
            document = ({'artists': [{**row, 'name': 'Synthetic discovered artist'}]}
                if operation.source == MUSICBRAINZ_ARTIST else
                {'recordings': [{**row, 'title': 'Synthetic discovered recording'}]})
            return 200, canonical_bytes(document), ()

        original_dispatch = _DurablePorts.dispatch
        def diagnose_dispatch(ports, *args, **kwargs):
            try:
                return original_dispatch(ports, *args, **kwargs)
            except Exception as error:
                traceback.print_tb(error.__traceback__)
                raise

        with patch.object(_DurablePorts, 'dispatch', diagnose_dispatch), patch.object(CheckedTransport, 'send', model_reply), patch(
                'shelf.discovery_transport.musicbrainz_authorized', side_effect=source_reply):
            # Validate the hosted adapter with its real permit, using only a fake
            # wire response. An isolated disclosure fixture cannot authorize it.
            self.service.adapter = HostedResponsesAdapter()
            del self.authority._disclosure_fixture_token
            self.put()
            self.confirm_probe()
            artist, recording = self.configure(), self.configure(False)
            self.assertEqual(self.grant(artist).status_code, 303)
            self.assertEqual(self.grant(recording, False).status_code, 303)
            self.assertEqual(self.post('/cabinet/taste', self.fields(category='artist',
                title='Synthetic deliberate artist')).status_code, 303)
            note = self.note_fields(category='artist', text='  First fictional note 雪  ')
            self.assertEqual(self.post('/cabinet/taste', note).status_code, 303)
            self.consent(self.profile_ref, classes=['anticipated_reactions',
                'experienced_enjoyment', 'raw_notes', 'free_text_entry'])
            prior_card = None
            for round_number in range(1, 4):
                if round_number == 2:
                    prior_card = self.current_card(current)
                    binding = next(row for row in prior_card['signals'] if row['signal'] == 'interest')
                    reaction = dict(profile_ref=self.profile_ref, target_ref=prior_card['target_ref'],
                        mutation_id=binding['mutation_id'], base_revision=str(binding['revision']),
                        operation='set', signal='interest', value='no')
                    self.assertEqual(self.post('/cabinet/feedback', reaction).status_code, 303)
                if round_number == 3:
                    # The same retained candidate has fresh native bindings. Undo
                    # restores the authored projection; no fabricated new rating.
                    binding = next(row for row in self.current_card(current)['signals']
                                   if row['signal'] == 'interest')
                    self.assertTrue(binding['can_undo'])
                    undo = dict(profile_ref=self.profile_ref, target_ref=prior_card['target_ref'],
                        mutation_id=binding['undo_mutation_id'], base_revision=str(binding['revision']),
                        operation='reverse', signal='interest', reverses_id=binding['event_ref'])
                    self.assertEqual(self.post('/cabinet/feedback', undo).status_code, 303)
                    updated = {**note, 'mutation_id': str(uuid.uuid4()), 'base_revision': '1',
                               'text': '  Changed fictional note é\n雪  '}
                    self.assertEqual(self.post('/cabinet/taste', updated).status_code, 303)
                    entries = feedback_context(self.authority, self.ref, self.profile_ref)['feedback_entries']
                    saved = next(row for row in entries if row['entry_ref'] == note['entry_ref'])
                    self.assertEqual(saved['signals'][0]['text'], updated['text'])
                start_fields = dict(profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                    client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
                run = self.action('/enhance/start', start_fields)
                run_ref = run['run_ref']
                trace.append('round-start')
                for _ in range(3):
                    preview = self.action(f'/enhance/runs/{run_ref}/preview', {})['disclosure']
                    if preview['kind'] == 'catalogue':
                        self.assertNotIn('fictional note', preview['query'])
                    else:
                        previews.append(preview['body'].encode())
                    confirmation = dict(preview_ref=preview['preview_ref'],
                        body_digest=preview['body_digest'], confirmed='yes')
                    status = self.action(f'/enhance/runs/{run_ref}/confirm', confirmation)
                    if status['published']:
                        break
                self.assertTrue(status['published'], {'round': round_number, 'status': status, 'trace': trace})
                self.assertEqual(status['publication_revision'], round_number)
                row = SynthesisRun.objects.get(pk=run_ref)
                self.assertEqual(row.attempts, 1)
                self.assertEqual({o['category'] for o in row.evidence['outcomes']}, set(KEYS))
                current = self.http.get(f'/enhance/current/{self.profile_ref}',
                    HTTP_ACCEPT='application/json', **self.headers).json()
                self.assertTrue(current['published'])
                self.assertEqual(current['publication_revision'], round_number)
                self.assertEqual({group['category'] for group in current['manifest']['categories']}, set(KEYS))
                self.assertEqual(self.http.get(f'/enhance/current/{self.profile_ref}',
                    HTTP_ACCEPT='application/json', **self.headers).json(), current)
                self.assertEqual(self.http.get('/cabinet', {'profile_ref': self.profile_ref},
                    **self.headers).status_code, 200)
                # Both confirmation replay and reload are reads of an existing
                # publication: they must never cause another paid attempt.
                self.assertEqual(self.action(f'/enhance/runs/{run_ref}/confirm', confirmation)
                    ['publication_revision'], round_number)
                self.assertEqual(len(bodies), round_number)
                self.assertEqual(SynthesisRun.objects.get(pk=run_ref).attempts, 1)
            self.assertEqual(SynthesisRun.objects.count(), 3)
            self.assertEqual(ProfileCabinetState.objects.get().publication_revision, 3)
            self.assertNotEqual(bodies[0]['profile'], bodies[1]['profile'])
            self.assertNotEqual(bodies[1]['profile'], bodies[2]['profile'])
            self.assertEqual(bodies[0]['profile']['raw_notes'][0]['text'], note['text'])
            self.assertEqual(bodies[2]['profile']['raw_notes'][0]['text'], updated['text'])
            self.assertEqual(trace.count('inference'), 3)
            for segment in range(3):
                start = [i for i, value in enumerate(trace) if value == 'round-start'][segment]
                stop = trace.index('inference', start)
                self.assertEqual(len(trace[start + 1:stop]), 2)

    def current_card(self, current):
        target = next(iter(current['reaction_targets'].values()))
        return card_feedback_context(self.authority, self.ref, self.profile_ref, target_ref=target)
