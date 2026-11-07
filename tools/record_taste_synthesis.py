"""Reserve or record one supervised Astra inference using the existing owner SSH.

This command does not invoke a model. Only a first reserve exports the prompt;
record input must come from the actual reserved Codex collaboration task.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
from uuid import UUID

from record_google_handoff import ROOT, SERVER, read_evidence, validate_session


def run(args):
    if Path(__file__).resolve() != ROOT / 'tools/record_taste_synthesis.py':
        raise ValueError('Deployed operator path required')
    if args.action != 'ready' and (not args.run or str(UUID(args.run)) != args.run):
        raise ValueError('Exact synthesis identity required')
    def guard():
        return validate_session(args, json.loads(SERVER.read_text()))
    expiry = guard()
    def expired(signum, frame):
        raise TimeoutError('Bounded synthesis handoff ended')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(30, (expiry - datetime.now(timezone.utc)).total_seconds()))
    evidence = read_evidence(sys.stdin.buffer) if args.action == 'record' else None
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'backend'))
    from cabinet import boundary
    boundary.prepare(['catalogue_prepare', '--provider-demo'], os.environ)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from shelf import synthesis
    actor = get_user_model().objects.get(pk=1, is_active=True)
    if actor.get_username() != 'demo-curator':
        raise ValueError('Existing owner required')
    if args.action == 'ready':
        from shelf.pick_enhancement import ready
        return ready(actor=actor, profile_id=1, session_guard=guard)
    kwargs = dict(actor=actor, profile_id=1, run_id=args.run, session_guard=guard)
    if args.action == 'reserve':
        row, packet = synthesis.reserve_operator(**kwargs)
        return {'state': row.state, 'revision': row.revision, 'packet': packet}
    row = synthesis.record_operator(**kwargs, evidence=evidence)
    return {'state': row.state, 'run_id': str(row.pk), 'revision': row.revision,
            'input_digest': row.input_digest, 'attempts': row.attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('ready', 'reserve', 'record'))
    parser.add_argument('--run')
    parser.add_argument('--expires', required=True)
    parser.add_argument('--renewal', required=True)
    try:
        result = run(parser.parse_args())
    except Exception as error:
        print(json.dumps({'state': 'unknown', 'reason': 'synthesis_operation_not_confirmed',
                          'error_type': type(error).__name__}))
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
