"""Offline dispatcher tests: synthetic producer/consumer receipts, never Kodi/SSH."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import json
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import mac_completion_dispatcher as dispatch
import mac_episode_consumer as episode
import mac_album_consumer as album
from test_mac_album_consumer import fixture as album_fixture, FakeTransfer as AlbumTransfer, FakeKodi as AlbumKodi

NOW = datetime(2026, 10, 30, 7, tzinfo=timezone.utc)


def fixture(number=1):
    request = f'{number:08d}-1111-4111-8111-111111111111'
    provenance = {'request_id': request, 'result_id': f'{number:08d}-2222-4222-8222-222222222222',
                  'result_digest': 'a' * 64, 'nzb_sha256': 'b' * 64}
    identity = {'provider': 'tmdb', 'medium': 'movie', 'source_id': number, 'scope': 'film',
                'title': 'SYNTHETIC movie', 'edition': 'SYNTHETIC release',
                'basis': 'selected_catalogue_identity_not_content_verification'}
    item = {'media_identity': 'sha256:' + 'c' * 64, 'relative_path': 'CC-' + request + '/movie.mkv',
        'sha256': 'c' * 64, 'size': 12, 'nzo_id': 'SABnzbd_nzo_synthetic' + str(number), 'provenance': provenance,
        'selected_scope': 'film', 'observed_media': 'video', 'observed_media_basis': 'filename_extension_only',
        'content_identity': 'unverified', 'full_work_verified': False,
        'verified_at': '2026-10-29T09:00:00.900000+00:00', 'acquired_at': '2026-10-29T09:00:00+00:00',
        'acquired_at_basis': 'sab_history_completed_at', 'test_only': False,
        'kodi_music_catalog_eligible': False, 'indexed': False, 'playback_started': False,
        'reverify_before_playback': True}
    evidence = {'source': 'sab_history', 'nzo_id': item['nzo_id'], 'acquired_at': item['acquired_at'],
        'acquired_at_basis': item['acquired_at_basis'], 'provenance': provenance,
        'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')}], 'source_identity': identity}
    handoff = {'version': 2, 'state': 'COMPLETED_FILES_VERIFIED', 'checked_at': NOW.isoformat(),
        'authorization': {'scope': 'film', 'edition': identity['edition'], 'authorized_content': True},
        'sab_history_completed': True, 'files': [item], 'source_identity': identity,
        'completion_evidence': evidence, 'completion_evidence_sha256': dispatch.consumer.digest(evidence, newline=True)}
    job = {'job_id': number, **provenance, 'nzo_id': item['nzo_id'], 'source_identity': identity,
           'completion_evidence_sha256': None, 'state': 'pending', 'reason': 'final_history_not_observed',
           'dispatchable': True, 'test_only': False, 'delivery_state': 'not_reported', 'delivery_sha256': None}
    return job, handoff


def tv_fixture(number=2, scope='series_s01e01'):
    job, handoff = fixture(number)
    identity = dict(job['source_identity'], medium='tv', scope=scope, title='SYNTHETIC show')
    job['source_identity'] = handoff['source_identity'] = identity
    handoff['authorization'].update(scope=scope)
    handoff['files'][0]['selected_scope'] = scope
    handoff['completion_evidence']['source_identity'] = identity
    handoff['completion_evidence_sha256'] = dispatch.consumer.digest(handoff['completion_evidence'], newline=True)
    return job, handoff


class FakeRuntime:
    def __init__(self, root):
        self.root, self.now = root, NOW
        self.jobs = [fixture()[0]]
        self.handoffs = {1: fixture()[1]}
        self.statuses = ['imported']
        self.calls, self.imports, self.feedback = [], [], []
        self.fail_feedback = False
        self.interrupt_consumer = False
        self.interrupt_ack = False
        self.export_state = 'completed'
        self.foreign_export = False
        self.foreign_receipt = False
        self.page_change = lambda page: page
        self.listing_version = 1
        self.album_kodi_factory = AlbumKodi
        self.album_kodis = []

    def clock(self):
        return self.now

    def run(self, command, payload=b'', **options):
        self.calls.append((command, payload, options))
        if command[0] != 'ssh':
            args = command
            handoff = dispatch.consumer.read_json(Path(args[args.index('--handoff') + 1]), private=True)
            selection = dispatch.consumer.read_json(Path(args[args.index('--selection') + 1]), private=True)
            state = Path(args[args.index('--state-dir') + 1])
            cache = args[args.index('--cache') + 1]
            module = {'tv': episode, 'music': album}.get(selection['source_identity']['medium'], dispatch.consumer)
            job = module.validate(handoff, selection, self.now)
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = state / (job['request_id'] + '.json')
            before = dispatch.consumer.read_json(path, private=True) if path.exists() else None
            self.imports.append((job['request_id'], before))
            if module is album:
                directory = Path(cache) / ('CC-' + job['request_id'])
                kodi = self.album_kodi_factory(directory)
                self.album_kodis.append(kodi)
                with patch.object(album, 'datetime', types.SimpleNamespace(now=lambda tz: self.now)):
                    receipt = album.consume(handoff, selection, cache=Path(cache), state_dir=state,
                        execute=True, transfer=AlbumTransfer(), art_transfer=AlbumTransfer(),
                        sidecar_verify=lambda relative, **expected: {'sha256': expected['expected_sha256'],
                            'bytes': expected['expected_bytes']}, kodi=kodi, timeout=0, now=self.now)
                return 0, dispatch.consumer.encoded(receipt)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            receipt = {'version': 1, 'request_id': job['request_id'], 'selection': selection,
                'file': job['file'], 'hook_sha256': job['hook_sha256'],
                'completion_evidence_sha256': job['completion_evidence_sha256'],
                'signature': dispatch.consumer.digest([job['signature'], cache]), 'status': status,
                'scan_key': 'video-' + job['request_id'], 'scan_attempts': int(status != 'blocked'),
                'created_at': NOW.isoformat(), 'checked_at': self.now.isoformat(), 'playback_started': False,
                'library': None}
            if status == 'imported':
                receipt['local_path'] = cache + '/' + job['file']['relative_path']
                receipt['library'] = {'movieid': 7, 'file': receipt['local_path'],
                                      'uniqueid': {'tmdb': str(selection['source_identity']['source_id'])}}
            if module is episode:
                receipt['scan_key'] = 'episode-' + job['request_id']
                receipt['probe'] = {'validated_sha256': job['file']['sha256'], 'video_codec': 'synthetic', 'duration': 60.0}
                if status == 'imported':
                    receipt['local_path'] = str(Path(cache) / ('CC-' + job['request_id']) / (episode.episode_code(selection['source_identity']['scope']) + '.mkv'))
                    receipt['library'] = {'episodeid': 8, 'tvshowid': 9, 'file': receipt['local_path'],
                        'season': episode.parse_scope(selection['source_identity']['scope'])[0],
                        'episode': episode.parse_scope(selection['source_identity']['scope'])[1], 'uniqueid': {'cabinet': episode.episode_id(selection)},
                        'show_uniqueid': {'tmdb': str(selection['source_identity']['source_id'])}}
                    receipt['readiness'] = dict(receipt['probe'], file_verified_at=self.now.isoformat(),
                                                library_observed_at=self.now.isoformat())
            if self.foreign_receipt:
                receipt['request_id'] = fixture(9)[0]['request_id']
            dispatch.consumer.save_receipt(path, receipt)
            if self.interrupt_consumer:
                raise KeyboardInterrupt
            return 0, dispatch.consumer.encoded(receipt)
        args = shlex.split(command[-1])
        script = Path(args[2]).name
        if script == 'list_completion_jobs.py':
            after = int(args[args.index('--after') + 1])
            available = [j for j in self.jobs if j['job_id'] > after]
            jobs = available[:16]
            page = {'version': 1, 'state': 'listed', 'actor_id': 1, 'profile_id': 1,
                'scope': 'movie_film_only', 'after': after, 'limit': 16, 'has_more': len(available) > 16,
                'next_cursor': jobs[-1]['job_id'] if jobs else after, 'jobs': jobs}
            if self.listing_version == 2:
                page.update(version=2, scope='supported_media')
            return 0, dispatch.consumer.encoded(self.page_change(page))
        number = int(args[args.index('--job') + 1])
        if script == 'export_completed_job.py':
            job = next(j for j in self.jobs if j['job_id'] == number)
            result = {key: job[key] for key in dispatch.BINDING if key != 'source_identity'}
            result['state'] = self.export_state
            result['handoff'] = deepcopy(self.handoffs[number])
            result['handoff']['checked_at'] = self.now.isoformat()
            if self.foreign_export:
                result['result_digest'] = 'd' * 64
            return 0, dispatch.consumer.encoded(result)
        assert script == 'record_kodi_completion.py'
        receipt = dispatch.decode(payload)
        self.feedback.append(payload)
        if self.fail_feedback:
            raise TimeoutError('SYNTHETIC lost SSH response')
        if self.interrupt_ack:
            raise KeyboardInterrupt
        return 0, dispatch.consumer.encoded({'recorded': True, 'state': receipt['status'],
                                            'job': number, 'revision': 1, 'playback_started': False})


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        helper = types.SimpleNamespace(unique_object=dispatch.unique, date=dispatch.stamp)
        sync = types.SimpleNamespace(validate_relative=lambda value: PurePosixPath(value))
        helper_patch = patch.object(dispatch.consumer, 'helpers', return_value=(helper, sync))
        helper_patch.start()
        self.addCleanup(helper_patch.stop)
        self.runtime = FakeRuntime(self.root)
        self.app = self.restart()

    def restart(self):
        return dispatch.Dispatcher(dispatch.EXPIRY, dispatch.RENEWAL, root=self.root / 'state',
            cache=self.root / 'cache', tv_cache=self.root / 'TV Shows', album_cache=self.root / 'Albums', runner=self.runtime.run, clock=self.runtime.clock)

    def advance(self):
        self.runtime.now += timedelta(seconds=61)

    def record(self, number=1):
        return self.app.read(self.app.jobs / f'{number}.json')

    def test_import_ack_is_terminal_and_repeated_listing_is_noop(self):
        self.app.tick(); self.advance(); self.restart().tick()
        self.assertEqual(len(self.runtime.imports), 1)
        self.assertEqual(len(self.runtime.feedback), 1)
        record = self.record()
        self.assertEqual(record['ack_state'], 'imported')
        self.assertEqual(record['ack_digest'], dispatch.consumer.digest(dispatch.decode(self.runtime.feedback[0])))
        self.assertIsNone(record['pending'])
        self.assertEqual((self.app.jobs / '1.json').stat().st_mode & 0o777, 0o600)

    def test_lost_feedback_retries_exact_bytes_without_another_consumer(self):
        self.runtime.fail_feedback = True
        self.app.tick()
        pending = self.record()['pending'].encode()
        self.assertEqual(pending, self.runtime.feedback[0])
        self.advance(); self.restart().tick()
        self.assertEqual(self.runtime.feedback, [pending, pending])
        self.assertEqual(len(self.runtime.imports), 1)
        self.runtime.fail_feedback = False
        self.advance(); self.restart().tick()
        self.assertEqual(self.record()['ack_state'], 'imported')
        self.assertEqual(self.runtime.feedback, [pending] * 3)
        self.assertEqual(len(self.runtime.imports), 1)

    def test_interrupt_after_consumer_recovers_receipt_before_new_side_effect(self):
        self.runtime.interrupt_consumer = True
        with self.assertRaises(KeyboardInterrupt):
            self.app.tick()
        self.assertEqual(self.record()['phase'], 'reserved')
        self.runtime.interrupt_consumer = False
        self.advance(); self.restart().tick()
        # Recovery durably queues first; the new feedback backoff is deliberate.
        self.assertEqual(len(self.runtime.imports), 1)
        self.advance(); self.restart().tick()
        self.assertEqual(self.record()['ack_state'], 'imported')
        self.assertEqual(len(self.runtime.imports), 1)

    def test_interrupted_scan_keeps_single_scan_marker_for_reconciliation(self):
        self.runtime.statuses = ['scanning', 'imported']
        self.runtime.interrupt_consumer = True
        with self.assertRaises(KeyboardInterrupt):
            self.app.tick()
        self.runtime.interrupt_consumer = False
        self.advance(); self.restart().tick()
        self.assertEqual(len(self.runtime.imports), 2)
        self.assertEqual(self.runtime.imports[1][1]['status'], 'scanning')
        self.assertEqual(self.runtime.imports[1][1]['scan_attempts'], 1)
        self.assertEqual(self.record()['ack_state'], 'imported')

    def test_bad_feedback_acknowledgement_preserves_pending_bytes(self):
        original = self.runtime.run
        def wrong_ack(command, payload=b'', **options):
            result = original(command, payload, **options)
            if command[0] == 'ssh' and 'record_kodi_completion.py' in command[-1]:
                return 0, dispatch.consumer.encoded({'recorded': True, 'job': 2,
                    'state': 'imported', 'playback_started': False})
            return result
        self.app.runner = wrong_ack
        self.app.tick()
        self.assertEqual(self.record()['ack_state'], 'not_reported')
        self.assertEqual(self.record()['pending'].encode(), self.runtime.feedback[0])
        self.advance(); self.restart().tick()
        self.assertEqual(len(self.runtime.imports), 1)
        self.assertEqual(self.runtime.feedback[0], self.runtime.feedback[1])

    def test_expiry_during_export_prevents_consumer_launch(self):
        original = self.runtime.run
        def expires_during_export(command, payload=b'', **options):
            result = original(command, payload, **options)
            if command[0] == 'ssh' and 'export_completed_job.py' in command[-1]:
                self.runtime.now = dispatch.stamp(dispatch.EXPIRY)
            return result
        self.app.runner = expires_during_export
        self.app.tick()
        self.assertEqual(self.runtime.imports, [])
        self.assertEqual(self.runtime.feedback, [])

    def test_interrupt_after_feedback_send_preserves_identical_pending_payload(self):
        self.runtime.interrupt_ack = True
        with self.assertRaises(KeyboardInterrupt):
            self.app.tick()
        payload = self.runtime.feedback[0]
        self.runtime.interrupt_ack = False
        self.advance(); self.restart().tick()
        self.assertEqual(self.runtime.feedback, [payload, payload])
        self.assertEqual(len(self.runtime.imports), 1)

    def test_same_state_timestamps_suppressed_and_transitions_monotonic(self):
        self.runtime.statuses = ['blocked', 'blocked', 'unknown', 'unknown', 'imported']
        for _ in range(5):
            self.app.tick(); self.advance()
        states = [dispatch.decode(raw)['status'] for raw in self.runtime.feedback]
        self.assertEqual(states, ['blocked', 'unknown', 'imported'])
        self.assertEqual(len(self.runtime.imports), 5)
        self.assertEqual(self.runtime.imports[-1][1]['scan_attempts'], 1)
        self.assertEqual(self.record()['ack_state'], 'imported')

    def test_blocked_backoff_and_unavailable_export_retry_without_fake_receipt(self):
        self.runtime.export_state = 'unknown'
        self.app.tick(); self.app.tick()
        exports = [c for c in self.runtime.calls if 'export_completed_job.py' in c[0][-1]]
        self.assertEqual(len(exports), 1)
        self.assertEqual(self.runtime.imports, [])
        self.assertEqual(self.runtime.feedback, [])
        self.runtime.export_state = 'completed'
        self.advance(); self.app.tick()
        self.assertEqual(len(self.runtime.imports), 1)

    def test_foreign_listing_and_export_refused_before_import(self):
        self.runtime.page_change = lambda p: dict(p, profile_id=2)
        with self.assertRaises(ValueError):
            self.app.tick()
        self.runtime.page_change = lambda p: p
        self.runtime.foreign_export = True
        self.app.tick()
        self.assertEqual(self.runtime.imports, [])
        self.assertEqual(self.runtime.feedback, [])

    def test_foreign_consumer_receipt_never_forwarded(self):
        self.runtime.foreign_receipt = True
        self.app.tick()
        self.assertEqual(len(self.runtime.imports), 1)
        self.assertEqual(self.runtime.feedback, [])
        self.assertEqual(self.record()['phase'], 'reserved')

    def test_missing_unknown_receipt_cannot_start_a_new_scan(self):
        self.runtime.jobs[0]['delivery_state'] = 'unknown'
        self.app.tick()
        self.assertEqual(self.runtime.imports, [])
        self.assertEqual(self.runtime.feedback, [])

    def test_changed_evidence_or_selected_identity_cannot_import(self):
        self.runtime.jobs[0]['completion_evidence_sha256'] = 'e' * 64
        self.app.tick()
        self.assertEqual(self.runtime.imports, [])
        self.runtime.jobs[0]['completion_evidence_sha256'] = None
        self.runtime.handoffs[1]['source_identity'] = dict(self.runtime.handoffs[1]['source_identity'], source_id=9)
        self.advance(); self.app.tick()
        self.assertEqual(self.runtime.imports, [])

    def test_pagination_advances_past_unavailable_jobs_and_resets(self):
        self.runtime.jobs = [fixture(i)[0] for i in range(1, 19)]
        self.runtime.handoffs = {i: fixture(i)[1] for i in range(1, 19)}
        self.runtime.export_state = 'pending'
        self.app.tick()
        self.assertEqual(self.app.read(self.app.root / 'cursor.json')['after'], 16)
        self.advance(); self.app.tick()
        self.assertEqual(self.app.read(self.app.root / 'cursor.json')['after'], 0)
        exports = [shlex.split(c[0][-1]) for c in self.runtime.calls if 'export_completed_job.py' in c[0][-1]]
        self.assertEqual([int(a[a.index('--job') + 1]) for a in exports], list(range(1, 19)))

    def test_duplicate_rows_and_job_aliases_cannot_duplicate_import(self):
        self.runtime.page_change = lambda p: dict(p, jobs=p['jobs'] * 2)
        with self.assertRaises(ValueError):
            self.app.tick()
        self.assertEqual(self.runtime.imports, [])
        self.runtime.page_change = lambda p: p
        self.runtime.jobs.append(dict(self.runtime.jobs[0], job_id=2))
        self.app.tick()
        self.assertEqual(len(self.runtime.imports), 1)
        self.assertEqual(len(self.runtime.feedback), 1)

    def test_stuck_feedback_does_not_starve_unrelated_jobs(self):
        self.runtime.jobs.append(fixture(2)[0]); self.runtime.handoffs[2] = fixture(2)[1]
        self.runtime.fail_feedback = True
        self.app.tick(); self.advance(); self.app.tick()
        self.assertEqual(len(self.runtime.imports), 2)
        self.assertEqual([row[0] for row in self.runtime.imports], [fixture(i)[0]['request_id'] for i in (1, 2)])
        self.assertEqual(self.record(1)['phase'], 'feedback')
        self.assertEqual(self.record(2)['phase'], 'feedback')

    def test_v2_movie_and_tv_use_separate_consumers_caches_and_states(self):
        job, handoff = tv_fixture()
        self.runtime.jobs.append(job); self.runtime.handoffs[2] = handoff
        self.runtime.listing_version = 2
        self.app.tick()
        self.assertEqual(len(self.runtime.imports), 2)
        launches = [command for command, _, _ in self.runtime.calls if command[0] != 'ssh']
        self.assertEqual([Path(command[2]).name for command in launches],
                         ['mac_kodi_consumer.py', 'mac_episode_consumer.py'])
        self.assertEqual(Path(launches[0][launches[0].index('--state-dir') + 1]), self.app.consumer_state)
        self.assertEqual(Path(launches[1][launches[1].index('--state-dir') + 1]), self.app.consumer_state / 'tv')
        self.assertEqual(Path(launches[1][launches[1].index('--cache') + 1]), self.root / 'TV Shows')
        self.assertEqual(self.record(2)['ack_state'], 'imported')
        self.advance(); self.restart().tick()
        self.assertEqual(len(self.runtime.imports), 2)

    def test_explicit_s02e02_dispatch_uses_persisted_binding_and_frozen_feedback(self):
        job, handoff = tv_fixture(scope='episode_s02e02')
        self.runtime.jobs = [job]
        self.runtime.handoffs = {2: handoff}
        self.runtime.listing_version = 2
        self.runtime.fail_feedback = True
        self.app.tick()
        self.assertEqual(len(self.runtime.imports), 1)
        frozen = self.record(2)['pending'].encode()
        self.assertEqual(json.loads(frozen)['library']['season'], 2)
        self.assertEqual(json.loads(frozen)['library']['episode'], 2)
        self.advance()
        self.runtime.fail_feedback = False
        self.restart().tick()
        self.assertEqual(self.runtime.feedback, [frozen, frozen])
        self.assertEqual(self.record(2)['ack_state'], 'imported')
        self.assertEqual(len(self.runtime.imports), 1)

    def test_album_uses_full_manifest_identity_and_separate_consumer_state(self):
        handoff, selection = album_fixture(request_id=fixture(3)[0]['request_id'])
        for item in handoff['files']:
            item['test_only'] = False
        job = dict(fixture(3)[0], **selection['provenance'], source_identity=selection['source_identity'],
                   album_identity=selection['album_identity'], nzo_id=selection['nzo_id'])
        self.runtime.jobs.append(job); self.runtime.handoffs[3] = handoff
        self.runtime.listing_version = 2
        self.app.tick()
        self.assertEqual(len(self.runtime.imports), 2)
        launches = [command for command, _, _ in self.runtime.calls if command[0] != 'ssh']
        command = launches[1]
        self.assertEqual(Path(command[2]).name, 'mac_album_consumer.py')
        self.assertEqual(Path(command[command.index('--state-dir') + 1]), self.app.consumer_state / 'album')
        self.assertEqual(Path(command[command.index('--cache') + 1]), self.root / 'Albums')
        receipt = dispatch.decode(self.runtime.feedback[1])
        self.assertEqual(receipt['files'], handoff['files'])
        self.assertEqual(receipt['album_identity'], selection['album_identity'])
        self.assertEqual(receipt['status'], 'imported')
        self.assertEqual(self.record(3)['ack_state'], 'imported')
        self.advance(); self.restart().tick()
        self.assertEqual(len(self.runtime.imports), 2)


    def test_acknowledged_partial_album_readback_reconciles_without_a_scan(self):
        handoff, selection = album_fixture(request_id=fixture(3)[0]['request_id'])
        for item in handoff['files']:
            item['test_only'] = False
        job = dict(fixture(3)[0], **selection['provenance'], source_identity=selection['source_identity'],
                   album_identity=selection['album_identity'], nzo_id=selection['nzo_id'])
        self.runtime.jobs = [job]; self.runtime.handoffs = {3: handoff}
        self.runtime.listing_version = 2

        def preexisting_songs(directory):
            kodi = AlbumKodi(directory)
            # Kodi already knows one exact track on the first observation; the
            # next run sees the completed external indexing of the second track.
            count = 1 if not self.runtime.album_kodis else 2
            kodi.songs = deepcopy(kodi.rows[:count])
            return kodi
        self.runtime.album_kodi_factory = preexisting_songs
        self.app.tick()
        first = dispatch.decode(self.runtime.feedback[0])
        self.assertEqual(self.record(3)['ack_state'], 'unknown')
        self.assertEqual((first['scan_attempts'], first['readback_seen']), (0, True))
        self.assertEqual(self.runtime.album_kodis[0].scans, [])

        self.advance(); self.restart().tick()
        self.assertEqual(self.record(3)['ack_state'], 'imported')
        self.assertEqual([dispatch.decode(raw)['status'] for raw in self.runtime.feedback], ['unknown', 'imported'])
        self.assertEqual(len(self.runtime.imports), 2)
        self.assertEqual(self.runtime.imports[1][1]['scan_attempts'], 0)
        self.assertTrue(self.runtime.imports[1][1]['readback_seen'])
        for kodi in self.runtime.album_kodis:
            self.assertEqual(kodi.scans, [])
            self.assertLessEqual({method for method, _ in kodi.calls},
                                 {'AudioLibrary.GetSongs', 'AudioLibrary.GetAlbumDetails'})
        final = dispatch.decode(self.runtime.feedback[-1])
        self.assertEqual(final['scan_attempts'], 0)


    def test_legacy_listing_cannot_authorize_tv(self):
        job, handoff = tv_fixture()
        self.runtime.jobs = [job]; self.runtime.handoffs = {2: handoff}
        with self.assertRaises(ValueError):
            self.app.tick()
        self.assertEqual(self.runtime.imports, [])

    def test_tv_feedback_replays_exact_bytes_before_same_job_reconciliation(self):
        job, handoff = tv_fixture()
        self.runtime.jobs = [job]; self.runtime.handoffs = {2: handoff}
        self.runtime.listing_version = 2
        self.runtime.statuses = ['unknown', 'imported']
        self.runtime.fail_feedback = True
        self.app.tick()
        pending = self.record(2)['pending'].encode()
        self.advance(); self.restart().tick()
        self.assertEqual(self.runtime.feedback, [pending, pending])
        self.assertEqual(len(self.runtime.imports), 1)
        self.runtime.fail_feedback = False
        self.advance(); self.restart().tick()
        self.assertEqual(self.record(2)['ack_state'], 'unknown')
        self.advance(); self.restart().tick()
        self.assertEqual(self.record(2)['ack_state'], 'imported')
        self.assertEqual(self.runtime.imports[1][1]['scan_attempts'], 1)


    def test_corrupt_journal_is_preserved_and_cannot_be_silently_reset(self):
        path = self.app.jobs / '1.json'
        path.write_bytes(b'{"incomplete":')
        path.chmod(0o600)
        self.runtime.jobs.append(fixture(2)[0]); self.runtime.handoffs[2] = fixture(2)[1]
        self.app.tick()
        self.assertEqual(path.read_bytes(), b'{"incomplete":')
        self.assertEqual(self.runtime.imports, [])
        self.assertGreater(self.app.read(self.app.root / 'status.json')['counts']['job_errors'], 0)

    def test_private_health_reports_zero_eligible_without_claiming_import(self):
        self.runtime.jobs = [{'job_id': 1, 'state': 'failed', 'dispatchable': False}]
        self.app.tick()
        health = self.app.read(self.app.root / 'status.json')
        self.assertEqual(health['listing'], 'listed')
        self.assertEqual(health['counts'], {'listed': 1, 'eligible': 0, 'consumer_attempts': 0,
                                          'feedback_pending': 0, 'job_errors': 0})
        self.assertEqual(health['expires_at'], dispatch.EXPIRY)
        self.assertIsInstance(health['pid'], int)
        self.assertEqual((self.app.root / 'status.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.app.root), json.dumps(health))

    def test_failed_listing_is_explicit_in_health_and_once_exit(self):
        self.runtime.page_change = lambda p: dict(p, actor_id=2)
        with self.assertRaises(ValueError):
            self.app.tick()
        self.assertEqual(self.app.read(self.app.root / 'status.json')['listing'], 'unavailable')
        with patch.object(dispatch, 'STATE', self.app.root), \
             patch.object(dispatch, 'utcnow', return_value=NOW), \
             patch.object(dispatch, 'Dispatcher', return_value=self.app), \
             patch.object(dispatch.signal, 'signal'):
            self.assertEqual(dispatch.main(['--expires', dispatch.EXPIRY, '--renewal', dispatch.RENEWAL, '--once']), 1)

    def test_session_expiry_and_wrong_renewal_refuse_all_child_calls(self):
        with self.assertRaises(ValueError):
            dispatch.Dispatcher(dispatch.EXPIRY, 'unapproved', root=self.root / 'other', clock=self.runtime.clock)
        self.runtime.now = dispatch.stamp(dispatch.EXPIRY)
        with self.assertRaises(ValueError):
            self.app.tick()
        self.assertEqual(self.runtime.calls, [])

    def test_global_nonblocking_lock_and_private_state(self):
        with dispatch.locked(self.app.root):
            with self.assertRaises(BlockingIOError):
                with dispatch.locked(self.app.root):
                    self.fail('Second dispatcher acquired lock')
        link = self.root / 'link'
        link.symlink_to(self.app.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            dispatch.Dispatcher(dispatch.EXPIRY, dispatch.RENEWAL, root=link, clock=self.runtime.clock)

    def test_command_boundary_and_remaining_expiry_are_passed_to_every_child(self):
        self.app.tick()
        for command, raw, options in self.runtime.calls:
            self.assertEqual(options['expires'], dispatch.stamp(dispatch.EXPIRY))
            self.assertLessEqual(options['timeout'], 180)
            if command[0] == 'ssh':
                self.assertIn('omarchy', command)
                self.assertIn(dispatch.RENEWAL, shlex.split(command[-1]))
            else:
                self.assertIn('--execute', command)
                self.assertEqual(Path(command[2]).name, 'mac_kodi_consumer.py')
        with self.assertRaises(ValueError):
            dispatch.decode(b'{"same":1,"same":2}')
        with self.assertRaises(ValueError):
            dispatch.decode(b'{"value":NaN}')


class ChildBoundaryTests(unittest.TestCase):
    def test_small_child_and_input_are_bounded(self):
        code, raw = dispatch.bounded_run([sys.executable, '-c', 'import sys; print(sys.stdin.read())'],
            b'synthetic', timeout=2, expires=dispatch.utcnow() + timedelta(seconds=3))
        self.assertEqual((code, raw), (0, b'synthetic\n'))

    def test_oversized_output_child_is_stopped(self):
        with self.assertRaises(ValueError):
            dispatch.bounded_run([sys.executable, '-c', 'print("x" * 1100000)'],
                timeout=2, expires=dispatch.utcnow() + timedelta(seconds=3))

    def test_expiry_stops_inflight_child_and_no_expired_child_starts(self):
        with self.assertRaises(TimeoutError):
            dispatch.bounded_run([sys.executable, '-c', 'import time; time.sleep(10)'],
                timeout=2, expires=dispatch.utcnow() + timedelta(seconds=.1))
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('must not start')):
            with self.assertRaises(ValueError):
                dispatch.bounded_run(['not-a-command'], timeout=2,
                                    expires=dispatch.utcnow() - timedelta(seconds=1))


if __name__ == '__main__':
    unittest.main()
