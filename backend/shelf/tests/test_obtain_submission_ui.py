"""Actual rendered candidate flow with trusted injected fixtures and CSRF."""
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from shelf import obtain_acquisition as acquire, obtain_preview as preview, obtain_runtime
from shelf.acquisition_contract import ConfirmedSelection
from shelf.acquisition_pipeline import AdapterUnavailable, Completion, Evidence, ProviderJob, Snapshot, StagedFile
from shelf.authority import SELECTION_KEY
from shelf.models import AcquisitionJob, FeedbackEvent, Grant, ObtainTransfer
from shelf.tests.test_obtain_acquisition import FixtureBackend, FixtureRetriever
from shelf.tests.test_obtain_ui import Page, fake_backend, listing
from shelf.tests.test_obtain_retrieval import NZB
from shelf.tests.test_saved import SavedFixture


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SubmissionWebTests(SavedFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.backend = FixtureBackend()
        self.retriever = FixtureRetriever()
        self.retriever.payload = NZB
        # Exercise the existing search -> exact listing -> local selection bridge.
        with patch.object(preview, 'search_backend', return_value=fake_backend(listing())):
            start = f'{self.base}{self.saved.pk}/preview/'
            self.client.get(start)
            result = self.client.post(start + 'search/')
        rows = self.client.get(result.url).context['obtain_preview']['results']
        choice = self.client.get(rows[0]['review_url'])
        form = choice.context['obtain_result_form']
        response = self.client.post(choice.context['obtain_result_confirm_url'],
            dict(form.initial, authorized_content='on', storage_check='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.backend.calls, [])
        self.job = AcquisitionJob.objects.get()
        self.status_url = response.url
        self.review_url = self.status_url + 'submit/review/'
        self.submit_url = self.status_url + 'submit/'
        self.context = acquire.OperatorContext(1, 1, 'offline_test', enabled=True)
        snapshot = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snapshot, ConfirmedSelection('tmdb:movie:11', 'film',
            snapshot.edition, snapshot.storage, True, True, True, True, True, True))
        self.services = obtain_runtime.SubmissionServices(self.context, self.evidence, 20000,
                                                         self.retriever, self.backend)
        self.factory_patch = patch.object(obtain_runtime, 'submission_factory', return_value=self.services)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    def prepare_review(self, **data):
        return self.client.post(self.review_url, data)

    def submission_data(self, response):
        form = Page(response).form(self.submit_url)
        data = {name: field['value'] for name, field in form['fields'].items()
                if field.get('type') == 'hidden'}
        return data | {name: 'on' for name in ('exact_selection', 'rights', 'scope', 'size', 'duplicates')}

    def test_full_csrf_flow_gets_never_enqueue_and_double_confirm_sends_once(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        status = self.client.get(self.status_url)
        csrf = Page(status).form(self.review_url)['fields']['csrfmiddlewaretoken']['value']
        for url in (self.review_url, self.submit_url):
            self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(self.review_url).status_code, 403)
        review = self.prepare_review(csrfmiddlewaretoken=csrf)
        self.assertContains(review, '12345 bytes')
        self.assertContains(review, '20000 bytes')
        self.assertContains(review, 'Full film')
        self.assertContains(review, self.job.edition)
        self.assertContains(review, 'legal right')
        self.assertContains(review, 'Hydra ID')
        self.assertEqual(self.backend.calls, [])
        data = self.submission_data(review)
        self.assertEqual(self.client.post(self.submit_url, data | {'csrfmiddlewaretoken': ''}).status_code, 403)
        self.assertEqual(self.client.post(self.submit_url, data | {'rights': ''}).status_code, 409)
        self.assertEqual(self.backend.calls, [])
        for _ in range(2):
            self.assertEqual(self.client.post(self.submit_url, data).status_code, 302)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.retriever.calls), 2)
        status = self.client.get(self.status_url)
        self.assertContains(status, 'queued')
        self.assertContains(status, 'Completion and local availability are unknown')
        self.assertNotContains(status, 'No remote transfer has started')
        self.assertNotContains(status, 'Downloads are disabled')
        self.assertEqual(FeedbackEvent.objects.count(), 0)

    def test_unconfigured_factory_refuses_and_requests_cannot_manufacture_evidence(self):
        self.factory_patch.stop()
        self.assertEqual(self.prepare_review().status_code, 409)
        for extra in ({'enabled': 'true'}, {'authorized_content': 'true'}, {'grant': 'owner'},
                      {'max_bytes': '20000'}, {'path': '/fiction/file'}, {'url': 'http://fiction.invalid/'}):
            self.assertEqual(self.prepare_review(**extra).status_code, 400)
        self.assertEqual(self.retriever.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_current_profile_ownership_before_factory_and_revocation_during_review(self):
        self.client.force_login(self.other)
        self.assertEqual(self.prepare_review().status_code, 404)
        self.factory.assert_not_called()
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': 1})
        original = self.retriever.retrieve
        def revoke(exact):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return original(exact)
        with patch.object(self.retriever, 'retrieve', side_effect=revoke):
            self.assertEqual(self.prepare_review().status_code, 404)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_expiry_during_review_prevents_confirmation(self):
        original = self.retriever.retrieve
        def expire(exact):
            from shelf.models import ObtainPreview
            ObtainPreview.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
            return original(exact)
        with patch.object(self.retriever, 'retrieve', side_effect=expire):
            self.assertEqual(self.prepare_review().status_code, 409)
        self.assertEqual(self.backend.calls, [])

    def test_expiry_after_review_prevents_enqueue(self):
        from shelf.models import ObtainPreview
        review = self.prepare_review()
        ObtainPreview.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(self.submit_url, self.submission_data(review)).status_code, 409)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_tampered_token_changed_nzb_and_missing_acknowledgement(self):
        review = self.prepare_review()
        data = self.submission_data(review)
        self.assertEqual(self.client.post(self.submit_url, data | {'token': 'forged'}).status_code, 409)
        self.retriever.payload = NZB + b'\n'
        self.assertEqual(self.client.post(self.submit_url, data).status_code, 409)
        self.assertEqual(self.backend.calls, [])
        self.retriever.payload = NZB
        self.backend.error = AdapterUnavailable('synthetic lost reply')
        self.assertEqual(self.client.post(self.submit_url, data).status_code, 302)
        self.assertEqual(self.client.post(self.submit_url, data).status_code, 302)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertContains(self.client.get(self.status_url), 'The enqueue outcome is unknown')
        self.assertEqual(self.client.get(self.status_url + 'local-handoff/').status_code, 409)

    def test_verified_local_receipt_exports_trusted_metadata_without_playing_or_reading(self):
        review = self.prepare_review()
        self.client.post(self.submit_url, self.submission_data(review))
        transfer = ObtainTransfer.objects.get(job=self.job)
        data = b'Synthetic bytes only; no real media or provider content.'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic.mkv'
            path.write_bytes(data)
            provider_job = ProviderJob(self.evidence.snapshot, transfer.provider_job)
            staged = StagedFile(provider_job, str(path), sha256(data).hexdigest(), len(data), True, False)
            acquired_at = timezone.now() - timedelta(minutes=1)
            metadata = {'source': 'Trusted synthetic fixture', 'artist': 'Fiction Artist & Friends',
                        'acquired_at_basis': 'sab_history_completed_at',
                        'artist_id': 'fixture-artist-1', 'track_title': 'Fiction Track', 'track_id': 'fixture-track-1'}
            completion = acquire.LocalCompletion(Completion(provider_job, 'completed', True, True, 0, staged),
                transfer.binding['exact']['digest'], transfer.nzb_sha256, str(path),
                acquired_at=acquired_at, playback_verified=True, metadata=metadata)
            acquire.handoff(actor=self.owner, profile_id=1, job_id=self.job.pk,
                            context=self.context, evidence=completion)
            with patch.object(acquire, 'verify_staging', side_effect=AssertionError('GET must not read files')):
                response = self.client.get(self.status_url + 'local-handoff/')
            exported = response.json()
            self.assertEqual(exported['path'], str(path))
            self.assertEqual(exported['sha256'], staged.sha256)
            self.assertEqual(exported['size'], len(data))
            self.assertEqual(exported['media_identity'], 'sha256:' + staged.sha256)
            self.assertEqual(exported['catalogue_identity'], 'tmdb:movie:11:film')
            self.assertFalse(exported['kodi_music_catalog_eligible'])
            self.assertTrue(exported['test_only'])
            self.assertEqual(exported['media_kind'], 'film')
            self.assertEqual(exported['acquired_at'], acquired_at.isoformat())
            self.assertEqual(exported['acquired_at_basis'], 'sab_history_completed_at')
            self.assertTrue(exported['verified_at'])
            self.assertEqual(exported['metadata'], metadata)
            self.assertEqual(exported['playback_verification'], 'verified_playable')
            self.assertFalse(exported['indexed'])
            self.assertFalse(exported['playback_started'])
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotIn('emby', response.content.decode().lower())
        self.assertContains(self.client.get(self.status_url), 'Export verified local handoff for Kodi')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
