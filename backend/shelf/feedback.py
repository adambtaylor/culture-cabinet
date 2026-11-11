"""Typed profile-private statements. Coordinator lock always precedes IMMEDIATE.

Revisions describe one signal, never the whole item. Reversal reduces history;
it never installs an untrusted before-image. No service accepts actor identity
or authority from a submitted body.
"""
from contextlib import contextmanager
from functools import wraps
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import sqlite3
import uuid

from django.db import connection
from django.http import Http404
from django.utils import timezone

from shelf.models import (FeedbackEvent, FeedbackProjection, FeedbackSubject,
                         FeedbackTarget, Grant, MutationReceipt)

SIGNALS = ('enjoyment', 'interest', 'familiarity', 'saved', 'skipped', 'opened',
           'completion', 'constraint', 'visibility', 'legacy_rating', 'legacy_state', 'seed_statement', 'note')
LABELS = dict(zip(SIGNALS, ('Experienced enjoyment', 'Future interest', 'Familiarity',
    'Saved', 'Skipped', 'Explicitly opened', 'Completion', 'Practical constraint',
    'Visibility', 'Legacy rating (basis unknown)', 'Legacy state', 'Attributed statement', 'Preference note')))
NATIVE_LABELS = {'buttons': ['1', '2', '3', '4', '5'], 'aria_pattern': 'X out of 5',
                 'low': 'LOW', 'high': 'HIGH', 'hint': 'LOW → HIGH'}
ORIGINS = ('direct', 'legacy_snapshot', 'imported_statement', 'seed_statement')


class FeedbackError(Exception):
    status = 400
    def __init__(self, message='Invalid feedback.'):
        super().__init__(message)


class Conflict(FeedbackError):
    status = 409


class Gone(FeedbackError):
    status = 410


class Busy(FeedbackError):
    status = 503


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def strict_json(data, *, limit=64 * 1024 * 1024):
    if not isinstance(data, bytes) or len(data) > limit:
        raise FeedbackError()
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise FeedbackError()
            d[k] = v
        return d
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(FeedbackError()))
        def depth(v, n=0):
            if n > 16:
                raise FeedbackError()
            if isinstance(v, dict):
                for x in v.values(): depth(x, n+1)
            elif isinstance(v, list):
                for x in v: depth(x, n+1)
        depth(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise FeedbackError() from None


def exact(obj, fields):
    if not isinstance(obj, dict) or set(obj) != set(fields):
        raise FeedbackError()
    return obj


def uid(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise FeedbackError() from None


def text(value, maximum, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or '\0' in value or (not empty and not value):
        raise FeedbackError()
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise FeedbackError()
    return value


def validate_body(signal, body, *, imported=False):
    if signal not in SIGNALS or (not imported and signal.startswith(('legacy_', 'seed_'))):
        raise FeedbackError()
    if body is None:
        return None
    if signal in ('enjoyment', 'legacy_rating'):
        keys = ('value', 'basis', 'scale') if signal == 'enjoyment' else ('value', 'basis', 'scale', 'labels')
        exact(body, keys)
        if type(body['value']) is not int or body['value'] not in range(1, 6): raise FeedbackError()
        if signal == 'enjoyment':
            if (body['basis'], body['scale']) != ('experienced', 'integer-1-5'): raise FeedbackError()
        elif (body['basis'], body['scale'], body['labels']) != ('legacy_unknown', 'legacy-native-1-5', NATIVE_LABELS):
            raise FeedbackError()
    elif signal == 'interest':
        exact(body, ('value', 'basis'))
        if body['value'] not in ('no', 'maybe', 'yes') or body['basis'] != 'anticipated': raise FeedbackError()
    elif signal in ('constraint', 'note'):
        exact(body, ('text',)); text(body['text'], 500)
    elif signal == 'seed_statement':
        exact(body, ('text', 'attribution', 'basis')); text(body['text'], 500); text(body['attribution'], 160)
        if body['basis'] != 'attributed_statement': raise FeedbackError()
    else:
        exact(body, ('value',)); v = body['value']
        allowed = {'familiarity': ('unknown', 'unfamiliar', 'aware', 'experienced'),
                   'completion': ('not_started', 'in_progress', 'completed'),
                   'visibility': ('visible', 'hidden'), 'legacy_state': ('rated', 'unseen', 'skipped')}
        if signal in ('saved', 'skipped', 'opened'):
            if type(v) is not bool or (signal == 'opened' and v is not True): raise FeedbackError()
        elif not isinstance(v, str) or v not in allowed[signal]: raise FeedbackError()
    return body


@dataclass(frozen=True)
class MutationCommand:
    mutation_id: str
    target_id: str
    target_type: str
    signal: str
    scope: str
    operation: str
    base_revision: int
    context_event_id: str | None
    value: dict | None = None
    reverses_id: str | None = None
    corrects_id: str | None = None


@dataclass(frozen=True)
class MutationResult:
    event_id: str
    profile_id: int
    target_id: str
    signal: str
    scope: str
    accepted_revision: int
    status: str = 'accepted'


@dataclass(frozen=True)
class EraseCommand:
    mutation_id: str
    target_id: str | None
    scope: str
    signals: tuple | str
    base_revisions: dict
    context_event_ids: dict
    acknowledgement: str


@dataclass(frozen=True)
class EraseReport:
    status: str
    restriction_sequences: tuple


def envelope_hash(operation, profile_id, payload, binding=None):
    return hashlib.sha256(canonical({'version': 1, 'operation': operation,
        'profile_id': int(profile_id), 'trusted_binding': binding, 'payload': payload})).hexdigest()


@contextmanager
def immediate():
    """Own the outer transaction; bounded SQLite lock waits, no nested TestCase.

    Callers retry complete transactions, including fresh authorization. busy_timeout
    is connection-local and restored even when BEGIN or COMMIT fails.
    """
    connection.ensure_connection()
    if connection.in_atomic_block or not connection.get_autocommit() or connection.connection.in_transaction:
        raise FeedbackError('An outer transaction is required.')
    raw = connection.connection
    timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
    raw.execute('PRAGMA busy_timeout=50')
    prior_rollback = connection.needs_rollback
    try:
        raw.execute('BEGIN IMMEDIATE')
        # Let ORM helpers use SAVEPOINTs inside our explicitly owned IMMEDIATE
        # transaction instead of asking Django to open a second BEGIN.
        connection.in_atomic_block = True
        yield
        if connection.needs_rollback:
            raise FeedbackError('Transaction requires rollback.')
        raw.execute('COMMIT')
    except BaseException:
        if raw.in_transaction: raw.execute('ROLLBACK')
        raise
    finally:
        connection.in_atomic_block = False
        connection.needs_rollback = prior_rollback
        raw.execute(f'PRAGMA busy_timeout={timeout}')


def is_busy(error):
    cause = error.__cause__ or error
    code = getattr(cause, 'sqlite_errorcode', 0)
    return code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def authorize(actor, profile_id, action):
    from shelf.authority import require_profile
    profile = require_profile(actor, profile_id, action)
    return profile, Grant.objects.get(user_id=actor.pk, profile_id=profile.pk)


def prior_receipt(actor, mid, profile_id, family, digest):
    receipt = MutationReceipt.objects.filter(actor_id=actor.pk, mutation_id=mid).first()
    if receipt is None: return None
    if receipt.state == 'retired': raise Gone('This request has been retired.')
    if (receipt.profile_id, receipt.operation_family, receipt.payload_sha256) != (int(profile_id), family, digest):
        raise Conflict('This request ID already describes another operation.')
    return receipt


def receipt_result(receipt):
    event = FeedbackEvent.objects.get(pk=receipt.event_id)
    return MutationResult(str(event.pk), event.profile_id, str(event.target_id), event.signal,
                          event.scope, receipt.accepted_revision)


def reduce_events(events):
    events = list(events)
    if any(e.erased or e.operation == 'erase' for e in events): return None, None, True
    reversed_ids = {e.reverses_id for e in events if e.operation == 'reverse'}
    effective = next((e for e in reversed(events) if e.operation == 'set' and e.pk not in reversed_ids), None)
    return (effective.pk, effective.body, False) if effective else (None, None, False)


def recompute(key):
    events = list(FeedbackEvent.objects.filter(**key).order_by('revision'))
    effective, value, erased = reduce_events(events)
    FeedbackProjection.objects.update_or_create(**key, defaults={'revision': events[-1].revision,
        'effective_event_id': effective, 'value': value, 'erased': erased})


def key_token(target, signal, scope):
    return f'{target}:{scope}:{signal}'


def validate_command(command):
    if not isinstance(command, MutationCommand): raise FeedbackError()
    uid(command.mutation_id); uid(command.target_id); integer(command.base_revision)
    for field in ('context_event_id', 'reverses_id', 'corrects_id'):
        if getattr(command, field) is not None: uid(getattr(command, field))
    if command.operation not in ('set', 'reverse') or command.target_type not in ('item', 'session') or command.scope != command.target_type:
        raise FeedbackError()
    if command.operation == 'set':
        validate_body(command.signal, command.value)
        if command.reverses_id: raise FeedbackError()
    elif command.signal not in SIGNALS or not command.reverses_id or command.corrects_id or command.value is not None:
        raise FeedbackError()


def mutate_in_transaction(*, actor, profile_id, command, barrier):
    """Join a checked outer IMMEDIATE writer; batch callers commit one receipt.

    Validation, authority and reducer semantics are shared with ordinary mutate.
    This port never opens/commits a transaction or accepts a supplied before-image.
    """
    if not connection.in_atomic_block or not connection.connection.in_transaction:
        raise FeedbackError('An owning transaction is required.')
    validate_command(command)
    payload = asdict(command); payload.pop('mutation_id')
    family = 'feedback.' + command.operation
    digest = envelope_hash(family, profile_id, payload)
    profile, grant = authorize(actor, profile_id, 'mutate')
    old = prior_receipt(actor, command.mutation_id, profile_id, family, digest)
    if old: return receipt_result(old)
    target = FeedbackTarget.objects.filter(pk=command.target_id, deleted=False).first()
    if not target or target.kind != command.target_type: raise Http404
    if target.kind == 'session' and (not target.parent_id or target.parent.kind != 'item'): raise FeedbackError()
    key = dict(profile_id=profile.pk, target_id=target.pk, signal=command.signal, scope=command.scope)
    events = FeedbackEvent.objects.filter(**key).order_by('revision')
    last = events.last()
    if (last.revision if last else 0) != command.base_revision or (str(last.pk) if last else None) != command.context_event_id:
        raise Conflict('This signal changed. Refresh before editing.')
    projection = FeedbackProjection.objects.filter(**key).first()
    if projection and projection.erased: raise Gone('This feedback scope is erased.')
    barrier.assert_scope_open(profile.pk, target.pk, command.signal, command.scope)
    if command.corrects_id and (not projection or str(projection.effective_event_id) != command.corrects_id):
        raise Conflict('Refresh the current statement before correcting it.')
    if command.operation == 'reverse':
        original = events.filter(pk=command.reverses_id, operation='set', erased=False).first()
        if not original or events.filter(reverses_id=original.pk).exists():
            raise Conflict('This statement cannot be undone again.')
    from shelf.models import ProfileCabinetState
    cabinet = ProfileCabinetState.objects.filter(profile=profile, data_schema_version=2).first()
    subject, _ = FeedbackSubject.objects.get_or_create(profile=profile,
        defaults={'subject_id': cabinet.subject_ref if cabinet else uuid.uuid4()})
    now = timezone.now()
    event = FeedbackEvent.objects.create(id=uuid.uuid4(), **key, revision=command.base_revision+1,
        operation=command.operation, actor_id=actor.pk, authority_grant_id=grant.pk,
        recorded_at=now, origin='direct', original_origin='direct',
        attribution={'subject_id': str(subject.subject_id)}, original_recorded_at=now,
        body=command.value, reverses_id=command.reverses_id, corrects_id=command.corrects_id)
    recompute(key)
    receipt = MutationReceipt.objects.create(actor_id=actor.pk, mutation_id=command.mutation_id,
        profile_id=profile.pk, operation_family=family, target_id=target.pk,
        payload_sha256=digest, event_id=event.pk, accepted_revision=event.revision, created_at=now)
    authorize(actor, profile.pk, 'mutate')
    return receipt_result(receipt)


def mutate(*, actor, profile_id, command):
    from shelf.grant_authority import writer_window
    from shelf.recovery import access_barrier
    validate_command(command)
    with access_barrier() as barrier:
        for attempt in range(3):
            try:
                with immediate(), writer_window(actor, profile_id):
                    recorded=MutationReceipt.objects.filter(actor=actor,mutation_id=command.mutation_id).exists()
                    result = mutate_in_transaction(actor=actor, profile_id=profile_id,
                        command=command, barrier=barrier)
                # A receipt replay cannot invent a point lost in the original
                # post-commit crash gap or attribute later choices to this input.
                if not recorded:
                    from shelf.portable_taste_history import capture_native_mutation
                    capture_native_mutation(actor,profile_id,command)
                return result
            except Exception as error:
                if not is_busy(error): raise
                if attempt == 2: raise Busy('Feedback is busy. Retry this request.') from None



def bounded_preflight(function):
    @wraps(function)
    def call(*args, **kwargs):
        for attempt in range(3):
            try: return function(*args, **kwargs)
            except Exception as error:
                if not is_busy(error): raise
                if attempt == 2: raise Busy() from None
    return call


@bounded_preflight
def erase_feedback(*, actor, profile_id, command, recovery):
    from shelf.recovery import installed_coordinator
    if recovery is not installed_coordinator() or not isinstance(command,EraseCommand): raise FeedbackError()
    if not isinstance(command.base_revisions,dict) or not isinstance(command.context_event_ids,dict): raise FeedbackError()
    if set(command.base_revisions)!=set(command.context_event_ids): raise Conflict()
    for key,revision in command.base_revisions.items():
        text(key,128,empty=False); integer(revision)
        context=command.context_event_ids[key]
        if context is not None: uid(context)
    if command.signals!='all' and not isinstance(command.signals,(tuple,list)): raise FeedbackError()
    uid(command.mutation_id)
    if command.acknowledgement != 'ERASE_FICTIONAL_FEEDBACK' or command.scope not in ('item', 'session', 'profile'):
        raise FeedbackError()
    if command.scope == 'profile':
        if command.target_id is not None: raise FeedbackError()
    else: uid(command.target_id)
    signals = list(SIGNALS) if command.signals == 'all' else list(command.signals)
    if not signals or len(set(signals)) != len(signals) or any(s not in SIGNALS for s in signals): raise FeedbackError()
    payload = asdict(command); payload.pop('mutation_id'); payload['signals'] = sorted(signals)
    digest = envelope_hash('feedback.erase', profile_id, payload)
    with recovery.barrier(recovery.store_id) as barrier:
        with immediate():
            profile, grant = authorize(actor, profile_id, 'mutate')
            prior_receipt(actor, command.mutation_id, profile.pk, 'feedback.erase', digest)
            qs = FeedbackProjection.objects.filter(profile=profile, signal__in=signals)
            if command.scope != 'profile':
                target = FeedbackTarget.objects.filter(pk=command.target_id, kind=command.scope).first()
                if not target: raise Http404
                ids = [target.pk] + list(FeedbackTarget.objects.filter(parent=target).values_list('pk', flat=True))
                qs = qs.filter(target_id__in=ids)
            rows = list(qs)
            expected = {key_token(str(p.target_id), p.signal, p.scope): p.revision for p in rows}
            contexts = {key_token(str(p.target_id), p.signal, p.scope): str(FeedbackEvent.objects.get(
                profile=profile, target_id=p.target_id, signal=p.signal, scope=p.scope, revision=p.revision).pk) for p in rows}
            if command.base_revisions != expected or command.context_event_ids != contexts: raise Conflict()
            restrictions = [dict(kind='erase', profile_id=profile.pk, target_id=str(p.target_id), signal=p.signal,
                scope=p.scope, subject_id=None, restriction_code='erase_feedback') for p in rows]
            if not restrictions: raise FeedbackError('No feedback in this scope.')
            # Closure markers also deny future statements on previously absent
            # signals/targets. Value-bearing erasure still has one explicit
            # restriction/event marker for every existing affected key.
            if command.scope == 'profile':
                restrictions += [dict(kind='erase',profile_id=profile.pk,target_id=None,signal=s,
                    scope='profile',subject_id=None,restriction_code='erase_feedback')
                    for s in ([None] if command.signals=='all' else signals)]
            elif command.signals == 'all':
                restrictions += [dict(kind='erase',profile_id=profile.pk,target_id=str(t.pk),signal=None,
                    scope=t.kind,subject_id=None,restriction_code='erase_feedback')
                    for t in FeedbackTarget.objects.filter(pk__in=ids)]
        pending = barrier.prepare(command.mutation_id, restrictions, audit=(actor.pk, grant.pk))
        # Only minimal, previously authorized denials cross the filesystem/DB gap.
        barrier.apply_pending(connection.connection, require_current_authority=True)
        barrier.finalize(pending, pending['local_commit_uuid'])
        return EraseReport('accepted', tuple(r['sequence'] for r in pending['restrictions']))


@dataclass(frozen=True)
class FeedbackContext:
    selected_profile: object
    target: object
    signals: tuple
    forms: tuple
    undo_forms: tuple
    can_mutate: bool
    can_export: bool
    is_hidden: bool
    message: str = ''


def feedback_context(*, actor, profile_id, target_id, include_hidden=False):
    from shelf.recovery import access_barrier
    from shelf.forms import forms_for
    with access_barrier():
        profile, grant = authorize(actor, profile_id, 'view')
        target = FeedbackTarget.objects.filter(pk=target_id, deleted=False).first()
        if not target: raise Http404
        rows = {p.signal: p for p in FeedbackProjection.objects.filter(profile=profile, target=target)}
        hidden = rows.get('visibility') and rows['visibility'].value == {'value': 'hidden'}
        if target.parent_id:
            hidden = hidden or FeedbackProjection.objects.filter(profile=profile, target_id=target.parent_id,
                signal='visibility', value={'value': 'hidden'}).exists()
        if hidden and not include_hidden: raise Http404
        signals = tuple({'name': s, 'label': LABELS[s], 'value': rows[s].value if s in rows else None,
            'basis': (rows[s].value or {}).get('basis') if s in rows else None,
            'scale': (rows[s].value or {}).get('scale') if s in rows else None,
            'revision': rows[s].revision if s in rows else 0,
            'effective_event_id': str(rows[s].effective_event_id) if s in rows and rows[s].effective_event_id else None}
            for s in SIGNALS)
        forms, undo = forms_for(profile, target, rows) if grant.can_mutate else ((), ())
        return FeedbackContext(profile, target, signals, forms, undo, grant.can_mutate,
                               grant.can_export, bool(hidden))
