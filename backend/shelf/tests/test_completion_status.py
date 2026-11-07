"""Production bridge against real isolated submission/hook contracts; no network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
import tempfile
from unittest import TestCase as PlainTestCase
from unittest.mock import patch

from django.test import Client, TransactionTestCase, override_settings
from django.http import Http404
from shelf import acquisition_backend, credential_store as store
from shelf import obtain_acquisition as acquire, obtain_runtime as runtime, completion_status as bridge
from shelf.acquisition_contract import Blocked, COMPLETION_SCRIPT
from shelf.acquisition_transport import LoopbackTransport
from shelf.models import ObtainCompletionStatus, ObtainTransfer, Grant, AlbumRequest
from shelf.tests.fixtures_credentials import FakeTree
from shelf.tests.test_acquisition_backend import grant
from shelf.tests import test_obtain_runtime as runtime_tests
from shelf.tests.test_saved import SavedFixture
from shelf.tests.test_obtain_ui import listing

ProviderHTTP = runtime_tests.ProviderHTTP

@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class CompletionStatusTests(SavedFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.series = self.item('tv')
        self.folder = tempfile.TemporaryDirectory(prefix='runtime-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.complete = Path(self.folder.name) / 'Complete'
        self.complete.mkdir()
        self.control = Path(self.folder.name) / 'control'
        self.control.mkdir(mode=0o700)
        self.registry = self.control / 'registry.json'
        self.receipts = Path(self.folder.name) / 'receipts'
        self.receipts.mkdir(mode=0o700)
        self.policy = runtime.SubmissionPolicy(True, str(self.complete), 20000, 4096, True,
            str(self.registry), True, COMPLETION_SCRIPT)
        self.paths, self.grants, self.reads = {}, {}, []
        for provider in ('hydra', 'sab'):
            tree = FakeTree(store, provider=provider)
            self.addCleanup(tree.temporary.cleanup)
            metadata = grant(provider)
            metadata = replace(metadata, binding=replace(metadata.binding, actor_id='1', profile_id='1'))
            self.grants[provider] = metadata
            result = store.commit_entry(tree.store, metadata, bytearray(b'FICTION-ONLY-' + provider.encode()))
            self.assertEqual(result.outcome, 'stored')
            self.paths[provider] = tree.path
            tree.store.close()
        def opener(*, provider):
            self.reads.append(provider)
            return store._fixture_store(self.paths[provider], provider=provider)
        self.http = ProviderHTTP()
        self.enterContext(patch.object(store, 'open_store', side_effect=opener))
        self.enterContext(patch.object(acquisition_backend, 'LoopbackTransport',
            return_value=LoopbackTransport(_connection_factory=self.http.factory)))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.enterContext(override_settings(OBTAIN_SUBMISSION_POLICY=vars(self.policy)))
        self.client = Client(enforce_csrf_checks=True)
        self.login()

    login = runtime_tests.RuntimeTests.login
    hidden = runtime_tests.RuntimeTests.hidden
    select = runtime_tests.RuntimeTests.select
    review = runtime_tests.RuntimeTests.review
    submit = runtime_tests.RuntimeTests.submit
    addfile_calls = runtime_tests.RuntimeTests.addfile_calls
    observe_history = runtime_tests.RuntimeTests.observe_history
    registered = runtime_tests.RuntimeTests.registered
    load_hook = runtime_tests.RuntimeTests.load_hook

    def completed(self):
        self.review(); self.submit()
        self.context = acquire.OperatorContext(1, 1, 'offline_test', True)
        entry = self.registered()
        root = Path(entry['root']); root.mkdir()
        (root / 'observed.mkv').write_bytes(b'Synthetic bytes, not playable video.')
        env = {'SAB_STATUS': 'Running', 'SAB_PP_STATUS': '0', 'SAB_NZO_ID': entry['nzo_id'],
               'SAB_COMPLETE_DIR': str(root), 'SAB_FILES': '["observed.mkv"]'}
        self.assertEqual(self.load_hook().run(str(self.registry), str(self.receipts), env), 0)
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        self.slot = {'nzo_id': entry['nzo_id'], 'name': root.name, 'status': 'Completed',
            'storage': str(root), 'script': COMPLETION_SCRIPT, 'fail_message': '',
            'completed': int(datetime.now(timezone.utc).timestamp())}
        result = self.observe_history(lambda nzo: self.slot)
        self.assertEqual(result['state'], 'completed')
        self.handoff = result['handoff']
        return ObtainCompletionStatus.objects.get(transfer__job=self.job)

    def receipt(self, state='imported'):
        h = self.handoff; f = h['files'][0]
        selection = {'source_identity': h['source_identity'], 'provenance': f['provenance'],
            'nzo_id': f['nzo_id'], 'completion_evidence_sha256': h['completion_evidence_sha256']}
        local = '/fixture/Cabinet Movies/' + f['relative_path']
        stamp = datetime.now(timezone.utc).isoformat()
        return {'version': 1, 'request_id': str(self.job.request_id), 'selection': selection,
            'file': deepcopy(f), 'completion_evidence_sha256': h['completion_evidence_sha256'],
            'hook_sha256': bridge.digest({'authorization': h['authorization'],
                'files': [dict(f, acquired_at=None, acquired_at_basis='unavailable')]}),
            'status': state, 'scan_key': 'video-' + str(self.job.request_id), 'scan_attempts': 1,
            'playback_started': False, 'created_at': stamp, 'checked_at': stamp,
            'local_path': local, 'library': {'movieid': 12, 'file': local,
                'uniqueid': {'tmdb': str(self.job.source_id)}, 'resume': {'position': 0}} if state == 'imported' else None}

    def record(self, receipt, **kw):
        return bridge.record_movie_import(**dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
            context=self.context, receipt=receipt, **kw))

    def test_completed_observer_persists_historical_status_and_reexport_is_noop(self):
        row = self.completed()
        original = (row.completion, row.observed_at, row.revision)
        again = self.observe_history(lambda nzo: self.slot)
        self.assertEqual(again['state'], 'completed')
        row.refresh_from_db()
        self.assertEqual((row.completion, row.observed_at, row.revision), original)
        page = self.client.get(self.status)
        self.assertContains(page, 'Download completed according to SAB history')
        self.assertContains(page, 'Kodi import has not been reported')
        self.assertNotContains(page, 'Final completion remains unknown')
        self.assertContains(page, 'Current file availability, playback and complete-work identity remain unverified')
        self.assertEqual(ObtainTransfer.objects.get(job=self.job).state, 'queued')

    def test_import_receipt_replay_is_noop_and_display_does_not_claim_playability(self):
        self.completed(); receipt = self.receipt()
        calls = len(self.http.calls)
        row = self.record(receipt)
        self.assertEqual(row.delivery_state, 'imported')
        self.assertEqual(row.revision, 1)
        self.assertNotIn('resume', str(row.delivery_receipt))
        self.assertEqual(self.record(receipt).revision, 1)
        self.assertEqual(len(self.http.calls), calls)
        self.assertContains(self.client.get(self.status), 'Kodi import was confirmed')
        self.assertEqual(ObtainTransfer.objects.get(job=self.job).state, 'queued')

    def readiness_receipt(self):
        receipt = self.receipt()
        stamp = datetime.now(timezone.utc).isoformat()
        receipt['readiness'] = {'file_verified_at': stamp, 'library_observed_at': stamp,
            'validated_sha256': receipt['file']['sha256'], 'video_codec': 'h264', 'duration': 60.5}
        return receipt

    def test_readiness_distinguishes_file_stream_library_and_unplayed_state(self):
        self.completed(); receipt = self.readiness_receipt()
        self.record(receipt)
        page = self.client.get(self.status)
        for phrase in ('Downloaded file bytes verified at', 'Mac file bytes verified at',
                       'matching Kodi library entry', 'playback has not been tested',
                       'One Kodi scan was reserved'):
            self.assertContains(page, phrase)
        self.assertEqual(self.record(receipt).revision, 1)
        self.assertEqual(bridge.status_for(ObtainTransfer.objects.get(job=self.job))[
            'current_availability'], 'unknown')

    def test_readiness_cannot_promote_missing_or_mismatched_stream_evidence(self):
        self.completed()
        for key, value in [('validated_sha256', '0' * 64), ('duration', float('nan')),
                ('duration', -1), ('video_codec', '<script>'),
                ('file_verified_at', '2039-01-01T00:00:00+00:00'),
                ('library_observed_at', '2020-01-01T00:00:00+00:00')]:
            receipt = self.readiness_receipt(); receipt['readiness'][key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                self.record(receipt)
        receipt = self.readiness_receipt(); receipt['status'] = 'blocked'; receipt['library'] = None
        with self.assertRaises(ValueError): self.record(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_wrong_request_nzo_file_result_version_and_library_identity_refuse(self):
        self.completed()
        changes = [(['request_id'], 'foreign'), (['version'], 2),
            (['selection', 'nzo_id'], 'SABnzbd_nzo_foreign'),
            (['selection', 'provenance', 'result_digest'], '0'*64),
            (['selection', 'provenance', 'nzb_sha256'], '0'*64),
            (['file', 'sha256'], '0'*64), (['file', 'size'], 99),
            (['completion_evidence_sha256'], '0'*64),
            (['library', 'uniqueid', 'tmdb'], '999'),
            (['library', 'file'], '/fixture/other.mkv'), (['playback_started'], True)]
        for path, value in changes:
            receipt = deepcopy(self.receipt()); dest = receipt
            for key in path[:-1]: dest = dest[key]
            dest[path[-1]] = value
            with self.subTest(path=path), self.assertRaises((ValueError, KeyError, TypeError)):
                self.record(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_unknown_reconciles_forward_but_imported_and_conflicts_cannot_be_overwritten(self):
        self.completed(); unknown = self.receipt('unknown')
        row = self.record(unknown); self.assertEqual(row.delivery_state, 'unknown')
        self.assertEqual(self.record(unknown).revision, 1)
        altered = deepcopy(unknown); altered['checked_at'] = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(ValueError): self.record(altered)
        imported = self.receipt(); self.assertEqual(self.record(imported).revision, 2)
        with self.assertRaises(ValueError): self.record(unknown)
        altered = deepcopy(imported); altered['library']['movieid'] = 99
        with self.assertRaises(ValueError): self.record(altered)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'imported')

    def test_cancellation_and_revoked_authority_leave_completion_history_unchanged(self):
        self.completed(); receipt = self.receipt()
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        with self.assertRaises(Http404): self.record(receipt)
        Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
        acquire.cancel_local(self.job)
        with self.assertRaises((ValueError, Blocked)): self.record(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_synthetic_input_cannot_be_promoted_by_live_operator(self):
        self.completed()
        self.context = acquire.OperatorContext(1, 1, 'candidate_operator', True)
        with self.assertRaises(ValueError): self.record(self.receipt(), session_guard=lambda: None)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_foreign_operator_and_wrong_job_refuse(self):
        self.completed()
        with self.assertRaises(Blocked):
            bridge.record_movie_import(actor=self.other, profile_id=1, job_id=self.job.pk,
                context=self.context, receipt=self.receipt())
        with self.assertRaises(Http404):
            bridge.record_movie_import(actor=self.owner, profile_id=1, job_id=self.job.pk + 1,
                context=self.context, receipt=self.receipt())
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_blocked_report_can_reconcile_and_unicode_digest_matches_existing_producer(self):
        from shelf import completion_registry
        import hashlib
        self.assertEqual(bridge.digest({'title': 'Fiction — café'}, newline=True),
            hashlib.sha256(completion_registry.encode_json({'title': 'Fiction — café'})).hexdigest())
        self.completed(); blocked = self.receipt('blocked'); blocked['scan_attempts'] = 0
        self.assertEqual(self.record(blocked).delivery_state, 'blocked')
        self.assertEqual(self.record(self.receipt()).delivery_state, 'imported')

    def test_expiry_checked_again_under_lock_rolls_back_write(self):
        self.completed(); calls = []
        def expires():
            calls.append(1)
            if len(calls) == 2: raise ValueError('session expired')
        with self.assertRaises(ValueError): self.record(self.receipt(), session_guard=expires)
        self.assertEqual(len(calls), 2)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_cancellation_during_receipt_validation_rolls_back_delivery(self):
        self.completed(); receipt = self.receipt()
        original = bridge._movie_receipt
        def cancelling(*args):
            acquire.cancel_local(self.job)
            return original(*args)
        with patch.object(bridge, '_movie_receipt', side_effect=cancelling):
            with self.assertRaises((ValueError, Blocked)): self.record(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    album_selection = runtime_tests.RuntimeTests.album_selection
    submit_album = runtime_tests.RuntimeTests.submit_album

    def completed_episode(self, scope='series_s01e01'):
        self.saved = self.series
        original = self.http.request
        from shelf.episode_scope import episode_code, parse_scope
        code = episode_code(scope)
        original_post = self.client.post

        def post(path, data=None, **kwargs):
            if scope != 'series_s01e01' and path.endswith('/preview/search/') and data:
                season, number = parse_scope(scope)
                data = {**data, 'season': season, 'episode': number}
            return original_post(path, data, **kwargs)

        def episode(method, path, **kwargs):
            original(method, path, **kwargs)
            if parse_qs(urlsplit(path).query).get('t') == ['search']:
                self.http.payload = json.dumps({'channel': {'item': [listing(
                    title='Fiction Series ' + code + ' 1080p', category='5000')]}}).encode()

        with patch.object(self.http, 'request', side_effect=episode), patch.object(self.client, 'post', side_effect=post):
            return self.completed()

    def test_actual_s02e02_producer_kodi_receipt_and_backend_record(self):
        row = self.completed_episode('episode_s02e02')
        tools = str(Path(__file__).resolve().parents[3] / 'tools')
        self.enterContext(patch.object(sys, 'path', [tools, *sys.path]))
        import mac_episode_consumer as consumer
        import mac_kodi_consumer as files
        from test_mac_episode_consumer import FakeKodi, FakeTransfer
        from shelf import completion_media, completion_dispatch
        entry = self.registered()
        selection = completion_media.selection(self.handoff, entry)
        self.assertEqual(completion_dispatch.list_jobs(actor=self.owner, profile_id=1,
            context=self.context)['jobs'][0]['source_identity']['scope'], 'episode_s02e02')
        helper = SimpleNamespace(date=datetime.fromisoformat, unique_object=bridge.registry.unique_object)
        sync = SimpleNamespace(validate_relative=PurePosixPath)
        self.enterContext(patch.object(files, 'helpers', return_value=(helper, sync)))
        self.enterContext(patch.object(consumer, 'helpers', return_value=(helper, sync)))
        cache, state = self.control / 'TV Shows', self.control / 'consumer'
        target = cache / ('CC-' + entry['request_id']) / 'S02E02.mkv'
        kodi = FakeKodi(target, selection)
        payload = (Path(entry['root']) / 'observed.mkv').read_bytes()
        transfer = FakeTransfer(payload)
        options = dict(cache=cache, state_dir=state, execute=True, transfer=transfer, kodi=kodi, timeout=0)
        receipt = consumer.consume(self.handoff, selection, **options)
        self.assertEqual(receipt['status'], 'imported')
        self.assertEqual(receipt['library']['uniqueid']['cabinet'],
            'tmdb-show-' + str(self.job.source_id) + ':s02e02')
        recorded = self.record_media(receipt)
        self.assertEqual(recorded.delivery_state, 'imported')
        self.assertEqual(self.record_media(receipt).revision, recorded.revision)
        self.assertEqual(consumer.consume(self.handoff, selection, **options), receipt)
        self.assertEqual(len(kodi.scans), 1)
        for key, value in (('season', 1), ('episode', 1), ('show_uniqueid', {'tmdb': '999'})):
            bad = deepcopy(receipt)
            bad['library'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge._episode_receipt(bad, row, entry)

    def episode_receipt(self, state='imported'):
        receipt = self.receipt(state)
        local = '/fixture/Cabinet Episodes/CC-' + str(self.job.request_id) + '/S01E01.mkv'
        receipt['local_path'] = local
        receipt['scan_key'] = 'episode-' + str(self.job.request_id)
        receipt['library'] = None if state != 'imported' else {
            'episodeid': 12, 'tvshowid': 4, 'file': local, 'season': 1, 'episode': 1,
            'show_uniqueid': {'tmdb': str(self.job.source_id)},
            'uniqueid': {'cabinet': 'tmdb-show-' + str(self.job.source_id) + ':s01e01'},
        }
        return receipt

    def record_media(self, receipt, **kwargs):
        return bridge.record_import(actor=self.owner, profile_id=1, job_id=self.job.pk,
            context=self.context, receipt=receipt, **kwargs)

    def completed_album(self):
        self.album_selection()
        self.submit_album()
        self.context = acquire.OperatorContext(1, 1, 'offline_test', True)
        entry = self.registered()
        root = Path(entry['root'])
        root.mkdir()
        for name, payload in (
            ('01.flac', b'fLaCfirst synthetic track'),
            ('02.flac', b'fLaCsecond synthetic track'),
            ('cover.jpg', b'\xff\xd8\xffsynthetic image marker'),
            ('album.cue', b'opaque fixture sidecar'),
        ):
            (root / name).write_bytes(payload)
        env = {'SAB_STATUS': 'Running', 'SAB_PP_STATUS': '0', 'SAB_NZO_ID': entry['nzo_id'],
            'SAB_COMPLETE_DIR': str(root), 'SAB_FILES': '["01.flac","02.flac","cover.jpg","album.cue"]'}
        self.assertEqual(self.load_hook().run(str(self.registry), str(self.receipts), env), 0)
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        self.slot = {'nzo_id': entry['nzo_id'], 'name': root.name, 'status': 'Completed',
            'storage': str(root), 'script': COMPLETION_SCRIPT, 'fail_message': '',
            'completed': int(datetime.now(timezone.utc).timestamp())}
        result = self.observe_history(lambda nzo: self.slot)
        self.assertEqual(result['state'], 'completed')
        self.handoff = result['handoff']
        return ObtainCompletionStatus.objects.get(transfer__job=self.job)

    def album_receipt(self, state='imported'):
        h = self.handoff
        first = h['files'][0]
        selection = {'source_identity': h['source_identity'], 'album_identity': h['album_identity'],
            'provenance': first['provenance'], 'nzo_id': first['nzo_id'],
            'completion_evidence_sha256': h['completion_evidence_sha256']}
        hook = bridge.digest({'authorization': h['authorization'], 'files': [
            dict(item, acquired_at=None, acquired_at_basis='unavailable') for item in h['files']]})
        tracks = []
        for number, item in enumerate((item for item in h['files']
                                      if item['relative_path'].endswith('.flac')), 1):
            tracks.append({'relative_path': item['relative_path'],
                'local_path': '/fixture/Cabinet Albums/' + item['relative_path'],
                'sha256': item['sha256'], 'validated_sha256': item['sha256'], 'bytes': item['size'],
                'title': 'Fixture track ' + str(number), 'artist': self.album_identity['artist'],
                'album': self.album_identity['album'], 'disc': 1, 'track': number, 'duration': 123.5,
                'embedded_artwork': False})
        stamp = datetime.now(timezone.utc).isoformat()
        songs = [{'songid': number, 'albumid': 7, 'file': track['local_path'],
            'title': track['title'], 'artist': [track['artist']], 'album': track['album'],
            'track': track['track'], 'disc': track['disc'], 'duration': 123,
            'playcount': 0, 'lastplayed': '', 'thumbnail': '', 'art': {}}
            for number, track in enumerate(tracks, 1)]
        library = {'songs': songs, 'albums': [{'albumid': 7, 'title': self.album_identity['album'],
            'artist': [self.album_identity['artist']], 'art': {'thumb': '/fixture/Cabinet Albums/' +
                next(item['relative_path'] for item in h['files'] if item['relative_path'].endswith('cover.jpg'))}}],
            'all_tracks_indexed_once': True, 'artwork_present': True} if state == 'imported' else None
        result = {'version': 1, 'request_id': str(self.job.request_id), 'selection': selection,
            'signature': bridge.digest([bridge.digest([selection, hook]), '/fixture/Cabinet Albums']),
            'album_identity': h['album_identity'], 'files': deepcopy(h['files']), 'tracks': tracks,
            'hook_sha256': hook, 'completion_evidence_sha256': h['completion_evidence_sha256'],
            'status': state, 'scan_attempts': 1, 'scan_key': 'album-' + str(self.job.request_id),
            'readback_seen': state == 'imported', 'created_at': stamp, 'checked_at': stamp,
            'playback_started': False, 'library': library}
        if state == 'imported':
            result['readiness'] = {'file_verified_at': stamp, 'library_observed_at': stamp, 'track_count': 2}
        return result

    def test_episode_exact_canonical_path_readiness_and_compatibility_alias(self):
        self.completed_episode()
        receipt = self.episode_receipt()
        receipt['readiness'] = self.readiness_receipt()['readiness']
        with self.assertRaises(ValueError):
            self.record(receipt)
        row = self.record_media(receipt)
        self.assertEqual(row.delivery_state, 'imported')
        self.assertEqual(self.record_media(receipt).revision, 1)
        status = bridge.status_for(ObtainTransfer.objects.get(job=self.job))
        self.assertEqual(status['kind'], 'episode')
        self.assertTrue(status['supported_consumer'])
        self.assertEqual(status['current_availability'], 'unknown')
        self.assertEqual(status['library_metadata']['episodeid'], 12)

    def test_episode_wrong_show_episode_request_path_or_file_refuses(self):
        self.completed_episode()
        for path, value in (
            (['library', 'show_uniqueid', 'tmdb'], '999'),
            (['library', 'uniqueid', 'cabinet'], 'tmdb-show-999:s01e01'),
            (['library', 'episode'], 2), (['library', 'season'], True),
            (['library', 'episodeid'], True), (['library', 'tvshowid'], 0),
            (['scan_key'], 'video-' + str(self.job.request_id)),
            (['file', 'sha256'], '0' * 64),
        ):
            receipt = self.episode_receipt()
            target = receipt
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises((ValueError, KeyError)):
                self.record_media(receipt)
        for suffix in ('S01E02.mkv', 'S01E01.mp4', 'observed.mkv'):
            receipt = self.episode_receipt()
            receipt['local_path'] = receipt['library']['file'] = str(Path(receipt['local_path']).parent / suffix)
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.record_media(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_album_full_manifest_receipt_metadata_readiness_and_noop_replay(self):
        self.completed_album()
        receipt = self.album_receipt()
        row = self.record_media(receipt)
        self.assertEqual(row.delivery_state, 'imported')
        self.assertEqual(self.record_media(receipt).revision, 1)
        self.assertEqual(len(row.delivery_receipt['files']), 4)
        self.assertEqual(len(row.delivery_receipt['tracks']), 2)
        self.assertNotIn('lastplayed', str(row.delivery_receipt))
        self.assertNotIn('image://', str(row.delivery_receipt))
        status = bridge.status_for(ObtainTransfer.objects.get(job=self.job))
        self.assertEqual((status['kind'], status['artwork_present']), ('album', True))
        self.assertEqual(status['current_availability'], 'unknown')
        self.assertEqual(status['readiness']['track_count'], 2)
        page = self.client.get('/profiles/1/saved/jobs/' + str(self.job.pk) + '/')
        self.assertContains(page, 'FLAC tracks')
        self.assertContains(page, 'matching Kodi songs and album')
        self.assertNotContains(page, 'video stream')

    def test_album_wrong_release_missing_track_sidecar_and_duplicate_song_refuse(self):
        self.completed_album()
        mutations = [
            lambda r: r['album_identity'].update(release_id='foreign'),
            lambda r: r['selection']['album_identity'].update(track_count=1),
            lambda r: r['files'].pop(), lambda r: r['tracks'].pop(),
            lambda r: r['tracks'][1].update(track=1),
            lambda r: r['tracks'][0].update(validated_sha256='0' * 64),
            lambda r: r['tracks'][0].update(embedded_artwork=1),
            lambda r: r['tracks'][0].update(local_path='/fixture/foreign/01.flac'),
            lambda r: r['library']['songs'][1].update(songid=1),
            lambda r: r['library']['songs'][1].update(albumid=8),
            lambda r: r['library']['songs'][0].update(duration=999),
            lambda r: r['library']['songs'][0].update(track=99),
            lambda r: r['library']['albums'][0].update(artist=['Other artist']),
            lambda r: r['library']['songs'][0].update(albumartist=['Other artist']),
            lambda r: r['library']['albums'][0].update(art={'thumb': 'image://unrelated'}),
            lambda r: r['library'].update(artwork_present=False),
            lambda r: r['readiness'].update(track_count=3),
            lambda r: r.update(playback_started=True),
        ]
        for index, mutate in enumerate(mutations):
            receipt = deepcopy(self.album_receipt())
            mutate(receipt)
            with self.subTest(index=index), self.assertRaises((ValueError, KeyError)):
                self.record_media(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_album_receipt_monotonic_unknown_reconciliation_retains_readback(self):
        self.completed_album()
        blocked = self.album_receipt('blocked')
        blocked.update(scan_attempts=0, tracks=[], readback_seen=True)
        self.record_media(blocked)
        unknown = self.album_receipt('unknown')
        with self.assertRaises(ValueError):
            self.record_media(unknown)
        unknown['readback_seen'] = True
        self.record_media(unknown)
        self.assertEqual(self.record_media(unknown).revision, 2)
        imported = self.album_receipt()
        self.assertEqual(self.record_media(imported).revision, 3)
        with self.assertRaises(ValueError):
            self.record_media(self.album_receipt('unknown'))

    def test_album_revoked_authorization_or_cancelled_job_cannot_record(self):
        self.completed_album()
        receipt = self.album_receipt()
        album = AlbumRequest.objects.get()
        original = deepcopy(album.authorization)
        album.authorization['one_album'] = False
        album.save(update_fields=['authorization'])
        with self.assertRaises(ValueError):
            self.record_media(receipt)
        album.authorization = original
        album.save(update_fields=['authorization'])
        acquire.cancel_local(self.job)
        with self.assertRaises(ValueError):
            self.record_media(receipt)
        self.assertEqual(ObtainCompletionStatus.objects.get().delivery_state, 'not_reported')

    def test_album_current_identity_must_match_hash_bound_completion(self):
        self.completed_album()
        row = ObtainCompletionStatus.objects.get()
        bad = deepcopy(self.handoff)
        bad['album_identity']['release_id'] = 'foreign'
        with self.assertRaises(ValueError):
            bridge._completion(bad, self.registered(), self.job)
        self.assertEqual(row.delivery_state, 'not_reported')



class ConsumerReceiptBoundaryTests(PlainTestCase):
    def test_delayed_exports_produce_recordable_consumer_observation_times(self):
        """Real consumers emit durable receipts accepted by the backend contract.

        Transfers deliberately fail before publication; every adapter is injected
        and all consumer state is confined to a fresh temporary directory.
        """
        tools = str(Path(__file__).resolve().parents[3] / 'tools')
        with patch.object(sys, 'path', [tools, *sys.path]):
            fixtures = [importlib.import_module('test_mac_' + name + '_consumer')
                        for name in ('kodi', 'episode', 'album')]
        core = fixtures[0].video
        helper = SimpleNamespace(date=datetime.fromisoformat, unique_object=lambda pairs: dict(pairs))
        sync = SimpleNamespace(validate_relative=PurePosixPath)
        for fixture, validator in zip(fixtures, (bridge._movie_receipt, bridge._episode_receipt, bridge._album_receipt)):
            consumer = getattr(fixture, 'video', None) if fixture is fixtures[0] else getattr(
                fixture, 'episode' if fixture is fixtures[1] else 'album')
            with self.subTest(consumer=consumer.__name__), tempfile.TemporaryDirectory() as folder:
                handoff, selection = fixture.fixture()
                exported = datetime.now(timezone.utc) - timedelta(hours=1)
                acquired = (exported - timedelta(minutes=1)).replace(microsecond=0)
                handoff['checked_at'] = exported.isoformat()
                for item in handoff['files']:
                    item['acquired_at'] = acquired.isoformat()
                    item['verified_at'] = acquired.isoformat()
                handoff['completion_evidence']['acquired_at'] = acquired.isoformat()
                handoff['completion_evidence_sha256'] = core.digest(handoff['completion_evidence'], newline=True)
                selection['completion_evidence_sha256'] = handoff['completion_evidence_sha256']
                failing_transfer = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('synthetic transfer unavailable'))
                directory = Path(folder).resolve()
                options = dict(cache=directory / 'cache', state_dir=directory / 'state', execute=True,
                    transfer=failing_transfer, kodi=SimpleNamespace(call=lambda *args: self.fail('No Kodi calls')),
                    timeout=0)
                if consumer is fixtures[2].album:
                    options.update(art_transfer=failing_transfer, sidecar_verify=failing_transfer)
                with patch.object(core, 'helpers', return_value=(helper, sync)), patch.object(
                        fixtures[1].episode, 'helpers', return_value=(helper, sync)):
                    receipt = consumer.consume(handoff, selection, **options)
                self.assertEqual(receipt['status'], 'blocked')
                self.assertLess(datetime.fromisoformat(handoff['checked_at']), datetime.fromisoformat(receipt['created_at']))
                self.assertLessEqual(datetime.fromisoformat(receipt['created_at']), datetime.fromisoformat(receipt['checked_at']))
                row = SimpleNamespace(completion=handoff, completion_sha256=handoff['completion_evidence_sha256'])
                entry = dict(selection['provenance'], nzo_id=selection['nzo_id'])
                evidence, _ = validator(receipt, row, entry)
                self.assertEqual(evidence['status'], 'blocked')


class ReceiptCommandTests(PlainTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('record_kodi_completion_test',
            Path(__file__).resolve().parents[3] / 'tools/record_kodi_completion.py')
        self.cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.cli)
        self.now = datetime.now(timezone.utc)
        self.args = SimpleNamespace(job=1, request='c7ea8e79-7376-4d0c-917e-c0d14aab2327',
            expires=(self.now + timedelta(minutes=5)).isoformat(), renewal='fixture-renewal')
        self.server = {'expires_utc': (self.now + timedelta(minutes=10)).isoformat(),
                       'renewal_id': 'fixture-renewal'}

    def test_expired_replaced_or_extended_sessions_refuse(self):
        self.cli.validate_session(self.args, self.server, self.now)
        for server, now in ((self.server, self.now + timedelta(minutes=6)),
                (dict(self.server, renewal_id='replacement'), self.now),
                (dict(self.server, expires_utc=self.now.isoformat()), self.now)):
            with self.assertRaises(ValueError): self.cli.validate_session(self.args, server, now)

    def test_bounded_json_no_duplicates_nonfinite_and_candidate_path_refuse(self):
        for raw in (b'', b'x' * (self.cli.MAX_BYTES+1), b'{"status":1,"status":2}', b'{"n":NaN}'):
            with self.assertRaises(ValueError): self.cli.read_receipt(io.BytesIO(raw))
        with self.assertRaises(ValueError): self.cli.run(self.args, io.BytesIO(b'{}'))

    def test_live_cli_preflight_rejects_synthetic_unknown_marker_and_unsupported_tuple(self):
        for marker, medium in ((True, 'movie'), (None, 'movie'), (False, 'tv')):
            with self.assertRaises(ValueError):
                self.cli.require_live_receipt({'file': {'test_only': marker},
                    'selection': {'source_identity': {'provider': 'tmdb', 'medium': medium, 'scope': 'film'}}})

    def test_live_cli_accepts_only_supported_tuples_and_all_real_album_files(self):
        for provider, medium, scope in (('tmdb', 'movie', 'film'), ('tmdb', 'tv', 'series_s01e01'),
                                       ('album_request', 'music', 'album_flac')):
            receipt = {'selection': {'source_identity': {
                'provider': provider, 'medium': medium, 'scope': scope}}}
            if medium == 'music':
                receipt['files'] = [{'test_only': False}, {'test_only': False}]
            else:
                receipt['file'] = {'test_only': False}
            self.cli.require_live_receipt(receipt)
            if medium == 'music':
                for files in ([], [None], [{'test_only': False}, {'test_only': True}],
                              [{'test_only': False}, {}]):
                    with self.subTest(files=files), self.assertRaises(ValueError):
                        self.cli.require_live_receipt({**receipt, 'files': files})
