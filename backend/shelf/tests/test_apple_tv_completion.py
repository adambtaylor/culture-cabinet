"""Isolated real ledger fixtures; Apple TV storage never changes Kodi receipts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from shelf import apple_tv_completion as apple, completion_status
from shelf.models import AcquisitionJob, ObtainCompletionStatus, ObtainTransfer
from shelf.tests import test_completion_dispatch as fixtures
from shelf.tests.test_saved import SavedFixture


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class AppleTVTests(SavedFixture, TransactionTestCase):
    # Reuse the established synthetic credential/ledger/hook fixture, not a
    # fabricated production request or an imported manual episode.
    for name in ('login', 'hidden', 'select', 'review', 'submit', 'addfile_calls',
                 'observe_history', 'registered', 'load_hook', 'completed', 'completed_episode'):
        locals()[name] = getattr(fixtures.DispatchTests, name)

    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.series = self.item('tv')
        self.folder = fixtures.tempfile.TemporaryDirectory(prefix='runtime-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.complete = Path(self.folder.name) / 'Complete'
        self.complete.mkdir()
        self.control = Path(self.folder.name) / 'control'
        self.control.mkdir(mode=448)
        self.registry = self.control / 'registry.json'
        self.receipts = Path(self.folder.name) / 'receipts'
        self.receipts.mkdir(mode=448)
        self.policy = fixtures.runtime.SubmissionPolicy(True, str(self.complete), 20000, 4096, True, str(self.registry), True, fixtures.COMPLETION_SCRIPT)
        (self.paths, self.grants, self.reads) = ({}, {}, [])
        for provider in ('hydra', 'sab'):
            tree = fixtures.FakeTree(fixtures.store, provider=provider)
            self.addCleanup(tree.temporary.cleanup)
            metadata = fixtures.grant(provider)
            metadata = fixtures.replace(metadata, binding=fixtures.replace(metadata.binding, actor_id='1', profile_id='1'))
            self.grants[provider] = metadata
            result = fixtures.store.commit_entry(tree.store, metadata, bytearray(b'FICTION-ONLY-' + provider.encode()))
            self.assertEqual(result.outcome, 'stored')
            self.paths[provider] = tree.path
            tree.store.close()

        def opener(*, provider):
            self.reads.append(provider)
            return fixtures.store._fixture_store(self.paths[provider], provider=provider)
        self.http = fixtures.ProviderHTTP()
        self.enterContext(patch.object(fixtures.store, 'open_store', side_effect=opener))
        self.enterContext(patch.object(fixtures.acquisition_backend, 'LoopbackTransport', return_value=fixtures.LoopbackTransport(_connection_factory=self.http.factory)))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.enterContext(override_settings(OBTAIN_SUBMISSION_POLICY=vars(self.policy)))
        self.client = fixtures.Client(enforce_csrf_checks=True)
        self.login()
        self.context = fixtures.acquire.OperatorContext(1, 1, 'offline_test', True)
        self.apple_root = self.control / 'apple-tv-receipts'
        self.enterContext(patch.object(apple, 'ROOT', self.apple_root))

    def page(self, **kw):
        return apple.list_jobs(actor=self.owner, profile_id=1, context=self.context, **kw)

    def value(self):
        return apple._current(self.owner, 1, self.job.pk, self.context, None)[2]

    def receipt(self, status='blocked'):
        value = self.value()
        stamp = datetime.now(timezone.utc).isoformat()
        return {'version': 1, 'destination': 'apple_tv', 'contract_sha256': value['contract_sha256'],
            'request_id': str(self.job.request_id), 'source_sha256': value['source']['sha256'],
            'source_bytes': value['source']['bytes'], 'episode': value['episode'], 'status': status,
            'created_at': stamp, 'checked_at': stamp, 'playback_started': False, 'import_attempts': 0,
            'derivative': None, 'native': None, 'handoff': None, 'library': None, 'reason': 'fixture_blocked'}

    def record(self, receipt, **kw):
        return apple.record_receipt(actor=self.owner, profile_id=1, job_id=self.job.pk,
                                   context=self.context, receipt=receipt, **kw)

    def test_only_completed_pilots_are_listed_without_kodi_delivery_dependency(self):
        row = self.completed_episode()
        # Even malformed unrelated Kodi JSON cannot revoke independent admission.
        row.delivery_state, row.delivery_receipt = 'imported', {'unrelated': 'Kodi'}
        row.save(update_fields=['delivery_state', 'delivery_receipt'])
        page = self.page()
        self.assertEqual((page['version'], page['scope']), (2, 'apple_tv_episodes'))
        jobs = page['jobs']
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]['dispatchable'])
        self.assertEqual(jobs[0]['apple_tv_state'], 'not_reported')
        self.assertEqual(self.value()['episode']['season'], 1)
        self.assertEqual(self.value()['episode']['episode'], 1)
        self.assertFalse(self.apple_root.exists())

    def test_movie_has_no_apple_tv_discovery(self):
        self.completed()
        self.assertEqual(self.page()['jobs'], [])

    def test_record_is_independent_private_atomic_and_exact_replay_is_noop(self):
        row = self.completed_episode()
        before = (row.delivery_state, row.delivery_receipt, row.revision)
        receipt = self.receipt()
        self.record(receipt)
        target = self.apple_root / (str(self.job.request_id) + '.json')
        inode = target.stat().st_ino
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.apple_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.record(receipt), receipt)
        self.assertEqual(target.stat().st_ino, inode)
        row.refresh_from_db()
        self.assertEqual((row.delivery_state, row.delivery_receipt, row.revision), before)
        status = completion_status.status_for(row.transfer)
        self.assertEqual(status['apple_tv_status']['state'], 'blocked')

    def test_foreign_manual_episode_source_and_contract_are_rejected(self):
        self.completed_episode()
        original = self.receipt()
        for key, value in [('contract_sha256', 'a'*64), ('source_sha256', 'b'*64),
                           ('source_bytes', 1), ('playback_started', True)]:
            bad = deepcopy(original)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.record(bad)
        bad = deepcopy(original)
        bad['episode'].update(season=2, basis='owner_confirmed_existing_import')
        with self.assertRaises(ValueError):
            self.record(bad)
        self.assertFalse(self.apple_root.exists())

    def ready_receipt(self, status='unknown'):
        receipt = self.receipt(status)
        value = self.value()
        episode = value['episode']
        title = episode['show'] + ' - S01E01'
        receipt['derivative'] = {'version': 1, 'contract_sha256': value['contract_sha256'],
            'source_sha256': value['source']['sha256'], 'sha256': 'd'*64, 'bytes': 100,
            'remote_path': '/home/tang/Projects/culture-cabinet/.apple-tv-work/' + value['contract_sha256'] + '/episode.mp4',
            'validation': {'valid': True, 'plan_sha256': 'e'*64, 'duration_seconds': 60,
                'metadata': {'title': title, 'show': episode['show'], 'season_number': '1', 'episode_sort': '1',
                    'episode_id': 'S01E01', 'media_type': '10'},
                'atoms': {'video_sample_entry': 'hvc1', 'audio_sample_entry': 'ec-3'}, 'atmos': True,
                'hdr_display_verified': False, 'packet_integrity': {label: {'source_packets': 2,
                    'derivative_packets': 2, 'identical_packet_payload_hashes': True} for label in ('video', 'audio')}}}
        receipt['native'] = {'is_playable': True, 'duration_seconds': 60, 'playback_started': False,
            'tracks': [{'type': 'vide', 'formats': [{'codec': 'hvc1'}]},
                {'type': 'soun', 'language': 'eng', 'formats': [{'codec': 'ec-3'}]}]}
        receipt['import_attempts'] = 1
        receipt['handoff'] = {'path': apple.WATCHFOLDER + '/CC-' + receipt['request_id'] + '-S01E01-' + 'd'*64 + '.mp4',
            'reserved_at': receipt['created_at'], 'delivered_at': receipt['created_at']}
        return receipt

    def test_unknown_only_moves_forward_and_terminal_requires_native_exact_library(self):
        self.completed_episode()
        receipt = self.ready_receipt()
        self.record(receipt)
        backwards = deepcopy(receipt)
        backwards['status'] = 'blocked'
        with self.assertRaises(ValueError):
            self.record(backwards)
        terminal = deepcopy(receipt)
        terminal['status'] = 'library_import_observed'
        with self.assertRaises(ValueError):
            self.record(terminal)
        value = self.value()
        terminal['library'] = {'path': apple.LIBRARY_ROOT + '/fixture.mp4', 'sha256': 'd'*64, 'bytes': 100,
            'metadata': {**{key: value['episode'][key] for key in ('show', 'season', 'episode')},
                'title': terminal['derivative']['validation']['metadata']['title']}, 'native': terminal['native']}
        self.record(terminal)
        self.assertFalse(self.page()['jobs'][0]['dispatchable'])
        changed = deepcopy(terminal)
        changed['reason'] = 'cannot_replace_terminal'
        with self.assertRaises(ValueError):
            self.record(changed)

    def test_symlink_store_or_receipt_cannot_be_followed(self):
        self.completed_episode()
        receipt = self.receipt()
        self.apple_root.symlink_to(self.receipts, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.record(receipt)
        self.apple_root.unlink()
        self.apple_root.mkdir(mode=0o700)
        target = self.apple_root / (str(self.job.request_id) + '.json')
        target.symlink_to(self.registry)
        with self.assertRaises((OSError, ValueError)):
            self.record(receipt)

    def test_revoked_session_before_publish_leaves_no_receipt(self):
        self.completed_episode()
        receipt = self.receipt()
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('fixture session revoked')
        with self.assertRaises(ValueError):
            self.record(receipt, session_guard=guard)
        self.assertEqual(list(self.apple_root.glob('*.json')), [])

    def test_export_rechecks_current_bytes_but_keeps_recorded_contract_timestamp(self):
        self.completed_episode()
        original = self.value()
        from shelf import completion_observer
        with patch.object(completion_observer, 'read_history', return_value=self.slot):
            # observe's default reader is bound at definition; inject via wrapper.
            observed = completion_observer.observe(actor=self.owner, profile_id=1,
                job_id=self.job.pk, context=self.context, reader=lambda nzo: self.slot)
        with patch.object(completion_observer, 'observe', return_value=observed):
            exported = apple.export_job(actor=self.owner, profile_id=1, job_id=self.job.pk, context=self.context)
        self.assertEqual(exported, original)
        with patch.object(completion_observer, 'observe', return_value={'state': 'unknown'}), self.assertRaises(ValueError):
            apple.export_job(actor=self.owner, profile_id=1, job_id=self.job.pk, context=self.context)

    def test_actual_consumer_receipts_with_delayed_producer_observation(self):
        self.actual_consumer_receipts('series_s01e01')

    def test_actual_s02e02_consumer_receipts_with_delayed_producer_observation(self):
        self.actual_consumer_receipts('episode_s02e02')

    def actual_consumer_receipts(self, scope):
        row = self.completed_episode(scope)
        page = self.page()
        self.assertEqual((page['version'], page['scope']), (2, 'apple_tv_episodes'))
        self.assertEqual(page['jobs'][0]['source_identity']['scope'], scope)
        value = self.value()
        tools = str(Path(__file__).resolve().parents[3] / 'tools')
        self.enterContext(patch.object(sys, 'path', [tools, *sys.path]))
        import mac_apple_tv_consumer as consumer
        import mac_kodi_consumer as core
        from apple_tv_media import episode_metadata
        # Linux test runtime has no owner Mac helper scripts. Keep actual
        # no-follow JSON reads and duplicate-key rejection; inject only those
        # helpers, following the existing consumer/backend boundary fixture.
        helper = SimpleNamespace(date=datetime.fromisoformat, unique_object=apple.registry.unique_object)
        sync = SimpleNamespace(validate_relative=PurePosixPath)
        self.enterContext(patch.object(core, 'helpers', return_value=(helper, sync)))
        now = row.observed_at + timedelta(minutes=5)
        watch = self.control / 'Media' / 'Automatically Add to TV.localized'
        watch.mkdir(parents=True)
        library_root = self.control / 'Library'
        library_root.mkdir()
        self.enterContext(patch.object(consumer, 'WATCHFOLDER', watch))
        self.enterContext(patch.object(apple, 'WATCHFOLDER', str(watch)))
        self.enterContext(patch.object(apple, 'LIBRARY_ROOT', str(library_root)))
        self.enterContext(patch.object(apple.timezone, 'now', return_value=now + timedelta(minutes=5)))
        payload = b'Synthetic derivative payload, never playable.'
        result = self.ready_receipt()['derivative']
        # Use the real producer metadata algorithm, independently of backend
        # expectations, to catch neutral-title contract drift across workers.
        result['validation']['metadata'] = episode_metadata(value['episode'])
        self.assertEqual(result['validation']['metadata']['title'], value['episode']['show'] + ' - ' + apple.episode_code(scope))
        result.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
        native = self.ready_receipt()['native']
        observed, snapshots = [], []
        original_save = consumer.save_receipt
        def save(path, receipt):
            snapshots.append(deepcopy(receipt))
            original_save(path, receipt)
        def transfer(derivative, path, **kwargs):
            path.write_bytes(payload)
        def library(episode, derivative, **kwargs):
            return deepcopy(observed[0]) if observed else None
        clock_calls = 0
        def advancing_clock():
            nonlocal clock_calls
            clock_calls += 1
            return now + timedelta(milliseconds=clock_calls)
        options = dict(execute=True, remux=lambda *a, **kw: result, transfer=transfer,
            native_probe=lambda *a, **kw: native, library=library, clock=advancing_clock)
        state = self.control / 'consumer'
        self.enterContext(patch.object(consumer, 'save_receipt', side_effect=save))
        pending = consumer.consume(value, state, **options)
        self.assertEqual(pending['status'], 'handoff_pending')
        # Intermediate remux readiness is a distinct truthful observation.
        self.assertIn('remux_ready', {item['status'] for item in snapshots})
        for receipt in snapshots:
            apple.validate_receipt(receipt, value)
        self.record(pending)
        # Simulate TV consuming the watchfolder entry before exact library proof.
        Path(pending['handoff']['path']).unlink()
        unknown = consumer.consume(value, state, **options)
        self.assertEqual(unknown['status'], 'unknown')
        self.record(unknown)
        observed.append({'path': str(library_root / 'fixture.mp4'), 'bytes': result['bytes'],
            'sha256': result['sha256'], 'metadata': {**{key: value['episode'][key] for key in ('show', 'season', 'episode')},
                'title': result['validation']['metadata']['title']}, 'native': native})
        terminal = consumer.consume(value, state, **options)
        self.assertEqual(terminal['status'], 'library_import_observed')
        # Include checkpoints from reconciliation too, not just final results.
        # These objects are exactly what a crash could leave on disk.
        for receipt in snapshots:
            apple.validate_receipt(receipt, value)
        reservations = [receipt for receipt in snapshots if receipt['handoff']
                        and receipt['handoff']['delivered_at'] is None]
        self.assertTrue(reservations)
        self.assertLess(datetime.fromisoformat(reservations[0]['created_at']),
                        datetime.fromisoformat(reservations[0]['handoff']['reserved_at']))
        self.assertTrue(any(receipt['handoff'] and receipt['handoff']['delivered_at']
                            for receipt in snapshots))
        self.record(terminal)
        self.assertEqual(apple.status_for(row.transfer)['status'], 'library_import_observed')

    def test_rendered_status_distinguishes_watchfolder_from_observed_library(self):
        from django.template.loader import render_to_string
        completion = {'kind': 'episode', 'supported_consumer': True,
            'delivery_state': 'not_reported', 'apple_tv_status': {'status': 'handoff_pending'}}
        context = {'acquisition_job': {'id': 1, 'completion_status': completion}}
        pending = render_to_string('cabinet/saved_items.html', context)
        self.assertIn('Apple TV watchfolder handoff is pending.', pending)
        self.assertIn('library import has not been observed.', pending)
        self.assertNotIn('Apple TV library import was observed at', pending)
        self.assertIn('Kodi import has not been reported.', pending)
        completion['apple_tv_status'] = {'status': 'library_import_observed', 'checked_at': '2026-10-31T12:00:00Z'}
        observed = render_to_string('cabinet/saved_items.html', context)
        self.assertIn('Apple TV library import was observed at 2026-10-31T12:00:00Z.', observed)
        self.assertIn('playback and HDR display output remain unverified.', observed)
        self.assertNotIn('Apple TV watchfolder handoff is pending.', observed)
        self.assertIn('Kodi import has not been reported.', observed)

    def test_neutral_episode_title_is_not_duplicated_in_derivative_metadata(self):
        self.completed_episode()
        receipt = self.ready_receipt()
        value = self.value()
        self.assertEqual(value['episode']['title'], 'S01E01')
        self.assertEqual(receipt['derivative']['validation']['metadata']['title'],
                         value['episode']['show'] + ' - ' + apple.episode_code(self.job.scope))
        apple.validate_receipt(receipt, value)
        duplicated = deepcopy(receipt)
        duplicated['derivative']['validation']['metadata']['title'] += ' - S01E01'
        with self.assertRaises(ValueError):
            apple.validate_receipt(duplicated, value)

    def test_stale_early_pilot_does_not_starve_later_verified_job_or_leak_binding(self):
        # Deliberately stale synthetic ledger row with no selected result. Its
        # incomplete binding must not prevent later real fixture discovery.
        stale = AcquisitionJob.objects.create(actor=self.owner, profile_id=1, saved_item=self.series,
            request_id=uuid4(), active_key=uuid4().hex, provider='tmdb', medium='tv', source_id=999999,
            title='Synthetic stale pilot', scope='series_s01e01', edition='Synthetic stale edition',
            storage='local_library', authorized_content=True, storage_check=True, reason='synthetic')
        ObtainTransfer.objects.create(job=stale, binding={'exact': {'size': 1}, 'private': 'never export this'},
            result_guid='987654321', nzb_sha256='f'*64, state='queued', provider_job='SABnzbd_nzo_stale_fixture')
        row = self.completed_episode()
        page = self.page()
        self.assertEqual(page['jobs'][0], {'job_id': stale.pk, 'state': 'unknown',
            'reason': 'binding_unavailable', 'dispatchable': False})
        self.assertEqual(page['jobs'][1]['job_id'], row.transfer_id)
        self.assertTrue(page['jobs'][1]['dispatchable'])
        self.assertNotIn('never export this', str(page))
        first = self.page(limit=1)
        self.assertEqual(first['next_cursor'], stale.pk)
        self.assertTrue(first['has_more'])
        second = self.page(after=first['next_cursor'], limit=1)
        self.assertEqual(second['jobs'][0]['job_id'], row.transfer_id)
        self.assertFalse(second['has_more'])

    def test_listing_session_failure_revokes_entire_page(self):
        self.completed_episode()
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('synthetic session ended before job binding')
        with self.assertRaises(ValueError):
            self.page(session_guard=guard)
        self.assertEqual(len(calls), 2)
