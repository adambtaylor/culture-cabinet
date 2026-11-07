"""Add only the owner's explicitly authorized P3.23 discovery seeds.

Uses the existing bounded SSH operator and current grant/expiry checks. This
records authored exploration requests, never ratings, endorsements or playback.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

from record_google_handoff import ROOT, SERVER, validate_session


def run(args):
    if Path(__file__).resolve() != ROOT / 'tools/record_authored_seeds.py':
        raise ValueError('Deployed operator path required')
    def guard():
        return validate_session(args, json.loads(SERVER.read_text()))
    expiry = guard()
    def expired(signum, frame):
        raise TimeoutError('Bounded authored seed operation ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL,
        min(30, (expiry - datetime.now(timezone.utc)).total_seconds()))
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import authored_discovery
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    state = authored_discovery.authorize_defaults_operator(actor, 1, guard)
    return {'state': 'recorded', 'actor_id': actor.pk, 'profile_id': 1,
            'revision': state.revision, 'seed_ids': [row['id'] for row in state.data['seeds']],
            'seed_fingerprint': authored_discovery.revision(state.data),
            'numeric_ratings_created': 0, 'provider_calls': 0, 'playback_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args())
    except Exception as error:
        print(json.dumps({'state': 'unknown', 'reason': 'authored_seed_operation_not_confirmed',
                          'error_type': type(error).__name__}))
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
