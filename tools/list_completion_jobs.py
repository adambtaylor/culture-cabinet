"""Read-only bounded discovery through the existing owner SSH session."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

ROOT = Path('/home/tang/Projects/culture-cabinet')
SERVER = ROOT / '.p1-readiness/P2.1-funnel/deployment/server.json'


def validate_session(args, server, now=None):
    now = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(args.expires)
    limit = datetime.fromisoformat(server['expires_utc'])
    if (type(args.after) is not int or not 0 <= args.after < 2**63
            or type(args.limit) is not int or not 1 <= args.limit <= 32
            or expiry.utcoffset() is None or limit.utcoffset() is None
            or not now < expiry <= limit or args.renewal != server['renewal_id']):
        raise ValueError('Current bounded discovery session required')
    return expiry


def run(args):
    if Path(__file__).resolve() != ROOT / 'tools/list_completion_jobs.py':
        raise ValueError('Deployed operator path required')
    def guard():
        return validate_session(args, json.loads(SERVER.read_text()))
    expiry = guard()
    def expired(signum, frame):
        raise TimeoutError('Bounded discovery ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(30, (expiry-datetime.now(timezone.utc)).total_seconds()))
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import completion_dispatch, obtain_acquisition
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    context = obtain_acquisition.OperatorContext(1, 1, 'candidate_operator', True)
    return completion_dispatch.list_jobs(actor=actor, profile_id=1, context=context,
        after=args.after, limit=args.limit, session_guard=guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--limit', type=int, default=16)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args())
    except Exception as error:
        # No private path, SQL, provider response or raw exception escapes.
        print(json.dumps({'version': 1, 'state': 'unavailable', 'actor_id': 1, 'profile_id': 1,
            'reason': 'discovery_unavailable', 'jobs': [], 'next_cursor': None, 'has_more': False,
            'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
