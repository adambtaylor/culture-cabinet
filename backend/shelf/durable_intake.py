"""Conventional durable human intake, with temporary provider decks kept in sessions.

History records explicit responses/corrections/undo only. Receipts protect every
POST with optimistic revision, without storing any unjudged provider metadata.
"""
import hashlib
import json
import math
import time
import secrets
from copy import deepcopy

from django.contrib.auth import get_user
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shelf import intake
from shelf.feedback_scale import clean_note
from shelf.authority import SELECTION_KEY
from shelf.models import IntakeRun

MAX_HISTORY = 2000
MAX_RECEIPTS = 4000


def available():
    return IntakeRun._meta.db_table in connection.introspection.table_names()


def authority(actor, profile_id):
    from shelf.provider_views import _profiles
    if not _profiles(actor, 'select').filter(pk=profile_id).exists():
        raise Http404


def owned(actor, profile_id, run_id):
    authority(actor, profile_id)
    try:
        return IntakeRun.objects.get(pk=run_id, actor=actor, profile_id=profile_id)
    except (IntakeRun.DoesNotExist, ValueError, ValidationError):
        raise Http404 from None


def new_run(actor, profile_id, *, revision=0, nonce=None):
    with transaction.atomic():
        authority(actor, profile_id)
        run = IntakeRun.objects.create(actor=actor, profile_id=profile_id, revision=revision, nonce=nonce or secrets.token_urlsafe(24))
        authority(actor, profile_id)
        return run


def snapshot(card):
    """Whitelist a human-selected identity; exclude descriptions and optional metadata."""
    from shelf.saved import canonical_url
    if type(card) is not dict:
        raise intake.IntakeError()
    provider, medium, source_id = (card.get(k) for k in ('provider', 'medium', 'source_id'))
    url = canonical_url(provider, medium, source_id, card.get('url'))
    try:
        fetched = parse_datetime(card.get('fetched_at', '')) if isinstance(card.get('fetched_at'), str) else None
    except (TypeError, ValueError, OverflowError):
        fetched = None
    title, lane, origin, root = (card.get(k) for k in ('title', 'lane', 'origin', 'root'))
    if (not url or card.get('url') != url or card.get('key') != f'{provider}:{medium}:{source_id}'
            or not isinstance(title, str) or not title.strip() or len(title) > 300
            or not isinstance(lane, str) or not 1 <= len(lane) <= 160
            or origin not in ('editorial', 'search', 'neighbor')
            or not isinstance(root, str) or len(root) > 100
            or fetched is None or timezone.is_naive(fetched)):
        raise intake.IntakeError()
    return {k: card[k] for k in ('key', 'provider', 'medium', 'source_id', 'title', 'url',
                                'fetched_at', 'lane', 'origin', 'root')}


def effective_feedback(run):
    """Latest explicit response per identity; undo clears it, including its note."""
    feedback = {}
    for event in run.history:
        key = event['card']['key']
        feedback.pop(key, None)
        if event['choice'] is not None:
            value = {'choice': event['choice'], 'card': event['card']}
            if 'note' in event:
                value['note'] = event['note']
            feedback[key] = value
    return feedback


def effective(run):
    """Keep the historical two-tuple API for callers and archived runs."""
    choices = {key: value['choice'] for key, value in effective_feedback(run).items()}
    cards = {event['card']['key']: event['card'] for event in run.history}
    return choices, cards


def hydrate(run, cached=None):
    state = deepcopy(cached) if cached else intake.new_state(run.actor_id, run.profile_id)
    state.update(run_id=str(run.pk), revision=run.revision, nonce=run.nonce,
                 selector_version=run.selector_version, metadata_version='provider-catalogue-v1')
    choices, cards = effective(run)
    state['choices'] = choices
    state['notes'] = {key: value['note'] for key, value in effective_feedback(run).items() if 'note' in value}
    # These snapshots support scoring/correction, not a fetched temporary deck.
    # Drop previously synthesized snapshots, including responses now cleared.
    for key in state.get('history_only', []):
        state['candidates'].pop(key, None)
    state['history_only'] = []
    for key in choices:
        card = cards[key]
        if key not in state['candidates']:
            state['candidates'][key] = dict(card, description='', lane_label='Previously judged source',
                trailer_status='Trailer unavailable.')
            state['history_only'].append(key)
    historical = set(state['history_only'])
    for field in ('queued', 'search_results'):
        state[field] = [key for key in state[field] if key in state['candidates'] and key not in historical]
    intake.select_round_v2(state)
    return state


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def receipt(run, nonce, payload):
    previous = run.receipts.get(nonce)
    if previous:
        if previous['digest'] != fingerprint(payload):
            raise intake.IntakeError(409)
        return previous
    return None


def _commit(*, actor, profile_id, run_id, revision, nonce, payload, state):
    """CAS update: stale tabs cannot overwrite a newer response or correction.

    A repeated nonce with the same payload returns its original receipt; a
    changed payload conflicts. No provider I/O occurs inside the transaction.
    """
    if 'note' in payload:
        if payload.get('action') not in ('choose', 'correct'):
            raise intake.IntakeError()
        try:
            clean_note(payload['note'])
        except ValueError:
            raise intake.IntakeError() from None
    with transaction.atomic():
        run = owned(actor, profile_id, run_id)
        prior = receipt(run, nonce, payload)
        if prior:
            return run, prior
        if (run.archived_at or run.revision != revision or run.nonce != nonce
                or state.get('run_id') != str(run.pk)
                or state.get('actor_id') != actor.pk or state.get('profile_id') != profile_id
                or state.get('revision') != revision or state.get('nonce') != nonce
                or state.get('expires_at', 0) <= time.time()):
            raise intake.IntakeError(409)
        operation = payload['action']
        if (len(run.receipts) >= MAX_RECEIPTS and operation not in ('reset', 'new_run')
                or len(run.history) >= MAX_HISTORY and operation in ('choose', 'correct', 'undo')):
            raise intake.IntakeError(409)
        history = deepcopy(run.history)
        choices, cards = effective(run)
        if operation in ('choose', 'correct', 'undo'):
            key = payload.get('candidate_id')
            choice = payload.get('choice')
            if operation == 'undo':
                if not choices:
                    raise intake.IntakeError()
                key = key or next(reversed(choices))
                choice = None
            elif choice not in intake.CHOICES:
                raise intake.IntakeError()
            if operation == 'choose':
                if key not in state['current'] or key in choices:
                    raise intake.IntakeError()
                card = snapshot(state['candidates'].get(key))
            else:
                if key not in choices:
                    raise intake.IntakeError()
                card = cards[key]
            history.append({'card': card, 'choice': choice, 'operation': operation,
                            'responded_at': timezone.now().isoformat(), 'recorded_at': timezone.now().isoformat(),
                            'provenance': 'authenticated-human-post'})
            if 'note' in payload:
                history[-1]['note'] = payload['note']
        result = {'digest': fingerprint(payload), 'revision': revision + 1, 'action': operation}
        if operation == 'resume':
            target = owned(actor, profile_id, payload.get('run_id'))
            if target.archived_at:
                raise intake.IntakeError(409)
            result['next_run_id'] = str(target.pk)
        elif operation in ('reset', 'new_run'):
            replacement = new_run(actor, profile_id)
            result['next_run_id'] = str(replacement.pk)
        receipts = dict(run.receipts, **{nonce: result})
        values = dict(revision=revision+1, nonce=secrets.token_urlsafe(24), history=history, receipts=receipts)
        if operation in ('reset', 'new_run'):
            values['archived_at'] = timezone.now()
        authority(actor, profile_id)
        updated = IntakeRun.objects.filter(pk=run.pk, revision=revision, nonce=nonce, archived_at__isnull=True).update(**values)
        if not updated:
            raise intake.IntakeError(409)
        authority(actor, profile_id)
        run.refresh_from_db()
        return run, result


def import_state(*, actor, profile_id, state, session_key):
    """Import only validated choices from an unexpired authenticated session.

    Caller supplies a framework-authenticated actor, never a posted actor ID.
    Each session becomes a separate run. Original response times are unknown.
    """
    authority(actor, profile_id)
    if (type(state) is not dict or type(state.get('actor_id')) is not int
            or type(state.get('profile_id')) is not int
            or state.get('actor_id') != actor.pk or state.get('profile_id') != profile_id
            or type(state.get('expires_at')) not in (int, float)
            or not math.isfinite(state['expires_at'])
            or state['expires_at'] <= time.time() or not session_key):
        return None
    choices = state.get('choices')
    candidates = state.get('candidates')
    if type(choices) is not dict or not choices or len(choices) > intake.MAX_CANDIDATES or type(candidates) is not dict:
        return None
    history = []
    try:
        for key, choice in choices.items():
            if choice not in intake.CHOICES or key not in candidates:
                raise intake.IntakeError()
            card = snapshot(candidates[key])
            if card['key'] != key:
                raise intake.IntakeError()
            history.append({'card': card, 'choice': choice, 'operation': 'import',
                'responded_at': None, 'recorded_at': timezone.now().isoformat(),
                'provenance': 'server-session-v1; original response time unknown'})
            if key in state.get('notes', {}):
                history[-1]['note'] = clean_note(state['notes'][key])
    except (intake.IntakeError, TypeError, ValueError):
        return None
    token = hashlib.sha256(('intake-session-v1:' + session_key).encode()).hexdigest()
    with transaction.atomic():
        authority(actor, profile_id)
        run, _ = IntakeRun.objects.get_or_create(imported_session=token,
            defaults={'actor': actor, 'profile_id': profile_id, 'nonce': secrets.token_urlsafe(24), 'history': history})
        if run.actor_id != actor.pk or run.profile_id != profile_id:
            raise Http404
        authority(actor, profile_id)
        return run


def preserve_sessions():
    """Explicit stopped-runtime preparation only; no session writes or login revival."""
    count = 0
    for row in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        # Framework authentication on an in-memory copy: invalid hashes may
        # clear this copy, never mutate the existing login session.
        class AuthSession(dict):
            def flush(self):
                self.clear()
            def cycle_key(self):
                self.clear()
        session = AuthSession(row.get_decoded())
        actor = get_user(type('SessionRequest', (), {'session': session})())
        if not actor.is_authenticated or not actor.is_active:
            continue
        state = session.get(intake.SESSION_KEY)
        profile_id = session.get(SELECTION_KEY)
        try:
            if profile_id == 1 and import_state(actor=actor, profile_id=profile_id,
                    state=state, session_key=row.session_key):
                count += 1
        except Http404:
            continue
    return count


def export_history(actor, profile_id):
    authority(actor, profile_id)
    output = {'schema': 'human-intake-v1', 'runs': []}
    size = 128
    # Bound the aggregate before accumulating it; each individual run already
    # has a finite response count and whitelisted field lengths.
    for run in IntakeRun.objects.filter(actor=actor, profile_id=profile_id).iterator(chunk_size=1):
        row = {'id': str(run.pk), 'selector_version': run.selector_version, 'revision': run.revision,
               'created_at': run.created_at.isoformat(),
               'archived_at': run.archived_at.isoformat() if run.archived_at else None,
               'history': run.history}
        size += len(json.dumps(row, ensure_ascii=True)) + 2
        if size > 2 * 1024 * 1024:
            raise intake.IntakeError(413)
        output['runs'].append(row)
    authority(actor, profile_id)
    return output


def replay(actor, profile_id, nonce, payload):
    authority(actor, profile_id)
    run = IntakeRun.objects.filter(actor=actor, profile_id=profile_id,
                                   receipts__has_key=nonce).first()
    if run:
        return run, receipt(run, nonce, payload)
    return None


def upgrade(actor, profile_id, state, session_key, *, create=False):
    """Attach an owned run; never merge separate session histories."""
    if not available():
        return state
    if state.get('run_id'):
        return hydrate(owned(actor, profile_id, state['run_id']), state)
    if state.get('choices'):
        run = import_state(actor=actor, profile_id=profile_id, state=state, session_key=session_key)
        return hydrate(run, state) if run else state
    if state.get('candidates'):
        try:
            for card in state['candidates'].values():
                snapshot(card)
        except (intake.IntakeError, TypeError, ValueError):
            # Keep the old prototype usable with unsupported source contracts;
            # never manufacture durable provenance for an invalid identity.
            return state
        if create:
            run = new_run(actor, profile_id, revision=state['revision'], nonce=state['nonce'])
            return hydrate(run, state)
        return state
    authority(actor, profile_id)
    run = IntakeRun.objects.filter(actor=actor, profile_id=profile_id, archived_at__isnull=True).first()
    return hydrate(run, state) if run else state


def commit(**kwargs):
    try:
        return _commit(**kwargs)
    except OperationalError as error:
        # A contending SQLite writer must not become an HTTP 500 or overwrite
        # the winner. The caller may reload and submit against the new revision.
        code = getattr(error.__cause__, 'sqlite_errorcode', 0)
        if code & 255 in (5, 6):
            raise intake.IntakeError(409) from None
        raise
