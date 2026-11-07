"""Record an actual supported media consumer receipt via existing owner SSH, never HTTP.

Feed the existing private Mac receipt on stdin. This command makes no provider,
Kodi or file-transfer calls and does not authenticate arbitrary JSON on its own.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
from uuid import UUID

ROOT = Path('/home/tang/Projects/culture-cabinet')
SERVER = ROOT / '.p1-readiness/P2.1-funnel/deployment/server.json'
MAX_BYTES = 256 * 1024


def validate_session(args, server, now=None):
    now = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(args.expires)
    limit = datetime.fromisoformat(server['expires_utc'])
    if (str(UUID(args.request)) != args.request or type(args.job) is not int or args.job <= 0
            or expiry.utcoffset() is None or limit.utcoffset() is None
            or not now < expiry <= limit or args.renewal != server['renewal_id']):
        raise ValueError('Current exact bounded session required')
    return expiry


def read_receipt(stream):
    raw = stream.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Bounded receipt required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON keys')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def require_live_receipt(receipt):
    if type(receipt) is not dict or type(receipt.get('selection')) is not dict:
        raise ValueError('Actual supported media consumer receipt required')
    identity = receipt['selection'].get('source_identity')
    if type(identity) is not dict:
        raise ValueError('Actual supported media consumer receipt required')
    kind = (identity.get('provider'), identity.get('medium'), identity.get('scope'))
    if kind in {('tmdb', 'movie', 'film'), ('tmdb', 'tv', 'series_s01e01')}:
        files = [receipt.get('file')]
    elif kind == ('album_request', 'music', 'album_flac'):
        files = receipt.get('files')
    else:
        raise ValueError('Unsupported media consumer receipt')
    if (type(files) is not list or not 1 <= len(files) <= 128
            or any(type(item) is not dict or item.get('test_only') is not False for item in files)):
        raise ValueError('Actual media files required')


def run(args, stream):
    if Path(__file__).resolve() != ROOT / 'tools/record_kodi_completion.py':
        raise ValueError('Deployed operator path required')
    def guard():
        return validate_session(args, json.loads(SERVER.read_text()))
    expiry = guard()
    def expired(signum, frame):
        raise TimeoutError('Bounded import observation ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(30, (expiry-datetime.now(timezone.utc)).total_seconds()))
    receipt = read_receipt(stream)
    # Reject synthetic and unsupported input before any Django/private store setup.
    require_live_receipt(receipt)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import completion_status, obtain_acquisition
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    context = obtain_acquisition.OperatorContext(1, 1, 'candidate_operator', True)
    job = obtain_acquisition.owned_job(actor, 1, args.job)
    if str(job.request_id) != args.request:
        raise ValueError('Request changed')
    row = completion_status.record_import(actor=actor, profile_id=1, job_id=job.pk,
        context=context, receipt=receipt, session_guard=guard)
    return {'state': row.delivery_state, 'job': job.pk, 'revision': row.revision,
            'recorded': True, 'playback_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True, type=int)
    parser.add_argument('--request', required=True)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args(), sys.stdin.buffer)
    except Exception as error:
        print(json.dumps({'state': 'unknown', 'recorded': False,
            'reason': 'operator_receipt_unavailable', 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
