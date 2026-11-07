#!/usr/bin/env python3
"""Independent bounded Apple TV worker with frozen write-ahead feedback.

A lost consumer result never authorizes a fresh child: its private receipt must
exist before recovery can continue. Unknown receipts only reconcile forward.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import mac_completion_dispatcher as base
import mac_kodi_consumer as files

STATE = base.WORKSPACE / 'appletv-evidence/runtime'
LIMIT = 16
STATES = {'not_reported', 'blocked', 'remux_ready', 'handoff_pending', 'unknown', 'library_import_observed'}
require = files.require


def bounded_run(command, payload=b'', *, timeout, expires, clock=base.utcnow):
    return base.bounded_run(command, payload, timeout=timeout, expires=expires,
                            clock=clock, cleanup_grace=5)


class Dispatcher:
    def __init__(self, expires, renewal, *, root=STATE, runner=bounded_run, clock=base.utcnow):
        self.expires = base.session(expires, renewal, clock())
        self.expiry_text, self.renewal = expires, renewal
        self.root = files.safe_directory(root, create=True, private=True)
        self.jobs = files.safe_directory(self.root / 'jobs', create=True, private=True)
        self.consumer_state = files.safe_directory(self.root / 'consumer', create=True, private=True)
        self.runner, self.clock = runner, clock

    def guard(self):
        base.session(self.expiry_text, self.renewal, self.clock())

    def read(self, path):
        value = files.read_json(path, private=True)
        require(len(files.encoded(value)) <= base.MAX_BYTES, 'Oversized journal.')
        return value

    def save(self, path, value):
        require(len(files.encoded(value, newline=True)) <= base.MAX_BYTES, 'Oversized journal.')
        files.save_receipt(path, value)

    def remote(self, script, args=(), payload=b''):
        self.guard()
        require(script in ('list_apple_tv_jobs.py', 'export_apple_tv_job.py', 'record_apple_tv_completion.py'),
                'Unexpected Apple TV operator.')
        command = [base.REMOTE + '/.venv/bin/python', '-B', base.REMOTE + '/tools/' + script,
                   '--expires', self.expiry_text, '--renewal', self.renewal, *map(str, args)]
        code, raw = self.runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            'omarchy', shlex.join(command)], payload, timeout=35, expires=self.expires, clock=self.clock)
        require(code == 0, 'Apple TV operator unavailable.')
        return base.decode(raw)

    def receipt(self, record):
        path = self.consumer_state / (record['request_id'] + '.json')
        if not path.exists() and not path.is_symlink():
            return None
        receipt = self.read(path)
        from mac_apple_tv_consumer import validate_receipt
        validate_receipt(receipt, record['contract'])
        return receipt

    def queue_feedback(self, path, record, receipt):
        require(receipt['status'] in STATES - {'not_reported'}, 'Invalid consumer receipt state.')
        if record['ack_digest'] == files.digest(receipt):
            record['phase'] = 'idle'
        elif record['ack_state'] == 'library_import_observed':
            require(False, 'Terminal import cannot change.')
        elif (record['ack_state'] == 'unknown' and receipt['status'] != 'library_import_observed'
              or record['ack_state'] == receipt['status'] and receipt['status'] != 'blocked'):
            record['phase'] = 'idle'
        else:
            raw = files.encoded(receipt, newline=True)
            require(len(raw) <= 256 * 1024, 'Oversized feedback.')
            record.update(phase='feedback', pending=raw.decode())
        record['next_attempt'] = self.clock().timestamp() + 60
        self.save(path, record)

    def feedback(self, path, record):
        raw = record['pending'].encode()
        receipt = base.decode(raw)
        from mac_apple_tv_consumer import validate_receipt
        validate_receipt(receipt, record['contract'])
        result = self.remote('record_apple_tv_completion.py',
            ('--job', record['job_id'], '--request', record['request_id']), raw)
        require(result.get('recorded') is True and result.get('job') == record['job_id']
            and result.get('state') == receipt['status'] and result.get('playback_started') is False,
            'Missing exact feedback acknowledgement.')
        record.update(ack_state=receipt['status'], ack_digest=files.digest(receipt), pending=None, phase='idle')
        self.save(path, record)

    def recover(self):
        paths = sorted(self.jobs.glob('*.json'))
        require(len(paths) <= files.MAX_RECEIPTS, 'Apple TV journal capacity exceeded.')
        errors = 0
        for path in paths:
            self.guard()
            try:
                record = self.read(path)
                require(record['version'] == 1 and type(record['job_id']) is int
                    and path.stem == str(record['job_id']) and record['ack_state'] in STATES
                    and record['phase'] in ('idle', 'reserved', 'feedback'), 'Invalid Apple TV journal.')
                base.canonical_uuid(record['request_id'])
                if record['phase'] == 'reserved':
                    receipt = self.receipt(record)
                    if receipt is not None:
                        self.queue_feedback(path, record, receipt)
                if record.get('pending') and self.clock().timestamp() >= record['next_attempt']:
                    record['next_attempt'] = self.clock().timestamp() + 60
                    self.save(path, record)
                    self.feedback(path, record)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                errors += 1
        return errors

    def process(self, job):
        require(type(job['job_id']) is int and job['job_id'] > 0 and job['test_only'] is False
            and job['dispatchable'] is True and job['apple_tv_state'] in STATES
            and base.supported(job['source_identity'])
            and (job['source_identity']['provider'], job['source_identity']['medium']) == ('tmdb', 'tv'),
            'Invalid live Apple TV episode.')
        base.canonical_uuid(job['request_id'])
        path = self.jobs / (str(job['job_id']) + '.json')
        if path.exists() or path.is_symlink():
            record = self.read(path)
            require(record['request_id'] == job['request_id'] and record['job_id'] == job['job_id']
                and record['contract']['contract_sha256'] == job['contract_sha256'], 'Apple TV binding changed.')
            if record['phase'] == 'reserved':
                # No receipt means child outcome is uncertain. Keep reservation;
                # never create new consumer state or repeat the import attempt.
                return
        else:
            require(len(list(self.jobs.glob('*.json'))) < files.MAX_RECEIPTS, 'Apple TV capacity reached.')
            record = {'version': 1, 'job_id': job['job_id'], 'request_id': job['request_id'],
                'phase': 'idle', 'ack_state': job['apple_tv_state'], 'ack_digest': None,
                'pending': None, 'next_attempt': 0, 'contract': None}
        if record['ack_state'] == 'library_import_observed' or record['pending'] or self.clock().timestamp() < record['next_attempt']:
            return
        contract = self.remote('export_apple_tv_job.py', ('--job', job['job_id'], '--request', job['request_id']))
        from apple_tv_media import validate_contract
        validate_contract(contract)
        require(contract['contract_sha256'] == job['contract_sha256']
            and contract['selection']['provenance']['request_id'] == job['request_id']
            and contract['selection']['source_identity'] == job['source_identity'], 'Foreign Apple TV contract.')
        if record['contract'] is not None:
            require(record['contract'] == contract, 'Reserved Apple TV contract changed.')
        record['contract'] = contract
        if record['ack_state'] in ('unknown', 'handoff_pending'):
            require(self.receipt(record) is not None, 'Original uncertain import receipt required.')
        folder = files.safe_directory(self.root / job['request_id'], create=True, private=True)
        self.save(folder / 'contract.json', contract)
        record.update(phase='reserved', next_attempt=self.clock().timestamp() + 60)
        self.save(path, record)
        self.guard()
        self.runner([sys.executable, '-B', str(Path(__file__).with_name('mac_apple_tv_consumer.py')),
            '--contract', str(folder / 'contract.json'), '--state-dir', str(self.consumer_state),
            '--execute', '--expires', self.expiry_text], timeout=1800, expires=self.expires, clock=self.clock)
        self.guard()
        receipt = self.receipt(record)
        require(receipt is not None, 'No durable Apple TV receipt.')
        self.queue_feedback(path, record, receipt)
        if record.get('pending'):
            self.feedback(path, record)

    def tick(self):
        self.guard()
        health = {'version': 1, 'destination': 'apple_tv', 'checked_at': self.clock().isoformat(),
            'expires_at': self.expiry_text, 'pid': os.getpid(), 'listing': 'unavailable', 'job_errors': self.recover()}
        try:
            cursor_path = self.root / 'cursor.json'
            after = self.read(cursor_path)['after'] if cursor_path.exists() else 0
            require(type(after) is int and after >= 0, 'Invalid cursor.')
            page = self.remote('list_apple_tv_jobs.py', ('--after', after, '--limit', LIMIT))
            require(type(page['version']) is int
                and (page['version'], page['scope']) in ((1, 'apple_tv_pilots'), (2, 'apple_tv_episodes'))
                and page['state'] == 'listed'
                and page['actor_id'] == page['profile_id'] == 1 and page['after'] == after
                and page['limit'] == LIMIT and type(page['has_more']) is bool
                and type(page['next_cursor']) is int and page['next_cursor'] >= after
                and (not page['has_more'] or page['next_cursor'] > after)
                and type(page['jobs']) is list and len(page['jobs']) <= LIMIT, 'Invalid Apple TV page.')
            ids = [job['job_id'] for job in page['jobs']]
            require(ids == sorted(set(ids)) and all(type(i) is int and after < i <= page['next_cursor'] for i in ids),
                    'Out-of-page Apple TV jobs.')
            if page['version'] == 1:
                require(all((job['source_identity']['provider'], job['source_identity']['medium'],
                             job['source_identity']['scope']) == ('tmdb', 'tv', 'series_s01e01')
                            for job in page['jobs'] if 'source_identity' in job or job.get('dispatchable') is True),
                        'Legacy pilot listing cannot authorize explicit episodes.')
            health['listing'] = 'listed'
            self.save(cursor_path, {'after': page['next_cursor'] if page['has_more'] else 0})
            for job in page['jobs']:
                self.guard()
                if job.get('dispatchable') is not True:
                    continue
                try:
                    self.process(job)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                    health['job_errors'] += 1
        finally:
            self.save(self.root / 'status.json', health)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        base.session(args.expires, args.renewal, base.utcnow())
        with base.locked(STATE):
            worker = Dispatcher(args.expires, args.renewal)
            while base.utcnow() < worker.expires:
                started = time.monotonic()
                failed = False
                try:
                    worker.tick()
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                    failed = True
                    print('Apple TV observation unavailable; durable state retained.', flush=True)
                if args.once:
                    return int(failed)
                remaining = (worker.expires-base.utcnow()).total_seconds()
                if remaining > 0:
                    time.sleep(min(remaining, max(60-(time.monotonic()-started), 0)))
        return 0
    except (KeyboardInterrupt, OSError, ValueError):
        print('Apple TV dispatcher stopped; durable state retained.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
