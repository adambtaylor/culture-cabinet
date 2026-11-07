"""Owned social source selection, one-shot reads and explicit review/import.

Complete-state compare-and-swap follows DiscoveryState. A read is reserved before
calling the adapter and accepted only against that exact revision and authority.
There is no scheduler, worker, account session, private API or automatic retry.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import secrets

from django.core import signing
from django.db import connection, transaction
from django.utils import timezone

from shelf import social_sources as sources, social_event_intake as event_intake
from shelf.discovery_state import Conflict, public_authorized
from shelf.models import SocialDiscoveryState

MAX_RECEIPTS = 500
CLEANUP_ACTIONS = frozenset(('remove_source', 'discard', 'withdraw', 'cancel', 'clear_tasks', 'event_withdraw'))


def available():
    return SocialDiscoveryState._meta.db_table in connection.introspection.table_names()


def initial_nonce(actor_id, profile_id, mode):
    return signing.Signer(salt='social-discovery-start').sign_object([actor_id, profile_id, mode])


def owned(request, profile_id, mode):
    public_authorized(request, profile_id)
    return SocialDiscoveryState.objects.filter(actor=request.user, profile_id=profile_id, mode=mode).first()


def empty_data():
    return {'sources': [], 'tasks': [], 'observations': []}


def _save(state, data, receipts):
    updated = SocialDiscoveryState.objects.filter(pk=state.pk, revision=state.revision, nonce=state.nonce).update(
        data=data, receipts=receipts, revision=state.revision + 1, nonce=secrets.token_urlsafe(24))
    if updated != 1:
        raise Conflict('A newer social discovery action won. Reload the page.')
    state.refresh_from_db()
    return state


def _find(rows, identity):
    result = next((row for row in rows if row['id'] == identity), None)
    if result is None:
        raise Conflict('This selected source, task or lead is no longer available.')
    return result


def _add_observations(data, incoming):
    by_id = {row['id']: row for row in data['observations']}
    for row in incoming:
        # A repeated capture may change its claims. Always require review again;
        # a new read must never silently replace already-imported evidence.
        previous = by_id.get(row['id'])
        if previous and previous['review'] == 'imported':
            continue
        by_id[row['id']] = row
    if len(by_id) > sources.MAX_INBOX:
        raise Conflict('The 60-lead inbox is full. Discard leads before another read.')
    data['observations'] = list(by_id.values())


def apply(request, profile_id, fields):
    mode, action, identity = fields['mode'], fields['action'], fields.get('identity', '')
    if mode not in ('household', 'sample'):
        raise sources.SourceError('Invalid mode.')
    digest = sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    read = None
    now = timezone.now()
    with transaction.atomic():
        public_authorized(request, profile_id)
        state = owned(request, profile_id, mode)
        if state is None:
            if fields['revision'] != 0 or fields['nonce'] != initial_nonce(request.user.pk, profile_id, mode):
                raise Conflict('Stale initial social form. Reload.')
            state = SocialDiscoveryState.objects.create(actor=request.user, profile_id=profile_id,
                mode=mode, nonce=fields['nonce'], data=empty_data())
        if fields['nonce'] in state.receipts:
            if state.receipts[fields['nonce']] != digest:
                raise Conflict('This form was already used for another action.')
            return state
        if fields['revision'] != state.revision or fields['nonce'] != state.nonce:
            raise Conflict('Stale social form. Reload before continuing.')
        if len(state.receipts) >= MAX_RECEIPTS and action not in CLEANUP_ACTIONS:
            raise Conflict('Local request limit reached. Cleanup and cancellation remain available.')
        data = deepcopy(state.data)
        if action in ('observe_event', 'event_withdraw'):
            permitted = set(event_intake.BOUNDS) | set(event_intake.ENUMS) | {'public_confirmed', 'action'}
            if action == 'event_withdraw':
                permitted = {'action', 'identity'}
            envelope = {key: value for key, value in fields.items() if key in permitted}
            data = event_intake.apply_event_action(data, envelope,
                captured_at=now.isoformat(), capture_actor='user:' + str(request.user.pk))
        elif action == 'add_source':
            source = sources.source_record(**{k: fields[k] for k in ('platform', 'url', 'label', 'kind', 'scope')})
            if not any(row['id'] == source['id'] for row in data['sources']):
                if len(data['sources']) >= sources.MAX_SOURCES:
                    raise Conflict('Choose at most 12 cultural sources. Remove one before adding another.')
                data['sources'].append(source)
        elif action == 'remove_source':
            _find(data['sources'], identity)
            data['sources'] = [row for row in data['sources'] if row['id'] != identity]
            data['tasks'] = [row for row in data['tasks'] if row['source_id'] != identity]
            data['observations'] = [row for row in data['observations'] if row['source_id'] != identity]
        elif action == 'queue':
            source = _find(data['sources'], identity)
            if len(data['tasks']) >= sources.MAX_TASKS:
                raise Conflict('The 30-task limit is reached. Clear completed tasks before another read.')
            if any(row['source_id'] == identity and row['status'] in ('queued', 'reading') for row in data['tasks']):
                raise Conflict('This source already has a queued or unfinished read. Cancel it before starting another.')
            data['tasks'].append({'id': secrets.token_hex(16), 'source_id': source['id'],
                'status': 'queued', 'queued_at': now.isoformat(), 'checked_at': '',
                'scenario': fields.get('scenario') or 'ready'})
        elif action == 'run':
            task = _find(data['tasks'], identity)
            source = _find(data['sources'], task['source_id'])
            if task['status'] != 'queued':
                raise Conflict('This task has already started or stopped. Queue a new read explicitly.')
            if not timedelta_valid(task['queued_at'], now, sources.TASK_TTL):
                task['status'] = 'expired'
            else:
                task.update(status='reading', checked_at=now.isoformat())
                read = (deepcopy(source), deepcopy(task))
        elif action == 'cancel':
            task = _find(data['tasks'], identity)
            if task['status'] not in ('queued', 'reading'):
                raise Conflict('This task is already stopped.')
            task['status'] = 'cancelled'
        elif action == 'clear_tasks':
            data['tasks'] = [row for row in data['tasks'] if row['status'] in ('queued', 'reading')]
        elif action == 'manual':
            source = _find(data['sources'], identity)
            item = {k: fields.get(k, '') for k in ('title', 'post_url', 'event_url', 'listed_when')}
            item.update(captured_at=now.isoformat(), published_at='')
            rows = sources.normalize_outcome(sources.BrowserOutcome('ready', source['url'], (item,)),
                source, now.isoformat(), now, provenance='fixture' if mode == 'sample' else 'manual-entry')
            _add_observations(data, rows)
        elif action in ('import', 'discard', 'withdraw'):
            row = _find(data['observations'], identity)
            if action == 'import':
                if not sources.fresh(row, now):
                    raise Conflict('This capture is older than 24 hours. Read or enter it again before importing.')
                row['review'] = 'imported'
            elif action == 'withdraw':
                row['review'] = 'pending'
            else:
                data['observations'].remove(row)
        else:
            raise sources.SourceError('Invalid social action.')
        public_authorized(request, profile_id)
        # Cleanup must remain possible at the cap. Its latest receipt replaces
        # an older replay record; any pruned form still fails the current
        # revision/nonce check and cannot repeat effects. Task evidence is separate.
        receipts = dict(list(state.receipts.items())[-(MAX_RECEIPTS - 1):])
        receipts[fields['nonce']] = digest
        _save(state, data, receipts)
    if read:
        _read(request, profile_id, state, *read)
    return state


def timedelta_valid(value, now, limit):
    at = sources.timestamp(value)
    return at is not None and 0 <= (now - at).total_seconds() < limit.total_seconds()


def _read(request, profile_id, reserved, source, task):
    request_data = sources.BrowserRead(source['url'], source['platform'], source['kind'], task['checked_at'])
    try:
        outcome = sources.adapter(reserved.mode, task['scenario']).read(request_data)
        rows = sources.normalize_outcome(outcome, source, task['checked_at'], timezone.now(),
            provenance='fixture' if reserved.mode == 'sample' else 'browser-observed')
        status = outcome.status
    except (OSError, TimeoutError, sources.SourceError):
        status, rows = 'unavailable', []
    with transaction.atomic():
        public_authorized(request, profile_id)
        if not SocialDiscoveryState.objects.filter(pk=reserved.pk, revision=reserved.revision,
                                                    nonce=reserved.nonce).exists():
            raise Conflict('The selected source or queue changed during the read. Results were not accepted.')
        data = deepcopy(reserved.data)
        current = _find(data['tasks'], task['id'])
        _add_observations(data, rows)
        current['status'] = status
        _save(reserved, data, reserved.receipts)


def dedup_key(url):
    """Comparison only; URLs were validated before persistence or provider parsing."""
    return (url or '').rstrip('/').replace('https://www.moshtix.com.au/', 'https://moshtix.com.au/')


@dataclass(frozen=True)
class ObservedEventCandidate(sources.SocialCandidate):
    def freshness(self, now):
        return 'Observed public event; current details and availability need confirmation.'


def reviewed_candidates(request, profile_id, existing):
    if not available():
        return []
    state = owned(request, profile_id, 'household')
    if not state:
        return []
    known = {dedup_key(card.url) for card in existing if card.url}
    selected = {row['id']: row for row in state.data['sources']}
    groups = {}
    for row in state.data['observations']:
        if row['review'] != 'imported' or not sources.fresh(row, timezone.now()) or row['source_id'] not in selected:
            continue
        url = row['event_url'] or row['post_url']
        key = dedup_key(url)
        if key in known:
            continue
        groups.setdefault(key, []).append(dict(row, source_label=selected[row['source_id']]['label']))
    cards = []
    for url, rows in groups.items():
        row = rows[0]
        cards.append(sources.SocialCandidate(
            key='official:event:' + sha256(('social\0' + url).encode()).hexdigest(),
            title=row['title'], kind='event', source='Reviewed social announcement',
            provenance=row['provenance'], rank=len(cards) + 1, retrieved_at=row['captured_at'],
            url=row['post_url'], category='all', social_evidence=tuple(rows)))
    # Connector identities suppress duplicate cards, while the complete original
    # observations and contradictions remain reviewable in social discovery.
    for row in event_intake.event_candidates(state.data, existing=tuple(existing) + tuple(cards)):
        evidence = tuple(dict(item, source_label=item.get('evidence_ref', ''),
            provenance='browser/manual public observation', listed_when=item.get('raw_date_time', ''),
            post_url=item.get('canonical_url', ''), event_url=item.get('canonical_url', ''))
            for item in row['social_evidence'])
        cards.append(ObservedEventCandidate(key=row['key'], title=row['title'], kind='event',
            source=row['source'], provenance=row['provenance'], rank=len(cards) + 1,
            retrieved_at=row['retrieved_at'], url=row['url'], category='all', social_evidence=evidence))
    return cards
