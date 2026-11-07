"""Bounded, explicit user-authored exploration; never inferred enjoyment.

This channel owns no ratings or provider credentials and performs no network I/O.
A supplied catalog must contain only episodes validated by the podcast adapter.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
import secrets

from django.core import signing
from django.db import connection, transaction
from django.utils import timezone

from shelf.discovery_state import Conflict, public_authorized
from shelf.models import AuthoredDiscoveryState

MAX_SEEDS = 30
MAX_SHORTLIST = 60
MAX_RECEIPTS = 500
DEFAULT_AUTHORIZATION = 'Add the five P3.23 user-authored nonfiction discovery topic clusters.'
DEFAULT_PROVENANCE = 'Direct user instruction, P3.23 nonfiction discovery increment'


def _default(identity, topic, terms, personalities, source_text):
    return dict(id=identity, topic=topic, terms=terms, personality_seeds=personalities,
                provenance=DEFAULT_PROVENANCE, source_text=source_text,
                authorization=DEFAULT_AUTHORIZATION)


DEFAULT_SEEDS = (
    _default('deep_dives', 'Deep dives: AI, history and geopolitics',
             ['AI', 'history', 'geopolitics', 'long-form interviews'], ['Lex Fridman'],
             'deep dives AI/history/geopolitics (Lex Fridman style)'),
    _default('macroeconomics', 'Macro financial systems, rates and money markets',
             ['macro financial systems', 'interest rates', 'money markets'], [],
             'macro financial systems/rates/money markets'),
    _default('sleep_health', 'Sleep and health optimisation',
             ['sleep', 'health optimisation'], ['Bryan Johnson', 'Huberman', 'Matthew Walker'],
             'sleep/health optimisation (Bryan Johnson dictated Brian/Optimize, Huberman, Matthew Walker discovery personalities not endorsement)'),
    _default('living_soil', 'Urban orchards, soil microbes, fungi and gardening',
             ['urban orchards', 'soil microbes', 'fungi', 'evidence-based high-performance gardening'], [],
             'urban orchards/soil microbes/fungi/evidence-based high-performance gardening'),
    _default('unusual_history', 'Espionage and unusual history',
             ['espionage', 'unusual history', 'wartime amphetamines'], [],
             'espionage/unusual history incl wartime amphetamines'),
)


def available():
    return AuthoredDiscoveryState._meta.db_table in connection.introspection.table_names()


def empty_data():
    return {'seeds': [], 'shortlist': []}


def initial_nonce(actor_id, profile_id, mode):
    return signing.Signer(salt='authored-discovery-start').sign_object([actor_id, profile_id, mode])


def owned(request, profile_id, mode):
    if mode not in ('household', 'sample'):
        raise ValueError('Invalid authored discovery mode.')
    public_authorized(request, profile_id)
    return AuthoredDiscoveryState.objects.filter(actor=request.user, profile_id=profile_id, mode=mode).first()


def _text(value, limit, label, *, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and not (multiline and c in '\n\t\r') for c in value):
        raise ValueError('Invalid ' + label + '.')
    return value


def seed_record(seed, actor_id, captured_at):
    """Retain supplied wording and provenance, with a server capture timestamp."""
    keys = {'id', 'topic', 'terms', 'personality_seeds', 'provenance', 'source_text', 'authorization'}
    if not isinstance(seed, dict) or set(seed) != keys:
        raise ValueError('Invalid authored seed fields.')
    result = {key: _text(seed[key], limit, key, multiline=key == 'source_text') for key, limit in
              [('id', 64), ('topic', 180), ('provenance', 500), ('source_text', 2000), ('authorization', 500)]}
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', result['id']):
        raise ValueError('Invalid seed identity.')
    for key, limit in [('terms', 12), ('personality_seeds', 8)]:
        values = seed[key]
        if not isinstance(values, (list, tuple)) or len(values) > limit:
            raise ValueError('Invalid seed ' + key + '.')
        result[key] = [_text(value, 120, key) for value in values]
    if not result['terms']:
        raise ValueError('A seed needs exploration terms.')
    result.update(captured_at=captured_at, captured_by='user:' + str(actor_id))
    return result


def signals(data):
    return [dict(key='authored:' + seed['id'], title=seed['topic'], medium='topic',
                 source='User-authored discovery seed', basis='discovery_seed', value='explore')
            for seed in data.get('seeds', [])]


def revision(data):
    """Include complete source wording and capture provenance in invalidation."""
    return sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def episode_snapshot(identity, catalog):
    """Project bounded metadata from the server's validated catalog, never POST."""
    if not isinstance(catalog, dict) or identity not in catalog:
        raise ValueError('Episode is not in the validated catalog.')
    item = catalog[identity]
    if not isinstance(item, dict) or item.get('key') != identity:
        raise ValueError('Invalid catalog episode.')
    snapshot = {'key': _text(identity, 200, 'episode identity')}
    text_fields = {'title': 500, 'show': 300, 'apple_url': 2048,
        'apple_episode_id': 100, 'apple_collection_id': 100, 'published_at': 100,
        'source_url': 2048, 'retrieved_at': 100, 'evidence_status': 100,
        'content_kind': 100, 'why': 2000}
    for key, limit in text_fields.items():
        value = item.get(key)
        if value is not None and value != '':
            snapshot[key] = _text(value, limit, key)
    if not all(snapshot.get(key) for key in ('title', 'show', 'apple_url', 'apple_episode_id')):
        raise ValueError('Incomplete validated episode.')
    duration = item.get('duration_seconds')
    if duration is not None:
        if type(duration) is not int or not 0 <= duration <= 604800:
            raise ValueError('Invalid episode duration.')
        snapshot['duration_seconds'] = duration
    books = item.get('books', [])
    if not isinstance(books, (list, tuple)) or len(books) > 8:
        raise ValueError('Invalid episode books.')
    snapshot['books'] = []
    for book in books:
        if not isinstance(book, dict):
            raise ValueError('Invalid episode book.')
        snapshot['books'].append({key: _text(book[key], 2048, key) for key in
            ('title', 'author', 'publisher_url', 'source_url', 'hardcover_url', 'why') if book.get(key)})
    if len(json.dumps(snapshot).encode()) > 20000:
        raise ValueError('Episode snapshot is too large.')
    return snapshot


def check_result(request, profile_id, reserved):
    """Revalidate a one-shot refresh before publishing its local cache result."""
    public_authorized(request, profile_id)
    if not AuthoredDiscoveryState.objects.filter(pk=reserved.pk, actor=request.user,
            profile_id=profile_id, revision=reserved.revision, nonce=reserved.nonce).exists():
        raise Conflict('A newer authored discovery action superseded this read.')


def apply(request, profile_id, fields, catalog=()):
    mode, action = fields.get('mode'), fields.get('action')
    if mode not in ('household', 'sample'):
        raise ValueError('Invalid authored discovery mode.')
    if action not in ('seed_add', 'seed_remove', 'seed_authorized_defaults', 'shortlist_add', 'shortlist_remove', 'refresh'):
        raise ValueError('Invalid authored discovery action.')
    nonce = fields.get('nonce')
    if not isinstance(nonce, str) or not 1 <= len(nonce) <= 200 or type(fields.get('revision')) is not int:
        raise ValueError('Invalid authored discovery form.')
    digest = revision(fields)
    with transaction.atomic():
        state = owned(request, profile_id, mode)
        if state is None:
            if fields['revision'] != 0 or nonce != initial_nonce(request.user.pk, profile_id, mode):
                raise Conflict('Stale initial authored discovery form. Reload.')
            state = AuthoredDiscoveryState.objects.create(actor=request.user, profile_id=profile_id,
                mode=mode, nonce=nonce, data=empty_data())
        if nonce in state.receipts:
            if state.receipts[nonce] != digest:
                raise Conflict('This form was already used for another action.')
            return None if action == 'refresh' else state
        if fields['revision'] != state.revision or nonce != state.nonce:
            raise Conflict('Stale authored discovery form. Reload.')
        if len(state.receipts) >= MAX_RECEIPTS and action not in ('seed_remove', 'shortlist_remove'):
            raise Conflict('Local authored discovery request limit reached.')
        data = deepcopy(state.data)
        now = timezone.now().isoformat()
        if action in ('seed_add', 'seed_authorized_defaults'):
            if action == 'seed_authorized_defaults':
                if fields.get('authorization') != DEFAULT_AUTHORIZATION:
                    raise ValueError('Explicit topic packet authorization is required.')
                incoming = DEFAULT_SEEDS
            else:
                incoming = [fields.get('seed')]
            for supplied in incoming:
                seed = seed_record(supplied, request.user.pk, now)
                existing = next((row for row in data['seeds'] if row['id'] == seed['id']), None)
                if existing:
                    if any(existing[key] != seed[key] for key in supplied):
                        raise Conflict('This seed identity already has different authored content.')
                    continue
                if len(data['seeds']) >= MAX_SEEDS:
                    raise Conflict('The 30-topic seed limit is reached.')
                data['seeds'].append(seed)
        elif action == 'seed_remove':
            data['seeds'] = [row for row in data['seeds'] if row['id'] != fields.get('identity')]
        elif action == 'refresh':
            if fields.get('identity') not in {row['id'] for row in DEFAULT_SEEDS}:
                raise ValueError('Invalid refresh topic.')
        elif action == 'shortlist_add':
            identity = fields.get('identity')
            snapshot = episode_snapshot(identity, catalog)
            if not any(row['key'] == identity for row in data['shortlist']):
                if len(data['shortlist']) >= MAX_SHORTLIST:
                    raise Conflict('The 60-episode shortlist limit is reached.')
                data['shortlist'].append(snapshot)
        else:
            data['shortlist'] = [row for row in data['shortlist'] if row['key'] != fields.get('identity')]
        public_authorized(request, profile_id)
        # Keep the fixed operator packet receipt so later cleanup cannot
        # accidentally turn a replay into re-authoring an owner-removed seed.
        operator_receipts = {key: value for key, value in state.receipts.items()
                             if key.startswith('operator-defaults:')}
        ordinary = [(key, value) for key, value in state.receipts.items()
                    if key not in operator_receipts]
        room = MAX_RECEIPTS - len(operator_receipts) - 1
        receipts = dict(ordinary[-room:]) if room > 0 else {}
        receipts.update(operator_receipts)
        receipts[nonce] = digest
        updated = AuthoredDiscoveryState.objects.filter(pk=state.pk, revision=state.revision, nonce=nonce).update(
            data=data, receipts=receipts, revision=state.revision + 1, nonce=secrets.token_urlsafe(24))
        if updated != 1:
            raise Conflict('A newer authored discovery action won. Reload.')
        state.refresh_from_db()
        return state


def authorize_defaults_operator(actor, profile_id, session_guard):
    """Apply only the reviewed owner packet under the fixed operator session guard.

    The caller owns validation of the deployed operator session and its expiry.
    No browser session is manufactured here. Database authority is independently
    rechecked around the complete-state CAS; this service cannot author free text.
    """
    from django.contrib.auth import get_user_model
    from shelf.provider_views import _profiles

    def authorized():
        session_guard()
        if actor.pk != 1 or profile_id != 1:
            raise ValueError('The authored seed operator is limited to the owner.')
        current = get_user_model().objects.filter(pk=1, username='demo-curator', is_active=True).first()
        if current is None or not _profiles(current, 'select').filter(pk=profile_id).exists():
            raise ValueError('Current owner selection authority is required.')

    digest = revision(DEFAULT_SEEDS)
    receipt_key = 'operator-defaults:' + digest
    with transaction.atomic():
        authorized()
        state = AuthoredDiscoveryState.objects.filter(actor_id=1, profile_id=1, mode='household').first()
        if state is None:
            state = AuthoredDiscoveryState.objects.create(actor_id=1, profile_id=1,
                mode='household', nonce=secrets.token_urlsafe(24), data=empty_data())
        data = deepcopy(state.data)
        for supplied in DEFAULT_SEEDS:
            existing = next((row for row in data['seeds'] if row['id'] == supplied['id']), None)
            if existing and any(existing[key] != supplied[key] for key in supplied):
                raise Conflict('Existing authored seed differs from the reviewed owner packet.')
        # An owner's later removal is also respected when the same operator
        # packet is replayed. A new authoring intent requires a separate action.
        if receipt_key in state.receipts:
            authorized()
            return state
        if len(state.receipts) >= MAX_RECEIPTS:
            raise Conflict('Local authored discovery request limit reached.')
        for supplied in DEFAULT_SEEDS:
            if any(row['id'] == supplied['id'] for row in data['seeds']):
                continue
            if len(data['seeds']) >= MAX_SEEDS:
                raise Conflict('The 30-topic seed limit is reached.')
            seed = seed_record(supplied, 1, timezone.now().isoformat())
            seed.update(captured_by='operator_for_owner:1', captured_actor='operator_for_owner:1')
            data['seeds'].append(seed)
        authorized()
        receipts = dict(state.receipts, **{receipt_key: digest})
        updated = AuthoredDiscoveryState.objects.filter(pk=state.pk, revision=state.revision, nonce=state.nonce).update(
            data=data, receipts=receipts, revision=state.revision + 1, nonce=secrets.token_urlsafe(24))
        if updated != 1:
            raise Conflict('A newer authored discovery action won. Reload.')
        authorized()
        state.refresh_from_db()
        return state
