"""Record a supervised Google tool read-back via bounded existing owner SSH.

This command does not call Google. Input is normalized by the trusted operator
from actual connected-tool reads, never from an uploaded claim in the web app.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
from uuid import UUID

ROOT = Path('/home/tang/Projects/culture-cabinet')
SERVER = ROOT / '.p1-readiness/P2.1-funnel/deployment/server.json'
LIMIT = 131072


def read_evidence(stream):
    raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Bounded provider read-back required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON keys')
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError('Non-finite JSON')
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError('Observed provider record required')
    return value


def validate_session(args, server, now=None):
    expiry = datetime.fromisoformat(args.expires)
    limit = datetime.fromisoformat(server['expires_utc'])
    now = now or datetime.now(timezone.utc)
    if (expiry.utcoffset() is None or limit.utcoffset() is None or not now < expiry <= limit
            or args.renewal != server['renewal_id']):
        raise ValueError('Current bounded session required')
    return expiry


def run(args):
    if Path(__file__).resolve() != ROOT / 'tools/record_google_handoff.py':
        raise ValueError('Deployed operator path required')
    if str(UUID(args.plan)) != args.plan or not re.fullmatch(r'[a-f0-9]{64}', args.key):
        raise ValueError('Exact reserved identity required')
    def guard():
        return validate_session(args, json.loads(SERVER.read_text()))
    expiry = guard()
    now = datetime.now(timezone.utc)
    def expired(signum, frame):
        raise TimeoutError('Bounded receipt recording ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(30, (expiry-now).total_seconds()))
    evidence = read_evidence(sys.stdin.buffer)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import real_outings
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    plan = real_outings.record_operator(actor=actor, profile_id=1, plan_id=args.plan,
                                       kind=args.kind, key=args.key, evidence=evidence, session_guard=guard)
    return {'state': plan.data['handoffs'][args.kind]['state'], 'plan': str(plan.pk),
            'kind': args.kind, 'version': plan.version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--kind', choices=('calendar', 'email'), required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args())
    except Exception as error:
        print(json.dumps({'state': 'unknown', 'reason': 'google_readback_not_recorded',
                          'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
