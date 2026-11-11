"""Authenticated local discovery. GET renders local data; POST records human intent."""
from functools import wraps
import hashlib
from dataclasses import replace, asdict
from urllib.parse import urlencode

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, connection, transaction
from django.core.cache.backends.locmem import LocMemCache
from django.http import Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from shelf import discovery_sources as sources, discovery_state as storage
from shelf.discovery_reducer import CHOICE_LABELS, replay, select
from shelf.models import DiscoveryState
from shelf.recovery import RecoveryBlocked
from shelf.provider_views import _profile
from shelf.authority import SELECTION_KEY
from shelf.feedback_scale import REACTION_LABELS, clean_note
from shelf.event_source_registry import (SOURCES as EVENT_SOURCES, LEGACY_ALIASES,
                                         source_cache_key, source_controls)

DECK_KEY = 'discovery_feedback_decks_v1'
QUEUES = (('all', 'Music & events'), ('artist', 'Music (Bands)'))

PUBLIC_CACHE = LocMemCache('discovery-public', {'TIMEOUT': 900, 'OPTIONS': {'MAX_ENTRIES': 128}})


class ActionForm(forms.Form):
    mode = forms.ChoiceField(choices=[(v, v) for v in ('household', 'sample')])
    revision = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=200)
    operation = forms.ChoiceField(choices=[(v, v) for v in ('respond', 'save', 'unsave', 'undo', 'restart', 'event_source', 'radio', 'events', 'venue303', 'forum', 'rrr', 'rrr_detail', 'beat', 'beat_detail', 'pbs', 'tribe', 'soundsystems', 'gallery')])
    source_id = forms.ChoiceField(required=False, choices=[(key, value.label) for key, value in EVENT_SOURCES.items()])
    key = forms.CharField(required=False, max_length=160)
    choice = forms.ChoiceField(required=False, choices=CHOICE_LABELS)
    queue = forms.ChoiceField(required=False, choices=QUEUES)
    radio_mode = forms.ChoiceField(required=False, choices=[(v, v) for v in ('easy', 'medium', 'hard')])
    note = forms.CharField(required=False, max_length=500, strip=False)

    def clean_note(self):
        try:
            return clean_note(self.cleaned_data['note'])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None

    def clean(self):
        data = super().clean()
        if 'queue' in self.data and not data.get('queue'):
            self.add_error('queue', 'Choose an available queue.')
        # Preserve the byte-for-byte historical receipt input when absent.
        for field in ('note', 'queue', 'source_id'):
            if field not in self.data:
                data.pop(field, None)
        if data.get('operation') == 'event_source':
            if not data.get('source_id'):
                self.add_error('source_id', 'Choose an available source.')
        elif data.get('source_id'):
            self.add_error('source_id', 'Unexpected source for this action.')
        return data


def guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            # The existing source restriction barrier spans rendering as well as
            # link projection, so a restriction cannot race the emitted link.
            profile_id = kwargs.get('profile_id', args[0] if args else None)
            with storage.reference_window(request, profile_id):
                return view(request, *args, **kwargs)
        except storage.Conflict:
            return _error(request, 'This response is stale. Reload before responding.', 409)
        except (OperationalError, IntegrityError):
            return _error(request, 'Local discovery storage unavailable or busy. Reload before retrying.', 503)
        except RecoveryBlocked:
            return _error(request, 'Saved event source policy is unavailable. Reload before retrying.', 503)
        except ValueError:
            return _error(request, 'Invalid discovery action. Reload the page.', 400)
    return call


def cache_key(request, profile_id):
    return f'{request.user.pk}:{profile_id}:{request.session.session_key}'


def public_entries(request, profile_id):
    now = timezone.now()
    return {key: value for key, value in PUBLIC_CACHE.get(cache_key(request, profile_id), {}).items()
            if 0 <= (now - value['at']).total_seconds() < 900}


def gallery_context(request, profile_id, now, *, mode='household'):
    """Shared cached offer/source projection; reading never fetches or records taste."""
    from shelf import gallery_curation, gallery_commerce
    if mode != 'household':
        return {'gallery_offers': [], 'gallery_source_checks': [], 'gallery_brief_note': ''}
    storage.public_authorized(request, profile_id)
    offers, checks = {}, []
    for entry in public_entries(request, profile_id).values():
        outcome = entry['outcome']
        for offer in getattr(outcome, 'offers', ()):
            if isinstance(offer, dict):
                offers[offer.get('offer_id', '')] = {
                    'offer': offer, 'cost_gate': asdict(gallery_commerce.evaluate_offer(offer, now=now)),
                    'why': gallery_curation.offer_reasons(offer, actor_id=request.user.pk, profile_id=profile_id)}
        checks.extend(getattr(outcome, 'source_checks', ()))
    quotes = gallery_curation.principle_quotes(actor_id=request.user.pk, profile_id=profile_id)
    return {'gallery_offers': list(offers.values()), 'gallery_source_checks': checks,
        'gallery_brief_note': ('Your authorized art brief informs these curatorial looking experiments. '
            'Quiet space, expressive imagery, landscape and unfamiliar practices remain open directions; '
            'these reasons are hypotheses, not ratings or favourites.') if quotes else ''}


def candidates(request, profile_id, mode, history):
    if mode == 'sample':
        return sources.sample_candidates()
    cards = {c.key: c for c in sources.household_candidates()}
    for entry in public_entries(request, profile_id).values():
        for card in entry['outcome'].candidates:
            old = cards.get(card.key)
            if old:
                card = replace(card, roots=tuple(dict.fromkeys(old.roots + card.roots)),
                               lineage=tuple(dict.fromkeys(old.lineage + card.lineage)))
            cards[card.key] = card
    from shelf.social_discovery import reviewed_candidates
    for card in reviewed_candidates(request, profile_id, list(cards.values())):
        cards[card.key] = card
    from shelf.event_saved_references import project_references, ReferenceCandidate
    with storage.reference_window(request, profile_id) as denied:
        references = project_references(history, mode=mode, descriptors=EVENT_SOURCES,
                                        denied_namespaces=denied)
    # Retain only previously judged identity/provenance after public content expires.
    for event in history:
        key, source = event['key'], event.get('source', {})
        if key and key.split(':')[1] in ('event','artist','recording','venue') and key not in cards and source.get('provenance') in ('live-automated', 'browser-observed', 'manual-entry'):
            candidate_type = ReferenceCandidate if key in references else sources.Candidate
            extra = {'saved_reference': references[key]} if key in references else {}
            cards[key] = candidate_type(key, 'Saved event reference — source details expired'
                if key in references else 'Previously judged identity (source details expired)',
                key.split(':')[1], source['name'], source['provenance'], source['rank'],
                source['retrieved_at'], tuple(source.get('roots', [])),
                artist_key=source.get('artist_key'), venue_key=source.get('venue_key'),
                lineage=tuple(tuple(item) for item in source.get('lineage', [])), **extra)
    return list(cards.values())


def _read_event_detail(request, profile_id, data):
    from shelf.rrr_events import RRREvent
    from shelf.rrr_details import fetch_detail as rrr_detail
    from shelf.beat_events import BeatEvent, fetch_detail as beat_detail
    source, kind, fetch_detail = ('beat', BeatEvent, beat_detail) if data['operation'] == 'beat_detail' else ('rrr', RRREvent, rrr_detail)
    if data['mode'] != 'household' or data['choice'] or data['radio_mode'] or not data['key']:
        raise ValueError('Invalid detail form.')
    entry_key = (source, '', '')
    entry = public_entries(request, profile_id).get(entry_key)
    event = next((c for c in entry['outcome'].candidates if c.key == data['key']), None) if entry else None
    if type(event) is not kind:
        raise storage.Conflict('Read the source listing again before requesting this detail.')
    reserved = storage.reserve_public(request, profile_id, data)
    if reserved is None:
        return
    result = fetch_detail(event, timezone.now())
    with transaction.atomic():
        storage.check_public_result(request, profile_id, reserved)
        entries = public_entries(request, profile_id)
        current = entries.get(entry_key)
        if current is None or current['at'] != entry['at']:
            raise storage.Conflict('The listing expired or changed during this read; reload.')
        cards = tuple(result if c.key == event.key else c for c in current['outcome'].candidates)
        # Preserve the listing's original expiry and source evidence. A detail
        # read must never keep old listing content alive for another 15 minutes.
        current['outcome'] = replace(current['outcome'], candidates=cards)
        PUBLIC_CACHE.set(cache_key(request, profile_id), entries, timeout=900)


@login_required
@require_GET
def home(request):
    target = request.session.get(SELECTION_KEY)
    if target is None:
        return redirect('profiles')
    return index(request, target)


def _partial(request):
    return ('application/json' in request.headers.get('Accept', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest')


def _error(request, message, status):
    if _partial(request):
        return JsonResponse({'ok': False, 'message': message}, status=status)
    return HttpResponse(message, status=status, content_type='text/plain')


def _scope(request, profile_id, mode, queue):
    return f'{request.user.pk}:{profile_id}:{mode}:{queue}'


def _deck(request, profile_id, mode, queue):
    return request.session.get(DECK_KEY, {}).get(_scope(request, profile_id, mode, queue))


def _store_deck(request, profile_id, mode, queue, deck):
    decks = dict(request.session.get(DECK_KEY, {}))
    # Two modes and two queues for the selected owner, never posted identities.
    decks = {key: value for key, value in decks.items()
             if key.startswith(f'{request.user.pk}:{profile_id}:')}
    decks[_scope(request, profile_id, mode, queue)] = deck
    request.session[DECK_KEY] = decks


def _pool(cards, queue):
    return [card for card in cards if queue == 'all' or card.kind == 'artist']


def _pool_digest(cards):
    return hashlib.sha256('\n'.join(sorted({card.key for card in cards})).encode()).hexdigest()


def _rank(cards, state, category):
    from shelf.event_saved_references import ReferenceCandidate
    # A saved bookmark cannot become a fresh recommendation after cache loss.
    return select([card for card in cards if not isinstance(card, ReferenceCandidate)],
                  state, category=category, unjudged_only=True)


def _replace_key(deck, cards, state, submitted):
    """Pin other server-owned slots; only rank the submitted slot's successor."""
    registered = {card.key: card for card in cards}
    old = deck['keys'] + deck.get('source_keys', [])
    pinned = [key for key in old if key != submitted and key in registered and key not in state['choices']]
    origin = registered.get(submitted)
    # Existing music and event sections retain their own card family.
    eligible = [card for card in cards if card.key not in pinned and
                (origin is None or (card.kind in ('artist', 'recording')) ==
                 (origin.kind in ('artist', 'recording')))]
    ranked = _rank(eligible, state, deck['category'])['selection']
    replacement = ranked[0]['card'].key if ranked and submitted in old else ''
    for field in ('keys', 'source_keys'):
        deck[field] = [replacement if key == submitted else key for key in deck.get(field, [])
                       if key in pinned or key == submitted and replacement]
    return replacement


def _display(request, profile_id, card, state, now, why=()):
    from shelf.event_saved_references import ReferenceCandidate
    from shelf.gallery_curation import reasons
    why = tuple(why) + (reasons(card, actor_id=request.user.pk, profile_id=profile_id)
                        if card.provenance != 'fixture' else ())
    date_status = card.date_status(now)
    notice = getattr(card, 'source_notice', '')
    if notice:
        date_status += f' Source notice: {notice} (not a current seller check).'
    reference_only = isinstance(card, ReferenceCandidate)
    return {'card': card, 'why': why, 'freshness': card.freshness(now),
            'reference_only': reference_only,
            'saved_reference': card.saved_reference if reference_only else None,
            'date_status': date_status, 'choice': state['choices'].get(card.key, ''),
            'note': state.get('notes', {}).get(card.key, ''), 'saved': card.key in state['saved']}


def _context(request, profile, mode, queue, category, stored, cards):
    from shelf import for_you
    state = replay(stored.history if stored else [])
    available = DiscoveryState._meta.db_table in connection.introspection.table_names()
    revision = stored.revision if stored else 0
    pool = _pool(cards, queue)
    ranked = _rank(pool, state, category)
    deck = _deck(request, profile.pk, mode, queue)
    registered = {card.key: card for card in pool}
    pool_digest = _pool_digest(pool)
    if not deck or deck['category'] != category or deck.get('pool_digest') != pool_digest:
        deck = {'keys': [row['card'].key for row in ranked['selection']], 'category': category, 'revision': revision}
        if mode == 'household' and queue == 'all' and category in ('all', 'gallery', 'art_fair'):
            from shelf.gallery_curation import select as gallery_selection
            for card in gallery_selection((c for c in pool if c.key not in state['choices']
                    and (category == 'all' or c.category == category)),
                    actor_id=request.user.pk, profile_id=profile.pk, limit=2):
                if card.key not in deck['keys']:
                    deck['keys'].append(card.key)
    elif deck['revision'] != revision:
        last = stored.history[-1] if stored and stored.history else {}
        if deck['revision'] + 1 == revision and last.get('operation') == 'respond':
            replacement = _replace_key(deck, pool, state, last['key'])
            deck['receipt'] = {'revision': revision, 'submitted_key': last['key'], 'replacement_key': replacement}
        else:
            deck = {'keys': [row['card'].key for row in ranked['selection']], 'category': category}
        deck['revision'] = revision
    deck['pool_digest'] = pool_digest
    deck['keys'] = [key for key in deck['keys'] if key in registered and key not in state['choices']]
    _store_deck(request, profile.pk, mode, queue, deck)
    now = timezone.now()
    reasons = {row['card'].key: row['why'] for row in ranked['selection']}
    selected = [_display(request, profile.pk, registered[key], state, now, reasons.get(key, ())) for key in deck['keys']]
    current_event_keys = {card.key for entry in public_entries(request, profile.pk).values()
        for card in entry['outcome'].candidates if card.kind == 'event'} if mode == 'household' and queue == 'all' else set()
    if 'source_keys' not in deck:
        deck['source_keys'] = [card.key for card in pool if card.key in current_event_keys
            and card.key not in deck['keys'] and card.key not in state['choices']
            and (category == 'all' or card.category == category)]
    deck['source_keys'] = [key for key in deck['source_keys'] if key in registered and key not in state['choices']]
    _store_deck(request, profile.pk, mode, queue, deck)
    more = [_display(request, profile.pk, registered[key], state, now) for key in deck['source_keys']]
    responses = [_display(request, profile.pk, card, state, now) for card in pool
                 if card.key in state['choices'] or card.key in state['saved']]
    for_you.discovery(request, profile.pk, selected + more + responses, mode=mode)
    enhancement = {}
    if available and mode == 'household' and queue == 'artist':
        from shelf.pick_enhancement import context
        from shelf.acquisition_contract import Blocked
        from shelf.models import SynthesisRun
        if SynthesisRun._meta.db_table not in connection.introspection.table_names():
            enhancement = {'enhancement': {'state': 'blocked', 'label': 'Enhancement unavailable',
                'explanation': 'Supervised enhancement requires explicit local preparation.'}}
        else:
            try:
                enhancement = context(request, profile.pk, 'artist')
            except (Blocked, storage.Conflict):
                enhancement = {'enhancement': {'state': 'blocked', 'label': 'Enhancement needs current taste evidence',
                    'explanation': 'Record a household response before requesting supervised enhancement.'}}
    return {
        **gallery_context(request, profile.pk, now, mode=mode), **enhancement,
        'selected_profile': profile, 'mode': mode, 'category': category, 'queue': queue,
        'queue_options': QUEUES,
        'categories': [('all', 'All events'), ('music', 'Live music'), ('gallery', 'Gallery openings'),
                       ('art_fair', 'Affordable art fairs / sales'), ('community', 'Community / festivals'),
                       ('regional_day_trip', 'Regional winery / day-trip events')],
        'music': [row for row in selected if row['card'].kind in ('artist', 'recording')],
        'events': [row for row in selected if row['card'].kind in ('venue', 'event')],
        'more_source_events': more, 'responses': responses,
        'lanes': ranked['lanes'], 'sources': sources.source_statuses(), 'registry': sources.registry(),
        'event_sources': source_controls(),
        'choices': CHOICE_LABELS, 'choice_options': CHOICE_LABELS, 'reaction_options': REACTION_LABELS,
        'can_undo': state['can_undo'], 'available': available, 'revision': revision,
        'feedback_enabled': available and mode == 'household',
        'nonce': stored.nonce if stored else storage.initial_nonce(request.user.pk, profile.pk, mode),
        'action_url': f'/profiles/{profile.pk}/discovery/action/',
        'public_results': list(public_entries(request, profile.pk).values()) if mode == 'household' and queue == 'all' else [],
        'temporary_notice': 'Public results expire from memory after 15 minutes. Only explicit human choices, judged IDs and minimal provenance are durable. Sample history stays separate.',
    }


def apply_enhancement_order(request, profile_id, ids):
    """Materialize a verified consumer order in the current private artist deck."""
    storage.public_authorized(request, profile_id)
    stored = storage.owned(request, profile_id, 'household')
    deck = _deck(request, profile_id, 'household', 'artist')
    if (not deck or type(ids) is not list or not ids or len(ids) > 30
            or any(type(key) is not str for key in ids) or len(set(ids)) != len(ids)
            or deck['revision'] != (stored.revision if stored else 0)):
        raise storage.Conflict('Artist queue changed; reload before applying this order.')
    state = replay(stored.history if stored else [])
    pool = _pool(candidates(request, profile_id, 'household', stored.history if stored else []), 'artist')
    if deck.get('pool_digest') != _pool_digest(pool):
        raise storage.Conflict('Registered artists changed; reload before applying this order.')
    registered = {card.key for card in pool if card.key not in state['choices']}
    if any(key not in registered for key in ids):
        raise storage.Conflict('Enhanced artist identity is no longer available.')
    storage.public_authorized(request, profile_id)
    latest = storage.owned(request, profile_id, 'household')
    if (latest.revision if latest else 0) != deck['revision']:
        raise storage.Conflict('Artist feedback changed during application.')
    deck['keys'] = list(ids)
    deck.pop('receipt', None)
    _store_deck(request, profile_id, 'household', 'artist', deck)
    return True


@login_required
@require_GET
@guard
def index(request, profile_id):
    profile = _profile(request, profile_id)
    mode = request.GET.get('mode', 'household')
    category = request.GET.get('category', 'all')
    queue = request.GET.get('queue', 'all')
    if (mode not in ('household', 'sample') or category not in sources.CATEGORIES or queue not in dict(QUEUES)
            or any(k not in ('mode', 'category', 'queue') or len(request.GET.getlist(k)) != 1 for k in request.GET)):
        raise Http404
    available = DiscoveryState._meta.db_table in connection.introspection.table_names()
    stored = storage.owned(request, profile_id, mode) if available else None
    cards = candidates(request, profile_id, mode, stored.history if stored else [])
    return render(request, 'cabinet/discovery.html', _context(request, profile, mode, queue, category, stored, cards))


@login_required
@require_POST
@guard
def action(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    if (int(request.META.get('CONTENT_LENGTH') or 0) > 16384
            or any(k not in set(ActionForm.base_fields) | {'csrfmiddlewaretoken'}
                   or len(request.POST.getlist(k)) != 1 for k in request.POST)):
        return _error(request, 'Invalid discovery form.', 400)
    form = ActionForm(request.POST)
    if not form.is_valid():
        return _error(request, 'Invalid discovery form.', 400)
    data = form.cleaned_data
    queue = data.get('queue') or 'all'
    if _partial(request) and data['operation'] != 'respond':
        raise ValueError('Only explicit responses use partial requests.')
    public = data['operation'] in ('event_source', 'radio', 'events', 'venue303', 'forum', 'rrr', 'rrr_detail', 'beat', 'beat_detail', 'pbs', 'tribe', 'soundsystems', 'gallery')
    if ('note' in data and data['operation'] != 'respond') or public and queue != 'all':
        raise ValueError('Invalid source response fields.')
    if data['operation'] in ('rrr_detail', 'beat_detail'):
        _read_event_detail(request, profile_id, data)
    elif data['operation'] in ('event_source', 'radio', 'events', 'venue303', 'forum', 'rrr', 'beat', 'pbs', 'tribe', 'soundsystems', 'gallery'):
        seeds = {c.key: c for c in sources.household_candidates()
                 if c.kind == 'artist' and c.key.startswith('musicbrainz:artist:')}
        if (data['mode'] != 'household' or data['choice']
                or (data['operation'] == 'radio' and (data['key'] not in seeds or not data['radio_mode']))
                or (data['operation'] in ('event_source', 'events', 'venue303', 'forum', 'rrr', 'beat', 'pbs', 'tribe', 'soundsystems', 'gallery') and (data['key'] or data['radio_mode']))):
            return _error(request, 'Invalid public source form.', 400)
        reserved = storage.reserve_public(request, profile_id, data)
        if reserved is not None:
            seed = data['key'].split(':')[-1] if data['key'] else ''
            source_id = data.get('source_id') if data['operation'] == 'event_source' else LEGACY_ALIASES.get(data['operation'])
            if data['operation'] == 'event_source':
                result = sources.fetch_public('event_source', seed, data['radio_mode'], timezone.now(), source_id=source_id)
            else:
                result = sources.fetch_public(data['operation'], seed, data['radio_mode'], timezone.now())
            with transaction.atomic():
                storage.check_public_result(request, profile_id, reserved)
                entries = public_entries(request, profile_id)
                # Canonicalize aliases so a failed refresh through either form
                # replaces the same source result, without erasing other sources.
                entry_key = source_cache_key(source_id) if source_id else (data['operation'], seed, data['radio_mode'])
                source_label = EVENT_SOURCES[source_id].label if source_id else {
                    'radio': 'ListenBrainz artist radio', 'beat': 'Beat Magazine',
                    'tribe': 'Tribe Soundsystem RSS', 'soundsystems': 'Sound Systems of Melbourne',
                    'gallery': 'Gertrude / Australian Print Workshop'}[data['operation']]
                entries[entry_key] = {
                    'outcome': result, 'at': timezone.now(),
                    'source': source_label}
                PUBLIC_CACHE.set(cache_key(request, profile_id), entries, timeout=900)
    else:
        if data['radio_mode']:
            return _error(request, 'Invalid discovery form.', 400)
        stored = storage.owned(request, profile_id, data['mode'])
        cards = candidates(request, profile_id, data['mode'], stored.history if stored else [])
        deck = _deck(request, profile_id, data['mode'], queue)
        state = replay(stored.history if stored else [])
        prior = bool(stored and data['nonce'] in stored.receipts)
        registered = {card.key for card in _pool(cards, queue)}
        if data['operation'] in ('respond', 'save', 'unsave') and data['key'] not in registered:
            raise ValueError('Identity is outside this queue.')
        if _partial(request):
            if data['mode'] == 'household':
                storage.public_authorized(request, profile_id)
            if prior and stored.revision != data['revision'] + 1:
                raise storage.Conflict('A newer response superseded this receipt.')
            if not deck:
                raise storage.Conflict('Reload the queue before responding.')
            if deck.get('pool_digest') != _pool_digest(_pool(cards, queue)):
                raise storage.Conflict('Registered picks changed; reload before responding.')
            if not prior and deck['revision'] != data['revision']:
                raise storage.Conflict('The visible queue changed.')
            if (prior and deck['revision'] == stored.revision
                    and deck.get('receipt', {}).get('submitted_key') != data['key']):
                raise storage.Conflict('The visible queue superseded this receipt.')
            if not prior and data['key'] not in state['choices'] and data['key'] not in deck['keys'] + deck.get('source_keys', []):
                raise ValueError('Identity is not in the visible queue.')
        previous = replay(stored.history[:-1]) if prior else state
        operation = 'correct' if data['key'] in previous['choices'] else 'choose'
        stored = storage.commit(request, profile_id, data['mode'], data, cards)
        if data['operation'] == 'respond' and deck:
            state = replay(stored.history)
            receipt = deck.get('receipt', {})
            if prior and deck['revision'] == stored.revision and receipt.get('submitted_key') == data['key']:
                replacement = receipt.get('replacement_key', '')
            else:
                replacement = _replace_key(deck, _pool(cards, queue), state, data['key'])
                deck['revision'] = stored.revision
                deck['receipt'] = {'revision': stored.revision, 'submitted_key': data['key'], 'replacement_key': replacement}
            _store_deck(request, profile_id, data['mode'], queue, deck)
            if _partial(request):
                context = _context(request, profile, data['mode'], queue, deck['category'], stored, cards)
                rows = context['music'] + context['events'] + context['more_source_events']
                row = next((row for row in rows if row['card'].key == replacement), None)
                html = render_to_string('cabinet/discovery_card.html', {**context, 'row': row}, request=request) if row else ''
                if data['mode'] == 'household':
                    storage.public_authorized(request, profile_id)
                return JsonResponse({'enhance_context': {key: context[key] for key in ('enhance_digest', 'enhance_token', 'enhance_url', 'active_queue', 'enhancement') if key in context},
                    'ok': True, 'operation': operation, 'submitted_key': data['key'],
                    'replacement_html': html, 'revision': stored.revision, 'nonce': stored.nonce,
                    'queue': queue, 'mode': data['mode'],
                    'status_message': 'Response updated.' if operation == 'correct' else 'Response saved.' if row else 'Response saved. No more cached picks in this queue.',
                    'explanation_updates': {row['card'].key: row['for_you'] for row in rows}})
    query = urlencode({'mode': data['mode'], 'queue': queue})
    anchor = '#catalogue-card-' + data['key'] if data.get('key') else ''
    return redirect(f'/profiles/{profile_id}/discovery/?{query}{anchor}')
