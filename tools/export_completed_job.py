"""One bounded operator export over existing SSH; no provider or playback call."""
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


def run(args):
    if Path(__file__).resolve() != ROOT / 'tools/export_completed_job.py':
        raise ValueError('Deployed operator path required')
    expected = str(UUID(args.request))
    if expected != args.request or args.job <= 0:
        raise ValueError('Exact job required')
    expiry = datetime.fromisoformat(args.expires)
    now = datetime.now(timezone.utc)
    server = json.loads(SERVER.read_text())
    if (expiry.utcoffset() is None or not now < expiry <= datetime.fromisoformat(server['expires_utc'])
            or args.renewal != server['renewal_id']):
        raise ValueError('Current bounded session required')
    def expired(signum, frame):
        raise TimeoutError('Bounded export ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(30, (expiry-now).total_seconds()))
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import obtain_acquisition, completion_observer
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    context = obtain_acquisition.OperatorContext(1, 1, 'candidate_operator', True)
    job = obtain_acquisition.owned_job(actor, 1, args.job)
    if str(job.request_id) != expected:
        raise ValueError('Request changed')
    return completion_observer.observe(actor=actor, profile_id=1,
                                       job_id=job.pk, context=context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=int, required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args())
    except Exception as error:
        # Never return arbitrary exceptions, provider strings or private paths.
        print(json.dumps({'state': 'unknown', 'reason': 'operator_export_unavailable',
                          'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
