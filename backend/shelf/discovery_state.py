"""Actor/profile/mode-owned local human history, separate from old intake/saves."""
import hashlib
import json
import secrets

from django.core import signing
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from shelf.discovery_reducer import CHOICES, MAX_HISTORY, replay
from shelf.feedback_scale import clean_note, weight
from shelf.models import DiscoveryState
from shelf.provider_views import _profile


class Conflict(ValueError):
    pass


def initial_nonce(actor_id, profile_id, mode):
    return signing.Signer(salt='music-events-start').sign_object([actor_id, profile_id, mode])


def owned(request, profile_id, mode):
    _profile(request, profile_id)
    return DiscoveryState.objects.filter(actor=request.user, profile_id=profile_id, mode=mode).first()


def commit(request, profile_id, mode, data, cards):
    """Compare-and-swap after current grant validation; replay identical requests.

    Receipts include the request digest so reusing a nonce for a different action
    conflicts. Sample mode never has an import/promotion path into household mode.
    """
    if mode not in ('sample', 'household'):
        raise Http404
    operation, key, choice = (data.get(k, '') for k in ('operation', 'key', 'choice'))
    if operation not in ('respond', 'save', 'unsave', 'undo', 'restart'):
        raise ValueError('Invalid operation.')
    card = next((c for c in cards if c.key == key), None)
    if operation in ('respond', 'save', 'unsave'):
        if card is None or (mode == 'sample') != (card.provenance == 'fixture'):
            raise ValueError('Identity is not available in this mode.')
    elif key or choice:
        raise ValueError('Unexpected choice fields.')
    if (operation == 'respond' and choice not in CHOICES) or (operation != 'respond' and choice):
        raise ValueError('Invalid choice.')
    if 'note' in data:
        if operation != 'respond':
            raise ValueError('Notes require an explicit response.')
        clean_note(data['note'])
    nonce = data['nonce']
    digest = hashlib.sha256(json.dumps(dict(data), sort_keys=True).encode()).hexdigest()
    from shelf.grant_authority import writer_window
    with transaction.atomic(), writer_window(request.user, profile_id):
        _profile(request, profile_id, 'select')
        state = owned(request, profile_id, mode)
        if state is None:
            if data['revision'] != 0 or nonce != initial_nonce(request.user.pk, profile_id, mode):
                raise Conflict('Stale initial form.')
            state = DiscoveryState.objects.create(actor=request.user, profile_id=profile_id,
                                                   mode=mode, nonce=nonce)
        if nonce in state.receipts:
            if state.receipts[nonce] != digest:
                raise Conflict('Nonce already used for another action.')
            return state
        if data['revision'] != state.revision or nonce != state.nonce:
            raise Conflict('Stale form. Reload before responding.')
        if len(state.history) >= MAX_HISTORY:
            raise Conflict('Local history limit reached; no response recorded.')
        if operation == 'undo' and not replay(state.history)['can_undo']:
            raise Conflict('Nothing to undo.')
        event = {'operation': operation, 'key': key, 'choice': choice,
                 'at': timezone.now().isoformat(), 'origin': 'authenticated-human-post',
                 'mode': mode}
        if 'note' in data:
            event['note'] = data['note']
        if card:
            if operation == 'respond' and weight(choice) != 0:
                event['card'] = {'key': card.key, 'title': card.title,
                                 'medium': 'event' if card.kind == 'event' else 'music', 'source': card.source,
                                 'url': card.url, 'provenance': card.provenance,
                                 'lane': ('venue:' + card.venue_key) if card.venue_key else card.source + ':' + card.category,
                                 'roots': list(card.roots),
                                 'fetched_at': card.retrieved_at}
            event['source'] = {'name': card.source, 'provenance': card.provenance,
                               'rank': card.rank, 'retrieved_at': card.retrieved_at}
            if card.provenance == 'live-automated':
                event['source'].update(roots=list(card.roots), lineage=list(card.lineage),
                                       artist_key=card.artist_key, venue_key=card.venue_key)
        history = state.history + [event]
        receipts = dict(state.receipts, **{nonce: digest})
        _profile(request, profile_id, 'select')
        updated = DiscoveryState.objects.filter(pk=state.pk, revision=state.revision, nonce=nonce).update(
            history=history, receipts=receipts, revision=state.revision + 1,
            nonce=secrets.token_urlsafe(24))
        if updated != 1:
            raise Conflict('Concurrent response; reload.')
        state.refresh_from_db()
        return state


def public_authorized(request, profile_id):
    """Recheck the database actor/grant and persisted current session selection."""
    from shelf.authority import SELECTION_KEY
    _profile(request, profile_id, 'select')
    current = request.session.__class__(session_key=request.session.session_key)
    if current.get(SELECTION_KEY) != profile_id:
        raise Http404


def reserve_public(request, profile_id, data):
    """Consume a nonce before I/O, without adding a human choice/history event."""
    if data['mode'] != 'household' or data['operation'] not in ('event_source', 'radio', 'events', 'venue303', 'forum', 'rrr', 'rrr_detail', 'beat', 'beat_detail', 'pbs', 'tribe', 'soundsystems', 'gallery'):
        raise ValueError('Invalid public operation.')
    if data['operation'] == 'event_source':
        from shelf.event_source_registry import get_source
        get_source(data.get('source_id'))
        if data.get('key') or data.get('radio_mode') or data.get('choice'):
            raise ValueError('Unexpected event source fields.')
    elif data.get('source_id'):
        raise ValueError('Unexpected event source ID.')
    nonce = data['nonce']
    digest = hashlib.sha256(json.dumps(dict(data), sort_keys=True).encode()).hexdigest()
    from shelf.grant_authority import writer_window
    with transaction.atomic(), writer_window(request.user, profile_id):
        public_authorized(request, profile_id)
        state = owned(request, profile_id, 'household')
        if state is None:
            if data['revision'] != 0 or nonce != initial_nonce(request.user.pk, profile_id, 'household'):
                raise Conflict('Stale initial form.')
            state = DiscoveryState.objects.create(actor=request.user, profile_id=profile_id,
                                                   mode='household', nonce=nonce)
        if nonce in state.receipts:
            if state.receipts[nonce] != digest:
                raise Conflict('Nonce already used for another action.')
            return None
        if data['revision'] != state.revision or nonce != state.nonce:
            raise Conflict('Stale form. Reload before requesting a source.')
        if len(state.receipts) >= 1000:
            raise Conflict('Local request receipt limit reached.')
        updated = DiscoveryState.objects.filter(pk=state.pk, revision=state.revision, nonce=nonce).update(
            receipts=dict(state.receipts, **{nonce: digest}), revision=state.revision + 1,
            nonce=secrets.token_urlsafe(24))
        if updated != 1:
            raise Conflict('Concurrent action; reload.')
        state.refresh_from_db()
        return state


def check_public_result(request, profile_id, reserved):
    public_authorized(request, profile_id)
    if not DiscoveryState.objects.filter(pk=reserved.pk, actor=request.user, profile_id=profile_id,
                                         revision=reserved.revision, nonce=reserved.nonce).exists():
        raise Conflict('A newer action superseded this source result; reload.')
