"""Offline Hydra DTO fixtures; no private store, transport, URL or NZB retrieval."""
from copy import deepcopy
from datetime import timedelta
import importlib
import json
import uuid
from unittest.mock import Mock, patch

from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from shelf import obtain_parser as parser, obtain_preview as preview, saved
from shelf.acquisition_backend import PrivateSearch
from shelf.acquisition_contract import ContractError
from shelf.models import AcquisitionJob, FeedbackEvent, FeedbackProjection, Grant, ObtainPreview, ObtainResult, ObtainSelection
from shelf.tests.test_saved import SavedFixture


def item(title='Fiction Film 2024 1080p', guid='-9223372036854775808', category='2000', **changes):
    return {'title': title, 'guid': guid, 'id': guid,
        'link': 'http://private.invalid/getnzb?apikey=SYNTHETIC_SECRET',
        'description': '<b>SYNTHETIC_SECRET</b>',
        'enclosure': {'@attributes': {'length': '12345', 'type': 'application/x-nzb',
            'url': 'http://private.invalid/getnzb?apikey=SYNTHETIC_SECRET'}},
        'attr': [{'@attributes': {'name': 'category', 'value': category}},
                 {'@attributes': {'name': 'hydraIndexerName', 'value': 'Fiction Index'}},
                 {'@attributes': {'name': 'hydraIndexerHost', 'value': 'http://private.invalid'}}], **changes}


def payload(*rows):
    return json.dumps({'channel': {'item': list(rows)}}).encode()


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class PreviewTests(SavedFixture, TestCase):
    def fake(self, *rows, action=None):
        self.private = PrivateSearch(payload(*rows))
        backend = Mock()
        def respond(*args, **kwargs):
            if action:
                action()
            return self.private
        backend.search.side_effect = respond
        self.backend = backend
        return lambda **kw: backend

    def searched(self, *rows):
        self.saved = self.item()
        return preview.search(actor=self.owner, profile_id=1, item_id=self.saved.pk,
                              factory=self.fake(*(rows or [item()])))

    def data(self, receipt, result):
        return {'review_token': preview.review_token(receipt, result), 'scope': receipt.scope,
                'edition': result.metadata['title'], 'authorized_content': 'on',
                'storage': 'local_library', 'storage_check': 'on'}

    def choose(self, receipt, result, data=None):
        return preview.confirm(actor=self.owner, profile_id=1, preview_id=receipt.pk,
            result_id=result.pk, data=data or self.data(receipt, result))

    def test_persistence_exact_repeat_restart_no_secrets_no_taste_or_enqueue(self):
        receipt=self.searched(); result=receipt.results.get(); data=self.data(receipt,result)
        job, created=self.choose(receipt,result,data)
        self.assertTrue(created)
        self.assertEqual(ObtainSelection.objects.get(job=job).result_id,result.pk)
        self.assertEqual(job.status,'blocked_configuration')
        self.assertIn('Submission disabled',job.reason)
        importlib.reload(preview)
        again, created=self.choose(ObtainPreview.objects.get(pk=receipt.pk),
                                   ObtainResult.objects.get(pk=result.pk),data)
        self.assertFalse(created); self.assertEqual(again.pk,job.pk)
        self.backend.enqueue.assert_not_called()
        self.assertFalse(any(self.private._payload))
        dump=json.dumps(preview.context(receipt))
        for value in ('SYNTHETIC_SECRET','http','getnzb','apikey','description'):
            self.assertNotIn(value,dump)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertEqual(FeedbackProjection.objects.count(),0)

    def test_wrong_result_edition_scope_tokens_and_digest_conflict(self):
        receipt=self.searched(item(guid='1'),item(guid='2')); a,b=list(receipt.results.all())
        for changes in ({'edition':'Different edition'}, {'scope':'audiobook'},
                        {'review_token':'stale'}, {'review_token':self.data(receipt,b)['review_token']}):
            with self.subTest(changes=changes), self.assertRaises(saved.SavedError) as e:
                self.choose(receipt,a,self.data(receipt,a)|changes)
            self.assertEqual(e.exception.status,409)
        a.metadata['title']='Changed'; a.save()
        with self.assertRaises(saved.SavedError): self.choose(receipt,a)
        self.assertEqual(AcquisitionJob.objects.count(),0)

    def test_duplicate_choice_different_result_and_legacy_identity_conflict(self):
        receipt=self.searched(item(guid='1'),item(guid='2')); a,b=list(receipt.results.all())
        self.choose(receipt,a)
        with self.assertRaises(saved.SavedError) as e: self.choose(receipt,b)
        self.assertEqual(e.exception.status,409)
        second=preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk,factory=self.fake(item()))
        with self.assertRaises(saved.SavedError): self.choose(second,second.results.get())
        self.assertEqual(AcquisitionJob.objects.count(),1)
        self.backend.enqueue.assert_not_called()

    def test_expiry_nonce_version_and_cross_profile_actor(self):
        receipt=self.searched(); result=receipt.results.get(); data=self.data(receipt,result)
        for field,value in (('nonce',uuid.uuid4()),('version',2),('expires_at',timezone.now()-timedelta(seconds=1))):
            old=getattr(receipt,field); setattr(receipt,field,value); receipt.save()
            with self.assertRaises(saved.SavedError): self.choose(receipt,result,data)
            setattr(receipt,field,old); receipt.save()
        for actor,profile_id in ((self.other,1),(self.owner,2)):
            with self.assertRaises(Http404):
                preview.owned_preview(actor,profile_id,receipt.pk)

    def test_removal_revocation_and_cancellation_keep_history(self):
        receipt=self.searched(); result=receipt.results.get()
        job,_=self.choose(receipt,result)
        saved.remove(actor=self.owner,profile_id=1,item_id=self.saved.pk)
        job.refresh_from_db(); self.assertIsNone(job.saved_item_id)
        self.assertEqual(ObtainSelection.objects.get(job=job).result_id,result.pk)
        with self.assertRaises(Http404): self.choose(receipt,result)
        saved.cancel(actor=self.owner,profile_id=1,job_id=job.pk)
        job.refresh_from_db(); self.assertEqual(job.status,'cancelled')
        self.assertEqual(ObtainResult.objects.count(),1)
        self.backend.enqueue.assert_not_called()

    def test_revocation_or_removal_during_provider_work_stores_no_results(self):
        self.saved=self.item()
        for action in (lambda: Grant.objects.filter(pk=self.grant.pk).update(can_select=False),
                       lambda: self.saved.delete()):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
            with self.assertRaises(Http404):
                preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk,
                    factory=self.fake(item(),action=action))
            self.assertEqual(ObtainResult.objects.count(),0)
            self.assertFalse(any(self.private._payload))

    def test_failure_empty_expired_and_unconfigured_are_distinct(self):
        self.saved=self.item()
        for state in ('expired','unconfigured'):
            with patch.object(preview,'search_backend',side_effect=preview.SearchUnavailable(state)):
                r=preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk)
            self.assertEqual(r.state,state)
        r=preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk,factory=self.fake())
        self.assertEqual(r.state,'empty')
        factory=self.fake(); self.private._payload[:]=b'<error/>'
        r=preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk,factory=factory)
        self.assertEqual(r.state,'failed')

    def test_omitted_indexer_never_persists_and_partial_search_rechecks_authority(self):
        unsafe = item(guid='2')
        unsafe['attr'][1]['@attributes']['value'] = '<UNSAFE_DISPLAY_SECRET>'
        receipt = self.searched(item(), unsafe)
        self.assertEqual(receipt.state, 'partial')
        self.assertEqual(receipt.results.count(), 1)
        self.assertFalse(any(self.private._payload))
        self.assertNotIn('UNSAFE_DISPLAY_SECRET', json.dumps(preview.context(receipt)))
        result = receipt.results.get()
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Http404):
            self.choose(receipt, result)
        self.assertFalse(AcquisitionJob.objects.exists())
        self.backend.enqueue.assert_not_called()

    def test_album_caller_keeps_exact_authorized_selection_with_partial_results(self):
        from shelf.album_acquisition import prepare_album
        from shelf.acquisition_contract import Blocked
        from shelf.obtain_acquisition import exact_result
        identity = {'namespace': 'licensed_catalogue', 'artist_id': 'synthetic-artist',
                    'release_id': 'synthetic-release', 'artist': 'Fiction Artist',
                    'album': 'Fiction Album', 'track_count': 2, 'source': 'Synthetic catalogue'}
        authorization = {'rights_basis': 'Synthetic fixture only', 'one_album': True,
                         'storage_checked': True, 'duplicates_checked': True}
        title = 'Fiction Artist - Fiction Album FLAC'
        safe = item(title=title, category='3040', guid='1')
        unsafe = item(title=title + 'x' * 201, category='3040', guid='2')
        factory = self.fake(safe, unsafe)
        with self.assertRaises(Blocked):
            prepare_album(actor=self.owner, profile_id=1, identity=identity, authorization=authorization,
                          selected_guid='2', selected_title=title, backend=factory())
        self.assertFalse(AcquisitionJob.objects.exists())
        factory = self.fake(safe, unsafe)
        job = prepare_album(actor=self.owner, profile_id=1, identity=identity, authorization=authorization,
                            selected_guid='1', selected_title=title, backend=factory())
        exact, receipt = exact_result(job, fresh=True)
        self.assertEqual(receipt.state, 'partial')
        self.assertEqual(exact.guid, '1')
        self.assertEqual(job.edition, title)
        self.assertEqual(receipt.results.count(), 1)
        self.assertFalse(any(self.private._payload))
        self.backend.enqueue.assert_not_called()

    def test_album_missing_categories_cannot_be_selected_with_omitted_neighbors(self):
        from shelf.album_acquisition import prepare_album
        from shelf.acquisition_contract import Blocked
        from shelf.models import AlbumRequest
        identity = {'namespace': 'licensed_catalogue', 'artist_id': 'synthetic-artist',
                    'release_id': 'synthetic-release', 'artist': 'Fiction Artist',
                    'album': 'Fiction Album', 'track_count': 2, 'source': 'Synthetic catalogue'}
        authorization = {'rights_basis': 'Synthetic fixture only', 'one_album': True,
                         'storage_checked': True, 'duplicates_checked': True}
        title = 'Fiction Artist - Fiction Album FLAC'
        factory = self.fake(item(title=title, guid='1', attr=[]), item(title='<unsafe>', guid='2'))
        with self.assertRaises(Blocked):
            prepare_album(actor=self.owner, profile_id=1, identity=identity, authorization=authorization,
                          selected_guid='1', selected_title=title, backend=factory())
        self.assertFalse(AlbumRequest.objects.exists())
        self.assertFalse(AcquisitionJob.objects.exists())
        self.assertFalse(any(self.private._payload))
        self.backend.enqueue.assert_not_called()

    def test_methods_csrf_get_save_confirm_inert_and_http_boundary(self):
        receipt=self.searched(); result=receipt.results.get()
        base=f'/profiles/1/saved/previews/{receipt.pk}/'
        review=base+f'results/{result.pk}/'; search=f'/profiles/1/saved/{self.saved.pk}/preview/search/'
        with patch.object(preview,'search_backend',side_effect=AssertionError('external work')):
            self.assertEqual(self.client.get(search).status_code,405)
            self.assertEqual(self.client.get(review+'confirm/').status_code,405)
            self.assertEqual(self.client.get(review).status_code,200)
            self.assertEqual(self.client.get(base).status_code,200)
            self.assertEqual(self.heart().status_code,302)
            self.assertEqual(self.client.post(review+'confirm/',self.data(receipt,result)).status_code,302)
            self.assertEqual(self.client.post(search,{'url':'http://secret','apikey':'secret'}).status_code,400)
        csrf=Client(enforce_csrf_checks=True); csrf.force_login(self.owner)
        session=csrf.session; from shelf.authority import SELECTION_KEY
        session[SELECTION_KEY]=1; session.save()
        self.assertEqual(csrf.post(search).status_code,403)
        self.assertEqual(csrf.post(review+'confirm/',self.data(receipt,result)).status_code,403)
        self.client.logout(); self.assertEqual(self.client.get(base).status_code,302)

    def test_authorized_search_post_uses_server_identity_and_redirects(self):
        self.saved=self.item()
        factory=self.fake(item())
        with patch.object(preview,'search_backend',side_effect=factory):
            response=self.client.post(f'/profiles/1/saved/{self.saved.pk}/preview/search/')
        self.assertEqual(response.status_code,302)
        self.backend.search.assert_called_once_with(self.backend.caps.return_value,
            self.saved.title,'film',actor_id=1,profile_id=1)
        self.backend.enqueue.assert_not_called()

    def test_multiple_episode_results_have_no_review_or_confirmation_path(self):
        self.saved = self.item('tv')
        for suffix in ('S01E01+E02', 'S01E01 + E02', 'S01E01 and E02', 'S01E01/02',
                       'S01E01, E02', 'S01E01, 02', 'S01E01 and 02', 'S01E01 + 02'):
            with self.subTest(suffix=suffix):
                factory = self.fake(item(title='Fiction Series '+suffix, category='5000'))
                with patch.object(preview, 'search_backend', side_effect=factory):
                    response = self.client.post(f'/profiles/1/saved/{self.saved.pk}/preview/search/')
                self.assertEqual(response.status_code, 302)
                receipt = ObtainPreview.objects.get(pk=response.url.rstrip('/').split('/')[-1])
                result = receipt.results.get()
                self.assertFalse(result.metadata['selectable'])
                self.assertEqual(result.metadata['reason'], 'ambiguous_episode')
                base = f'/profiles/1/saved/previews/{receipt.pk}/'
                review = base+f'results/{result.pk}/'
                with patch.object(preview, 'search_backend', side_effect=AssertionError('external work')):
                    response = self.client.get(base)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, f'href="{review}"')
                    self.assertFalse(response.context['obtain_submission_enabled'])
                    self.assertEqual(self.client.get(review).status_code, 409)
                    # Even a server-signed token and complete acknowledgements cannot select it.
                    self.assertEqual(self.client.post(review+'confirm/', self.data(receipt, result)).status_code, 409)
                self.assertFalse(any(self.private._payload))
                self.backend.enqueue.assert_not_called()
                self.assertEqual(AcquisitionJob.objects.count(), 0)
                self.assertEqual(ObtainSelection.objects.count(), 0)

    def explicit_episode(self, season=2, episode=2, *rows):
        self.saved = getattr(self, 'saved', None) or self.item('tv')
        return preview.search(actor=self.owner, profile_id=1, item_id=self.saved.pk,
            season=season, episode=episode,
            factory=self.fake(*(rows or [item(title=f'Fiction Series S{season:02d}E{episode:02d}', category='5000')])))

    def test_explicit_episode_search_persists_scope_before_provider_and_confirms_exact_job(self):
        self.saved = self.item('tv')
        def check_persisted_scope():
            self.assertEqual(ObtainPreview.objects.get().scope, 'episode_s02e02')
        factory = self.fake(item(title='Fiction Series S02E02', category='5000'),
                            action=check_persisted_scope)
        receipt = preview.search(actor=self.owner, profile_id=1, item_id=self.saved.pk,
                                 season=2, episode=2, factory=factory)
        self.backend.search.assert_called_once_with(self.backend.caps.return_value,
            'Fiction Series S02E02', 'episode', actor_id=1, profile_id=1)
        result = receipt.results.get()
        job, created = self.choose(receipt, result)
        self.assertTrue(created)
        self.assertEqual(job.scope, 'episode_s02e02')
        self.assertEqual(saved.job_context(job)['episode_code'], 'S02E02')
        for url in (f'/profiles/1/saved/previews/{receipt.pk}/',
                    f'/profiles/1/saved/previews/{receipt.pk}/results/{result.pk}/',
                    f'/profiles/1/saved/jobs/{job.pk}/'):
            self.assertContains(self.client.get(url), 'Series — S02E02 only')
        self.backend.enqueue.assert_not_called()

    def test_episode_search_rejects_missing_invalid_and_non_tv_before_provider(self):
        self.saved = self.item('tv')
        url = f'/profiles/1/saved/{self.saved.pk}/preview/search/'
        with patch.object(preview, 'search_backend') as factory:
            for data in ({'season': '2'}, {'episode': '2'}, {'season': '-1', 'episode': '2'},
                         {'season': '2', 'episode': '1000'}, {'season': '2.5', 'episode': '2'},
                         {'season': '1000', 'episode': '2'}, {'season': '', 'episode': '2'}):
                with self.subTest(data=data):
                    self.assertEqual(self.client.post(url, data).status_code, 400)
            film = self.item()
            self.assertEqual(self.client.post(f'/profiles/1/saved/{film.pk}/preview/search/',
                                             {'season': '2', 'episode': '2'}).status_code, 400)
        factory.assert_not_called()
        self.assertFalse(ObtainPreview.objects.exists())

    def test_tv_search_fields_render_and_post_uses_selected_or_default_scope(self):
        self.saved = self.item('tv')
        start = self.client.get(f'/profiles/1/saved/{self.saved.pk}/preview/')
        self.assertContains(start, 'name="season"')
        self.assertContains(start, 'name="episode"')
        for data, code, scope in (({'season': '2', 'episode': '2'}, 'S02E02', 'episode_s02e02'),
                                  ({'season': '', 'episode': ''}, 'S01E01', 'series_s01e01')):
            with self.subTest(data=data):
                factory = self.fake(item(title='Fiction Series ' + code, category='5000'))
                with patch.object(preview, 'search_backend', side_effect=factory):
                    response = self.client.post(f'/profiles/1/saved/{self.saved.pk}/preview/search/', data)
                self.assertEqual(response.status_code, 302)
                receipt = ObtainPreview.objects.get(pk=response.url.rstrip('/').split('/')[-1])
                self.assertEqual(receipt.scope, scope)
                self.assertTrue(receipt.results.get().metadata['selectable'])
                self.backend.enqueue.assert_not_called()

    def test_explicit_special_and_three_digit_episode_boundaries(self):
        for season, episode, scope in ((0, 2, 'episode_s00e02'),
                                        (999, 999, 'episode_s999e999')):
            with self.subTest(season=season, episode=episode):
                receipt = self.explicit_episode(season, episode)
                self.assertEqual(receipt.scope, scope)
                job, _ = self.choose(receipt, receipt.results.get())
                self.assertEqual(job.scope, scope)

    def test_explicit_episode_wrong_listing_scope_edit_and_stale_token_cannot_confirm(self):
        receipt = self.explicit_episode(2, 2,
            item(title='Fiction Series S02E02', category='5000', guid='1'),
            item(title='Fiction Series S02E03', category='5000', guid='2'),
            item(title='Another Series S02E02', category='5000', guid='3'))
        good, wrong_episode, wrong_series = [receipt.results.get(guid=str(i)) for i in (1, 2, 3)]
        self.assertEqual(wrong_episode.metadata['reason'], 'ambiguous_episode')
        self.assertEqual(wrong_series.metadata['reason'], 'title_mismatch')
        for result in (wrong_episode, wrong_series):
            with self.assertRaises(saved.SavedError):
                self.choose(receipt, result)
        data = self.data(receipt, good)
        with self.assertRaises(saved.SavedError):
            self.choose(receipt, good, data | {'scope': 'episode_s02e03'})
        receipt.scope = 'episode_s02e03'
        receipt.save()
        with self.assertRaises(saved.SavedError):
            self.choose(receipt, good, data)
        with self.assertRaises(saved.SavedError):
            self.choose(receipt, good)  # Even a fresh signature cannot bless stale result metadata.
        receipt.scope = 'episode_s02e02'
        receipt.save()
        self.saved.source_id += 1
        self.saved.save()
        with self.assertRaises(saved.SavedError):
            self.choose(receipt, good, data)
        self.assertFalse(AcquisitionJob.objects.exists())

    def test_different_episodes_coexist_same_episode_deduplicates_and_cancelled_review_stays_stale(self):
        first = self.explicit_episode()
        job, _ = self.choose(first, first.results.get())
        other = self.explicit_episode(2, 3)
        other_job, _ = self.choose(other, other.results.get())
        self.assertNotEqual(job.active_key, other_job.active_key)
        duplicate = self.explicit_episode()
        with self.assertRaises(saved.SavedError):
            self.choose(duplicate, duplicate.results.get())
        data = self.data(first, first.results.get())
        saved.cancel(actor=self.owner, profile_id=1, job_id=job.pk)
        with self.assertRaises(saved.SavedError):
            self.choose(first, first.results.get(), data)
        replacement = self.explicit_episode()
        new_job, created = self.choose(replacement, replacement.results.get())
        self.assertTrue(created)
        self.assertNotEqual(new_job.pk, job.pk)

    def test_unqualified_pilot_and_explicit_pilot_share_duplicate_guard(self):
        self.saved = self.item('tv')
        legacy = preview.search(actor=self.owner, profile_id=1, item_id=self.saved.pk,
            factory=self.fake(item(title='Fiction Series S01E01', category='5000')))
        self.assertEqual(legacy.scope, 'series_s01e01')
        legacy_job, _ = self.choose(legacy, legacy.results.get())
        explicit = self.explicit_episode(1, 1)
        with self.assertRaises(saved.SavedError):
            self.choose(explicit, explicit.results.get())
        self.assertEqual(AcquisitionJob.objects.count(), 1)
        self.assertEqual(legacy_job.scope, 'series_s01e01')

    def test_database_unique_guid_and_active_identity_are_final_arbiter(self):
        receipt=self.searched(); result=receipt.results.get()
        with self.assertRaises(IntegrityError),transaction.atomic():
            ObtainResult.objects.create(preview=receipt,guid=result.guid,metadata=result.metadata,digest=result.digest)
        job,_=self.choose(receipt,result)
        with self.assertRaises(IntegrityError),transaction.atomic():
            job.pk=None; job.request_id=uuid.uuid4(); job.save(force_insert=True)

    def test_selection_binding_cannot_be_duplicated_or_rebound_by_replay(self):
        receipt=self.searched(item(guid='1'),item(guid='2'))
        a,b=list(receipt.results.all())
        job,_=self.choose(receipt,a)
        with self.assertRaises(IntegrityError),transaction.atomic():
            ObtainSelection.objects.create(job=job,result=b)
        with self.assertRaises(saved.SavedError):
            self.choose(receipt,b)
        self.assertEqual(ObtainSelection.objects.get(job=job).result_id,a.pk)
        self.assertEqual(AcquisitionJob.objects.count(),1)

    def test_invalid_backend_return_is_failed_without_closing_arbitrary_values(self):
        self.saved=self.item()
        factory=self.fake()
        self.backend.search.side_effect=None
        self.backend.search.return_value=1
        receipt=preview.search(actor=self.owner,profile_id=1,item_id=self.saved.pk,factory=factory)
        self.assertEqual(receipt.state,'failed')
        self.assertEqual(ObtainResult.objects.count(),0)

    def test_factory_reuses_exact_store_grant_closes_private_record_and_is_inert(self):
        from dataclasses import replace
        from shelf import credential_store as store
        from shelf.tests.test_acquisition_backend import grant
        metadata=grant('hydra')
        metadata=replace(metadata,binding=replace(metadata.binding,actor_id='1',profile_id='1'))
        handle=Mock(context=store._provider_context('hydra'))
        record=Mock(metadata=metadata)
        opener=Mock(return_value=handle)
        with patch.object(store,'lock_store'),patch.object(store,'read_record',return_value=record),\
             patch.object(preview,'Backend') as constructor:
            backend=preview.search_backend(actor=self.owner,profile_id=1,_open_store=opener)
            constructor.assert_called_once_with(hydra_grant=metadata,_open_store=opener)
            backend.caps.assert_not_called(); backend.search.assert_not_called()
        record.close.assert_called_once(); handle.close.assert_called_once()
        for changes,state in (({'scopes':store.SAB_SCOPES},'unconfigured'),
                              ({'expires_at':store.utc_now()-timedelta(seconds=1)},'unconfigured')):
            # Invalid metadata never constructs a backend. Expiry from the reviewed
            # reader has its own fixed expired diagnostic below.
            record.metadata=replace(metadata,**changes)
            with patch.object(store,'lock_store'),patch.object(store,'read_record',return_value=record),\
                 patch.object(preview,'Backend') as constructor:
                with self.assertRaises(preview.SearchUnavailable):
                    preview.search_backend(actor=self.owner,profile_id=1,_open_store=opener)
                constructor.assert_not_called()
        with patch.object(store,'lock_store'),patch.object(store,'read_record',side_effect=store.Refused('expired')):
            with self.assertRaises(preview.SearchUnavailable) as error:
                preview.search_backend(actor=self.owner,profile_id=1,_open_store=opener)
            self.assertEqual(error.exception.state,'expired')
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        opener.reset_mock()
        with self.assertRaises(Http404):
            preview.search_backend(actor=self.owner,profile_id=1,_open_store=opener)
        opener.assert_not_called()

    def test_missing_preview_tables_fail_honestly_before_factory(self):
        self.saved=self.item()
        with patch.object(preview,'available',return_value=False),patch.object(preview,'search_backend') as factory:
            response=self.client.post(f'/profiles/1/saved/{self.saved.pk}/preview/search/')
        self.assertEqual(response.status_code,503)
        factory.assert_not_called()
