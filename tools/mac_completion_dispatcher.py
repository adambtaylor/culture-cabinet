#!/usr/bin/env python3
"""Bounded owner movie, pilot and album completion dispatcher; never enqueue, play or infer identity.

The producer lists registered jobs. Fresh exports and the fixed media consumers
verify completion. A private write-ahead journal bridges consumer and server
receipts, so uncertain feedback is replayed byte-for-byte before that job resumes.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID

import mac_kodi_consumer as consumer
from episode_scope import is_episode_scope

WORKSPACE = Path('/Users/adamtaylor/Documents/Codex/2026-10-29/task-4')
STATE = WORKSPACE / 'dispatcher-evidence/runtime'
CACHE = Path('/Users/adamtaylor/Movies/Omarchy/Cabinet Movies')
TV_CACHE = Path('/Users/adamtaylor/Movies/Omarchy/TV Shows')
ALBUM_CACHE = Path('/Users/adamtaylor/Music/Omarchy/Cabinet Albums')
MEDIA = {('tmdb', 'movie', 'film'), ('tmdb', 'tv', 'series_s01e01'),
         ('album_request', 'music', 'album_flac')}
REMOTE = '/home/tang/Projects/culture-cabinet'
EXPIRY = '2026-11-06T04:17:29Z'
RENEWAL = '20261030-approved-seven-days'
LIMIT = 16
MAX_BYTES = 1024 * 1024
STATES = {'not_reported': 0, 'blocked': 1, 'unknown': 2, 'imported': 3}
BINDING = ('request_id', 'nzo_id', 'result_id', 'result_digest', 'nzb_sha256', 'source_identity')
require = consumer.require


def supported(identity):
    kind = tuple(identity.get(key) for key in ('provider', 'medium', 'scope'))
    return kind in MEDIA or (kind[:2] == ('tmdb', 'tv') and is_episode_scope(kind[2]))


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    require(type(value) is str, 'Text timestamp required.')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.utcoffset() is not None, 'Aware timestamp required.')
    return result


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key.')
        result[key] = value
    return result


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, 'Bounded JSON required.')
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON.')))


def canonical_uuid(value):
    require(type(value) is str and str(UUID(value)) == value, 'Canonical UUID required.')


def session(expires, renewal, now):
    expiry = stamp(expires)
    require(now < expiry <= stamp(EXPIRY) and renewal == RENEWAL, 'Current approved session required.')
    return expiry


@contextmanager
def locked(root):
    root = consumer.safe_directory(root, create=True, private=True)
    fd = os.open(root / 'dispatcher.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe lock.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def bounded_run(command, payload=b'', *, timeout, expires, clock=utcnow, cleanup_grace=0):
    """Bound stdout and elapsed time, including expiry; terminate only our child group."""
    require(type(payload) is bytes and len(payload) <= 256 * 1024, 'Oversized child input.')
    require(type(cleanup_grace) in (int, float) and 0 <= cleanup_grace <= 5, 'Invalid cleanup grace.')
    remaining = min(timeout, (expires - clock()).total_seconds())
    require(remaining > 0, 'Session expired before child launch.')
    deadline = time.monotonic() + remaining
    with tempfile.TemporaryFile() as incoming:
        incoming.write(payload)
        incoming.seek(0)
        child = subprocess.Popen(command, stdin=incoming, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    left = min(deadline - time.monotonic(), (expires - clock()).total_seconds())
                    if left <= 0:
                        raise TimeoutError('Child deadline reached.')
                    for key, _ in selector.select(min(left, .25)):
                        block = os.read(key.fileobj.fileno(), min(65536, MAX_BYTES + 1 - len(output)))
                        if not block:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(block)
                            require(len(output) <= MAX_BYTES, 'Child output exceeded bound.')
            left = min(deadline - time.monotonic(), (expires - clock()).total_seconds())
            if left <= 0:
                raise TimeoutError('Child deadline reached.')
            return child.wait(timeout=left), bytes(output)
        finally:
            # Apple TV's consumer needs a bounded chance to unwind its own
            # separately grouped transfer/native children. Existing callers
            # retain immediate termination unless they explicitly opt in.
            reaped = False
            try:
                if cleanup_grace:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=cleanup_grace)
                        reaped = True
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if not reaped:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
                child.stdout.close()


class Dispatcher:
    def __init__(self, expires, renewal, *, root=STATE, cache=CACHE, tv_cache=TV_CACHE, album_cache=ALBUM_CACHE,
                 runner=bounded_run, clock=utcnow):
        self.expires = session(expires, renewal, clock())
        self.expiry_text, self.renewal = expires, renewal
        self.root = consumer.safe_directory(root, create=True, private=True)
        self.jobs = consumer.safe_directory(self.root / 'jobs', create=True, private=True)
        self.consumer_state = consumer.safe_directory(self.root / 'consumer', create=True, private=True)
        self.cache, self.runner, self.clock = Path(cache), runner, clock
        self.tv_cache, self.album_cache = Path(tv_cache), Path(album_cache)
        self.health = None

    def adapter(self, identity):
        kind = (identity['provider'], identity['medium'], identity['scope'])
        require(supported(identity), 'Unsupported exact completion scope.')
        if kind == ('tmdb', 'movie', 'film'):
            return consumer, self.cache, self.consumer_state
        if kind[:2] == ('tmdb', 'tv') and is_episode_scope(kind[2]):
            import mac_episode_consumer as module
            return module, self.tv_cache, self.consumer_state / 'tv'
        import mac_album_consumer as module
        return module, self.album_cache, self.consumer_state / 'album'

    def guard(self):
        session(self.expiry_text, self.renewal, self.clock())

    def read(self, path):
        value = consumer.read_json(path, private=True)
        # The consumer's safe regular-file reader also rejects duplicate keys.
        # Re-encoding adds the strict finite-value check used by our journals.
        consumer.encoded(value)
        return value

    def save(self, path, value):
        require(len(consumer.encoded(value, newline=True)) <= MAX_BYTES, 'Journal exceeds bound.')
        consumer.save_receipt(path, value)

    def remote(self, script, arguments=(), payload=b''):
        self.guard()
        require(script in ('list_completion_jobs.py', 'export_completed_job.py', 'record_kodi_completion.py'),
                'Unexpected operator command.')
        args = [REMOTE + '/.venv/bin/python', '-B', REMOTE + '/tools/' + script,
                '--expires', self.expiry_text, '--renewal', self.renewal, *map(str, arguments)]
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'omarchy', shlex.join(args)]
        code, raw = self.runner(command, payload, timeout=35, expires=self.expires, clock=self.clock)
        require(code == 0, 'Operator command unavailable.')
        return decode(raw)

    def page(self, value, after):
        require(type(value) is dict and type(value.get('version')) is int
                and ((value['version'], value.get('scope')) in ((1, 'movie_film_only'), (2, 'supported_media')))
                and value.get('state') == 'listed'
                and type(value.get('actor_id')) is int and value['actor_id'] == 1
                and type(value.get('profile_id')) is int and value['profile_id'] == 1
                and value.get('after') == after
                and value.get('limit') == LIMIT, 'Foreign or invalid listing envelope.')
        jobs, cursor, more = value['jobs'], value['next_cursor'], value['has_more']
        require(type(jobs) is list and len(jobs) <= LIMIT and type(cursor) is int and cursor >= after
                and type(more) is bool and (not more or cursor > after), 'Invalid page bounds.')
        ids = [job.get('job_id') for job in jobs if type(job) is dict]
        require(len(ids) == len(jobs) and all(type(i) is int and after < i <= cursor for i in ids)
                and ids == sorted(set(ids)), 'Duplicate or out-of-page jobs.')
        if value['version'] == 1:
            require(all(not job.get('dispatchable') or (job.get('source_identity', {}).get('medium') == 'movie'
                    and job['source_identity'].get('scope') == 'film') for job in jobs),
                    'Legacy listing cannot authorize additional media.')
        return jobs, cursor if more else 0

    def binding(self, job):
        require(type(job['job_id']) is int and job['job_id'] > 0 and job['dispatchable'] is True
                and job['test_only'] is False and job['delivery_state'] in STATES, 'Not a live dispatchable media job.')
        canonical_uuid(job['request_id']); canonical_uuid(job['result_id'])
        require(type(job['nzo_id']) is str and re.fullmatch(
            r'(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|SABnzbd_nzo_[A-Za-z0-9_-]{1,100})', job['nzo_id']), 'Invalid job identity.')
        require(all(consumer.is_hash(job[key]) for key in ('result_digest', 'nzb_sha256')),
                'Invalid job provenance.')
        require(job['completion_evidence_sha256'] is None or consumer.is_hash(job['completion_evidence_sha256']),
                'Invalid completion evidence.')
        identity = job['source_identity']
        require(type(identity) is dict and set(identity) == {'provider', 'medium', 'source_id', 'scope', 'title', 'edition', 'basis'}
                and supported(identity)
                and type(identity['source_id']) is int and 0 < identity['source_id'] <= 2147483647
                and identity['basis'] == 'selected_catalogue_identity_not_content_verification', 'Supported media binding required.')
        for field, size in (('title', 300), ('edition', 500)):
            require(type(identity[field]) is str and 0 < len(identity[field].strip()) <= size
                    and not any(ord(c) < 32 for c in identity[field]), 'Invalid selected identity text.')
        binding = {key: job[key] for key in BINDING}
        if identity['scope'] == 'album_flac':
            require(type(job.get('album_identity')) is dict, 'Exact selected album identity required.')
            binding['album_identity'] = job['album_identity']
        return binding

    def receipt(self, record):
        module, cache, state = self.adapter(record['binding']['source_identity'])
        path = state / (record['binding']['request_id'] + '.json')
        if not path.exists() and not path.is_symlink():
            return None
        return self.validate_receipt(record, self.read(path))

    def validate_receipt(self, record, receipt):
        module, cache, state = self.adapter(record['binding']['source_identity'])
        if module is not consumer:
            return module.validate_receipt(receipt, record['handoff'], record['selection'],
                                           cache=cache, now=self.clock())
        # Preserve the reviewed movie receipt contract and signatures verbatim;
        # old pending movie uploads require no migration or timestamp refresh.
        validated = consumer.validate(record['handoff'], record['selection'], self.clock())
        require(receipt.get('version') == 1 and receipt.get('request_id') == validated['request_id']
                and receipt.get('selection') == record['selection'] and receipt.get('file') == validated['file']
                and receipt.get('hook_sha256') == validated['hook_sha256']
                and receipt.get('completion_evidence_sha256') == validated['completion_evidence_sha256']
                and receipt.get('signature') == consumer.digest([validated['signature'], str(cache)])
                and receipt.get('scan_key') == 'video-' + validated['request_id']
                and type(receipt.get('scan_attempts')) is int and receipt['scan_attempts'] in (0, 1)
                and receipt.get('playback_started') is False, 'Foreign consumer receipt.')
        require(receipt.get('status') in ('queued', 'scanning', 'blocked', 'unknown', 'imported'), 'Invalid consumer state.')
        if receipt['status'] == 'unknown':
            require(receipt['scan_attempts'] == 1, 'Unknown must retain its single-scan reservation.')
        return receipt

    def queue_feedback(self, path, record, receipt):
        state = receipt['status']
        if state not in STATES or STATES[state] <= STATES[record['ack_state']]:
            record['phase'] = 'idle'
        else:
            require(len(consumer.encoded(receipt, newline=True)) <= 256 * 1024, 'Receipt exceeds feedback bound.')
            record['pending'] = consumer.encoded(receipt, newline=True).decode()
            record['phase'] = 'feedback'
        record['next_attempt'] = self.clock().timestamp() + 60
        self.save(path, record)

    def feedback(self, path, record):
        raw = record['pending'].encode()
        receipt = decode(raw)
        # Validate the frozen pending object independently of subsequently
        # refreshed receipt timestamps; these exact bytes are the retry unit.
        require(receipt['selection'] == record['selection'] and receipt['request_id'] == record['binding']['request_id']
                and receipt['status'] in STATES, 'Pending feedback binding changed.')
        self.validate_receipt(record, receipt)
        result = self.remote('record_kodi_completion.py',
            ('--job', record['job_id'], '--request', record['binding']['request_id']), raw)
        require(result.get('recorded') is True and result.get('job') == record['job_id']
                and result.get('state') == receipt['status'] and result.get('playback_started') is False,
                'Feedback acknowledgement missing or foreign.')
        record.update(ack_state=receipt['status'], ack_digest=consumer.digest(receipt), pending=None, phase='idle')
        self.save(path, record)

    def recover_feedback(self):
        """Replay a job's lost feedback without starving unrelated registered jobs."""
        paths = sorted(self.jobs.glob('*.json'))
        require(len(paths) <= consumer.MAX_RECEIPTS, 'Dispatcher job capacity exceeded.')
        for path in paths:
            try:
                record = self.read(path)
                require(type(record['job_id']) is int and path.stem == str(record['job_id'])
                        and record['version'] == 1 and record['ack_state'] in STATES
                        and record['phase'] in ('idle', 'reserved', 'feedback'), 'Invalid journal identity.')
                canonical_uuid(record['binding']['request_id'])
                if record['phase'] == 'reserved' and record.get('handoff'):
                    receipt = self.receipt(record)
                    if receipt and receipt['status'] in STATES:
                        self.queue_feedback(path, record, receipt)
                if record.get('pending') and self.clock().timestamp() >= record['next_attempt']:
                    record['next_attempt'] = self.clock().timestamp() + 60
                    self.save(path, record)
                    self.feedback(path, record)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                self.health['counts']['job_errors'] += 1

    def process(self, job):
        binding = self.binding(job)
        path = self.jobs / (str(job['job_id']) + '.json')
        if path.exists() or path.is_symlink():
            record = self.read(path)
            require(record['binding'] == binding and record['job_id'] == job['job_id'], 'Registered binding changed.')
        else:
            existing = list(self.jobs.glob('*.json'))
            require(len(existing) < consumer.MAX_RECEIPTS, 'Dispatcher capacity reached.')
            require(all(self.read(other)['binding']['request_id'] != binding['request_id'] for other in existing),
                    'Request already belongs to another dispatcher job.')
            # Importing into a fresh dispatcher after an older unknown result
            # requires its original single-scan receipt, never a new scan.
            record = {'version': 1, 'job_id': job['job_id'], 'binding': binding, 'phase': 'idle',
                      'ack_state': job['delivery_state'], 'ack_digest': job.get('delivery_sha256'),
                      'pending': None, 'next_attempt': 0, 'selection': None, 'handoff': None}
        if record['ack_state'] == 'imported' or self.clock().timestamp() < record['next_attempt']:
            return
        if record['pending']:
            return  # Only this job waits for its exact pending acknowledgement.
        record['next_attempt'] = self.clock().timestamp() + 60
        self.save(path, record)
        result = self.remote('export_completed_job.py', ('--job', job['job_id'], '--request', binding['request_id']))
        require(all(result.get(key) == binding[key] for key in BINDING if key != 'source_identity'), 'Foreign completion export.')
        if result['state'] != 'completed':
            return
        handoff = result['handoff']
        evidence = handoff['completion_evidence_sha256']
        require(job['completion_evidence_sha256'] in (None, evidence), 'Listed completion evidence changed.')
        selection = {'source_identity': binding['source_identity'],
                     'provenance': {key: binding[key] for key in ('request_id', 'result_id', 'result_digest', 'nzb_sha256')},
                     'nzo_id': binding['nzo_id'], 'completion_evidence_sha256': evidence}
        if binding['source_identity']['scope'] == 'album_flac':
            selection['album_identity'] = binding['album_identity']
        module, cache, state = self.adapter(binding['source_identity'])
        checked = module.validate(handoff, selection, self.clock())
        require(all(item['test_only'] is False for item in (checked['files'] if 'files' in checked else [checked['file']])),
                'Synthetic live import refused.')
        if record['selection'] is not None:
            require(record['selection'] == selection, 'Completion binding changed after reservation.')
        record.update(selection=selection, handoff=handoff)
        if record['ack_state'] == 'unknown':
            previous = self.receipt(record)
            # A partial album readback freezes mutation even when this worker
            # never scanned; its validated marker also permits read-only retry.
            require(previous is not None and (previous['scan_attempts'] == 1
                    or (binding['source_identity']['scope'] == 'album_flac'
                        and previous.get('readback_seen') is True)),
                    'Read-only reconciliation receipt required.')
        folder = consumer.safe_directory(self.root / binding['request_id'], create=True, private=True)
        self.save(folder / 'handoff.json', handoff)
        self.save(folder / 'selection.json', selection)
        record['phase'] = 'reserved'
        self.save(path, record)  # Durable before any transfer, publication or Kodi call.
        self.guard()
        self.health['counts']['consumer_attempts'] += 1
        self.runner([sys.executable, '-B', str(Path(module.__file__).resolve()),
            '--handoff', str(folder / 'handoff.json'), '--selection', str(folder / 'selection.json'),
            '--cache', str(cache), '--state-dir', str(state), '--execute'],
            timeout=180, expires=self.expires, clock=self.clock)
        self.guard()
        receipt = self.receipt(record)
        require(receipt is not None, 'No actual consumer receipt produced.')
        self.queue_feedback(path, record, receipt)
        if record.get('pending'):
            self.feedback(path, record)

    def tick(self):
        self.guard()
        self.health = {'version': 1, 'checked_at': self.clock().isoformat(),
            'expires_at': self.expiry_text, 'pid': os.getpid(), 'listing': 'not_attempted',
            'counts': {'listed': 0, 'eligible': 0, 'consumer_attempts': 0, 'feedback_pending': 0, 'job_errors': 0}}
        self.save(self.root / 'status.json', self.health)
        try:
            self.recover_feedback()
            cursor_path = self.root / 'cursor.json'
            cursor = self.read(cursor_path)['after'] if cursor_path.exists() or cursor_path.is_symlink() else 0
            require(type(cursor) is int and cursor >= 0, 'Invalid durable cursor.')
            self.health['listing'] = 'unavailable'
            page = self.remote('list_completion_jobs.py', ('--after', cursor, '--limit', LIMIT))
            jobs, after = self.page(page, cursor)
            self.health['listing'] = 'listed'
            self.health['counts']['listed'] = len(jobs)
            self.save(cursor_path, {'after': after})
            for job in jobs:
                self.guard()
                if job.get('dispatchable') is not True or job.get('delivery_state') == 'imported':
                    continue
                self.health['counts']['eligible'] += 1
                try:
                    self.process(job)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                    # Keep failures scoped to their job; never serialize raw
                    # adapter exceptions or invent an importer receipt.
                    self.health['counts']['job_errors'] += 1
                    path = self.jobs / (str(job['job_id']) + '.json')
                    if path.exists():
                        try:
                            record = self.read(path)
                            record['next_attempt'] = self.clock().timestamp() + 60
                            self.save(path, record)
                        except (OSError, ValueError, KeyError, TypeError):
                            pass  # Preserve malformed state for inspection; do not reset it.
        finally:
            for path in self.jobs.glob('*.json'):
                try:
                    self.health['counts']['feedback_pending'] += bool(self.read(path).get('pending'))
                except (OSError, ValueError, KeyError, TypeError):
                    self.health['counts']['job_errors'] += 1
            self.health['checked_at'] = self.clock().isoformat()
            self.save(self.root / 'status.json', self.health)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        session(args.expires, args.renewal, utcnow())
        with locked(STATE):
            dispatcher = Dispatcher(args.expires, args.renewal)
            while utcnow() < dispatcher.expires:
                started = time.monotonic()
                failed = False
                try:
                    dispatcher.tick()
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                    failed = True
                    print('Completion observation unavailable; durable state retained.', flush=True)
                if args.once:
                    return 1 if failed else 0
                remaining = (dispatcher.expires - utcnow()).total_seconds()
                if remaining > 0:
                    time.sleep(min(remaining, max(60 - (time.monotonic() - started), 0)))
        return 0
    except (KeyboardInterrupt, OSError, ValueError):
        print('Dispatcher stopped; durable state retained.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
