"""Provider-only UI contract; all provider I/O requires an explicit human POST."""
from functools import wraps
from copy import deepcopy
from urllib.parse import urlencode
import time

from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django.http import Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import book_metadata, intake, durable_intake as durable, provider_catalogue as catalogue
from shelf.authority import SELECTION_KEY
from shelf.models import Profile
from shelf.feedback_scale import REACTION_LABELS, ALL_LABELS, clean_note

QUEUE_OPTIONS = (('all', 'All'), ('book', 'Books'), ('movie', 'Movies'), ('tv', 'Series'))


class ActionForm(forms.Form):
    action = forms.ChoiceField(choices=[(s, s) for s in
        ('start', 'search', 'browse', 'add', 'choose', 'neighbors', 'reset', 'continue', 'resume', 'new_run', 'undo', 'correct', 'export', 'refresh')])
    run_id = forms.UUIDField(required=False)
    revision = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=64)
    medium = forms.ChoiceField(required=False, choices=[(s, s) for s in ('movie', 'tv', 'book')])
    query = forms.CharField(required=False, max_length=100, strip=True)
    book_category = forms.ChoiceField(required=False, choices=[('graphic_novels', 'Graphic novels')])
    page = forms.IntegerField(required=False, min_value=1, max_value=catalogue.BOOK_MAX_PAGES)
    candidate_id = forms.CharField(required=False, max_length=100)
    choice = forms.ChoiceField(required=False, choices=[(s, s) for s in intake.CHOICES])

    queue = forms.ChoiceField(required=False, choices=QUEUE_OPTIONS)
    note = forms.CharField(required=False, max_length=500, strip=False)

    def clean_note(self):
        try:
            return clean_note(self.cleaned_data['note'])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None

    def clean(self):
        data = super().clean()
        if 'queue' in self.data and not self.data.get('queue'):
            self.add_error('queue', 'Choose a supported category.')
        required = {'search': ('medium', 'query'), 'browse': ('book_category',), 'add': ('candidate_id',),
                    'choose': ('candidate_id', 'choice'), 'correct': ('candidate_id', 'choice'), 'neighbors': ('candidate_id',)}
        for field in required.get(data.get('action'), ()):
            if not data.get(field):
                self.add_error(field, 'Required for this action.')
        if 'note' not in self.data:
            data.pop('note', None)
        elif data.get('action') not in ('choose', 'correct'):
            self.add_error('note', 'Notes belong to a response.')
        return data


def _partial(request):
    return ('application/json' in request.headers.get('Accept', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest')


def _queue(request):
    values = request.GET.getlist('queue')
    if len(values) > 1 or values and values[0] not in dict(QUEUE_OPTIONS):
        raise intake.IntakeError()
    return values[0] if values else 'all'


def _catalogue_redirect(profile, queue='all', key=''):
    url = reverse('profile_catalogue', kwargs={'profile_id': profile.pk})
    url += '?' + urlencode({'queue': queue})
    return redirect(url + ('#catalogue-card-' + key if key else ''))


def remember_current(state):
    """Cache only this private session's trusted deck, bound to its run revision."""
    queue = state.get('queue', 'all')
    state.setdefault('queue_decks', {})[queue] = {
        'run_id': state.get('run_id'), 'revision': state['revision'],
        'current': list(state['current']),
        'reasons': {key: state['reasons'].get(key, '') for key in state['current']},
    }


def _restore_queue(state, queue):
    deck = state.get('queue_decks', {}).get(queue)
    if (not deck or deck.get('revision') != state['revision']
            or deck.get('run_id') != state.get('run_id')):
        return False
    keys = deck['current']
    if len(set(keys)) != len(keys) or any(
            key not in state['queued'] or key not in state['candidates'] or key in state['choices']
            or queue != 'all' and state['candidates'][key]['medium'] != queue for key in keys):
        return False
    state['current'] = list(keys)
    state['reasons'] = {key: deck['reasons'].get(key, '') for key in keys}
    state['queue'] = queue
    return True


def _recover_queue(request, profile, state, data, queue):
    """Recover a committed card response whose session save was lost after a switch."""
    deck = state.get('queue_decks', {}).get(queue)
    if (not durable.available() or not deck or deck.get('revision') != data['revision']
            or deck.get('run_id') != state.get('run_id')
            or state['revision'] != data['revision'] + 1):
        return False
    prior = durable.replay(request.user, profile.pk, data['nonce'], _payload(data))
    if not prior:
        return False
    run, result = prior
    if str(run.pk) != state.get('run_id') or result['revision'] != state['revision'] or run.archived_at:
        return False
    submitted = data['candidate_id']
    keys = deck['current']
    if (data['action'] == 'choose' and submitted not in keys
            or len(set(keys)) != len(keys) or any(
                key not in state['queued'] or key not in state['candidates']
                or key in state['choices'] and key != submitted
                or queue != 'all' and state['candidates'][key]['medium'] != queue for key in keys)):
        return False
    before = dict(state, queue=queue, current=list(keys), reasons=dict(deck['reasons']))
    replacement = _pin_round(before, state, submitted)
    state['card_receipt'] = {'nonce': data['nonce'], 'replacement_key': replacement}
    return True


def _select_queue(state, queue):
    if state.get('queue', 'all') != queue:
        remember_current(state)
        if not _restore_queue(state, queue):
            intake.select_round_v2(state, category=queue)
            state['queue'] = queue
    else:
        state['queue'] = queue
    remember_current(state)
    return state


def _pin_round(before, state, submitted=''):
    """Keep trusted visible cards in place while ranking one unseen replacement."""
    queue = before.get('queue', 'all')
    current = before['current']
    pinned = [key for key in current if key != submitted and key in state['candidates']
              and key not in state['choices']]
    ranked = deepcopy(state)
    ranked['queued'] = [key for key in state['queued'] if key not in pinned]
    ranked.pop('added_key', None)
    intake.select_round_v2(ranked, category=queue)
    replacement = ranked['current'][0] if submitted in current and ranked['current'] else ''
    ordered = []
    for key in current:
        if key == submitted and replacement:
            ordered.append(replacement)
        elif key in pinned:
            ordered.append(key)
    state['current'] = ordered
    state['queue'] = queue
    state['reasons'] = {key: before.get('reasons', {}).get(key, state['reasons'].get(key, '')) for key in pinned}
    if replacement:
        state['reasons'][replacement] = ranked['reasons'][replacement]
    remember_current(state)
    return replacement


def _profiles(actor, action='view'):
    """Human intake uses existing owner view+select, independently of feedback.

    The original grant schema supports this per-actor/profile experiment;
    official feedback and export retain their separate, unchanged authority.
    """
    # Deliberately use only the existing framework user/profile/grant tables.
    # The synthetic feedback recovery barrier is NOT this runtime's authority.
    if not actor.is_authenticated or actor.pk != 1:
        return Profile.objects.none()
    filters = {'pk': 1, 'kind': Profile.Kind.OWNER, 'active': True,
               'grant__user_id': actor.pk, 'grant__user__is_active': True,
               'grant__can_view': True}
    filters['grant__can_' + action] = True
    return Profile.objects.filter(**filters)


def _profile(request, profile_id, action='view'):
    if request.session.get(SELECTION_KEY) != profile_id:
        raise Http404
    profile = _profiles(request.user, action).filter(pk=profile_id).first()
    if profile is None:
        request.session.pop(intake.SESSION_KEY, None)
        raise Http404
    return profile


def _guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except intake.IntakeError as error:
            if _partial(request):
                return JsonResponse({'ok': False, 'message': 'This response is stale. Reload to review the current catalogue.' if error.status == 409 else 'Review the response and try again.'}, status=error.status)
            return HttpResponse('Intake action is invalid or stale. Reload the catalogue.\n',
                                status=error.status, content_type='text/plain')
    return call


def _state(request, profile):
    state = intake.load(request.session, request.user.pk, profile.pk)
    if state is None:
        state = intake.new_state(request.user.pk, profile.pk)
        request.session[intake.SESSION_KEY] = state
    if durable.available() and _profiles(request.user, 'select').filter(pk=profile.pk).exists():
        before = deepcopy(state)
        state = durable.upgrade(request.user, profile.pk, state, request.session.session_key)
        if before.get('run_id') == state.get('run_id'):
            # Hydration recalculates scores; visible forms keep their server-owned order.
            removed = [key for key in before['current'] if key in state['choices']]
            replacement = _pin_round(before, state, removed[0] if len(removed) == 1 else '')
            if (len(removed) == 1 and state['revision'] == before['revision'] + 1):
                state['card_receipt'] = {'nonce': before['nonce'], 'replacement_key': replacement}
        request.session[intake.SESSION_KEY] = state
    return state


def _page_context(request, profile, state, **kwargs):
    context = _context(profile, state, **kwargs)
    from shelf import pick_enhancement
    from shelf.acquisition_contract import Blocked
    from shelf.discovery_state import Conflict
    try:
        context.update(pick_enhancement.context(request, profile.pk, state.get('queue', 'all')))
    except (Blocked, Conflict):
        context['enhancement'] = {'state': 'blocked', 'label': 'Enhancement needs review',
            'explanation': 'The current taste or cached source binding is unavailable. Review this page before requesting.'}
    return context


def _context(profile, state, *, form=None, message='', queue=None):
    from shelf.saved import catalogue_context
    saved_context = catalogue_context(profile.pk)
    weights = intake.lane_weights(state)
    cards = []
    for key in state['current']:
        card = dict(state['candidates'][key])
        card['selection_reason'] = state['reasons'].get(key, '')
        card['lane_weight'] = weights[card['lane']]
        card['save_url'] = saved_context['save_url']
        cards.append(card)
    from shelf import for_you
    cards = for_you.catalogue(profile, state, cards)
    roots = [dict(c, can_expand=True, provider_label='Hardcover' if c['provider'] == 'hardcover' else 'TMDB')
             for c in intake.positive_roots(state)]
    enabled = durable.available()
    run = durable.IntakeRun.objects.filter(pk=state['run_id']).first() if state.get('run_id') and enabled else None
    persistence = ('Human intake choices are stored locally across logins. Suggestions and saved hearts are separate. '
                   'New run archives this history; undo or correction preserves earlier responses.')
    results = [dict(state['candidates'][k], save_url=saved_context['save_url']) for k in state['search_results']]
    return {
        **saved_context,
        'durable_available': enabled, 'durable_active': bool(run),
        'persistence_notice': persistence if run else 'Durable intake requires explicit preparation.' if not enabled else 'Continue with durable local intake.',
        'current_run': run, 'run_id': str(run.pk) if run else '',
        'human_history': run.history if run else [],
        'human_choices': [dict(state['candidates'][key], choice=choice, note=state.get('notes', {}).get(key, '')) for key, choice in state['choices'].items()] if run else [],
        'undo_label': 'Remove latest response (history retained)',
        'intake_runs': list(durable.IntakeRun.objects.filter(actor_id=state['actor_id'], profile_id=profile.pk).values('id', 'revision', 'created_at', 'archived_at')) if enabled else [],
        'durable_url': f'/profiles/{profile.pk}/intake/',
        'durable_action_url': f'/profiles/{profile.pk}/intake/action/',
        'resume_available': bool(run), 'undo_available': bool(state['choices']) and bool(run),
        'selector_version': state.get('selector_version', 'prototype-v1'),
        'selected_profile': profile, 'feedback_enabled': True, 'cards': cards,
        'search_results': results, 'browse': state.get('browse'),
        'result_series': book_metadata.series_groups(results),
        'positive_roots': roots,
        'expanded_roots': state['expanded'],
        'choice_counts': intake.summary(state), 'response_count': len(state['choices']), 'choices': intake.CHOICES,
        'choice_options': ALL_LABELS, 'reaction_options': REACTION_LABELS,
        'queue': queue or state.get('queue', 'all'), 'queue_options': QUEUE_OPTIONS,
        'revision': state['revision'], 'nonce': state['nonce'],
        'expires_at': state['expires_at'], 'started': state['started'],
        'exhausted': state['started'] and not cards,
        'form': form or ActionForm(initial={'revision': state['revision'], 'nonce': state['nonce']}),
        'action_url': f'/profiles/{profile.pk}/catalogue/action/',
        'message': message, 'provider_failures': state['failures'],
        'temporary_notice': persistence if run else 'Experimental choices expire within one hour. Reset clears them. '
                            'They are not official ratings or enjoyment records; reset has no undo.',
        'identity_notice': 'Local owner credential binding only; provider identity is not verified.',
        'crossmedia_notice': 'Independent film, series and book lanes. No verified cross-media facet or adaptation links are available.',
        'neighbor_notice': 'TMDB recommendations and Hardcover cached similar books are provider neighbors, not a verified population co-like graph.',
        'tmdb_attribution': 'This product uses the TMDB API but is not endorsed or certified by TMDB.',
        'tmdb_url': 'https://www.themoviedb.org/', 'hardcover_url': 'https://hardcover.app/',
    }


class ProviderLogoutView(LogoutView):
    next_page = '/login/'
    http_method_names = ['post', 'options']


@login_required
@require_GET
def profiles(request):
    return render(request, 'cabinet/provider_profiles.html', {'profile_options': _profiles(request.user, 'select')})


@login_required
@require_POST
def select_profile(request):
    if request.POST.getlist('profile_id') != ['1']:
        raise Http404
    profile = _profiles(request.user, 'select').first()
    if profile is None:
        raise Http404
    # Preserve a still-owned prototype history before profile selection clears its deck.
    old = intake.load(request.session, request.user.pk, profile.pk)
    if (old and old.get('choices') and request.session.get(SELECTION_KEY) == profile.pk
            and durable.available()):
        durable.upgrade(request.user, profile.pk, old, request.session.session_key)
    request.session.pop(intake.SESSION_KEY, None)
    request.session[SELECTION_KEY] = profile.pk
    return redirect('catalogue')


@login_required
@require_GET
def catalogue_home(request):
    target = request.session.get(SELECTION_KEY)
    if target is None:
        return redirect('profiles')
    return profile_catalogue(request, target)


@login_required
@require_GET
@_guard
def profile_catalogue(request, profile_id):
    profile = _profile(request, profile_id)
    return render(request, 'cabinet/provider_catalogue.html', _page_context(request, profile, _catalogue_state(request, profile)))


def _catalogue_state(request, profile):
    queue = _queue(request)
    state = _select_queue(_state(request, profile), queue)
    request.session[intake.SESSION_KEY] = state
    return state


@login_required
@require_POST
@_guard
def action(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    if int(request.META.get('CONTENT_LENGTH') or 0) > 16384:
        raise intake.IntakeError()
    if any(len(request.POST.getlist(k)) != 1 for k in request.POST):
        raise intake.IntakeError()
    form = ActionForm(request.POST)
    state = _state(request, profile)
    if not form.is_valid():
        if _partial(request):
            return JsonResponse({'ok': False, 'message': 'Review the response and try again.'}, status=400)
        return render(request, 'cabinet/provider_catalogue.html',
                      _page_context(request, profile, state, form=form, message='Review the action form.'), status=400)
    data = form.cleaned_data
    operation = data['action']
    queue = data.get('queue') or 'all'
    if _partial(request) and operation not in ('choose', 'correct'):
        raise intake.IntakeError()
    if operation in ('choose', 'correct') and queue != state.get('queue', 'all'):
        # A category GET must not invalidate another same-revision visible form.
        # Work on a copy so a subsequent stale/forged POST cannot move the deck.
        restored = deepcopy(state)
        if not _restore_queue(restored, queue) and not _recover_queue(request, profile, restored, data, queue):
            raise intake.IntakeError(409)
        state = restored
    if durable.available() and operation in ('choose', 'neighbors', 'add') and not state.get('run_id'):
        before_upgrade = deepcopy(state)
        state = durable.upgrade(request.user, profile.pk, state, request.session.session_key, create=True)
        _pin_round(before_upgrade, state)
    if state.get('run_id') or operation in ('continue', 'resume', 'new_run', 'undo', 'correct', 'export', 'refresh'):
        return _durable_action(request, profile, state, data)
    intake.check_revision(state, str(data['revision']), data['nonce'])
    auth = {'actor_id': request.user.pk, 'profile_id': profile.pk}
    before = deepcopy(state)
    try:
        if operation == 'reset':
            state = intake.new_state(request.user.pk, profile.pk)
        elif operation == 'start':
            if state['started']:
                raise intake.IntakeError(409)
            cards, failures = catalogue.initial_deck(**auth)
            intake.merge(state, cards)
            state['started'], state['failures'] = True, failures
            intake.select_round(state)
        elif operation in ('search', 'browse'):
            _fetch_results(state, data, auth)
        elif operation == 'add':
            intake.add_search_result(state, data['candidate_id'])
        elif operation == 'choose':
            intake.choose(state, data['candidate_id'], data['choice'])
        elif operation == 'neighbors':
            key = data['candidate_id']
            roots = {c['key']: c for c in intake.positive_roots(state)}
            if key not in roots or key in state['expanded']:
                raise intake.IntakeError()
            cards = catalogue.neighbors(roots[key], **auth)
            intake.merge(state, cards)
            state['expanded'].append(key)
            intake.select_round(state)
    except catalogue.CatalogueError as error:
        # Fixed status codes only. Never render/log provider bodies or exceptions.
        _profile(request, profile_id, 'select')
        intake.advance(state)
        request.session[intake.SESSION_KEY] = state
        # Django does not save sessions on HTTP 5xx. Consume this request's nonce
        # explicitly so the error page's next form and replay guard agree.
        request.session.save()
        return render(request, 'cabinet/provider_catalogue.html',
                      _page_context(request, profile, state, message='Provider request unavailable: ' + error.status.value), status=502)
    # Repeat current database authority after I/O before saving provider data.
    _profile(request, profile_id, 'select')
    if state['expires_at'] <= time.time():
        request.session.pop(intake.SESSION_KEY, None)
        raise intake.IntakeError(409)
    upgraded = durable.upgrade(request.user, profile.pk, state, request.session.session_key, create=True) if durable.available() else state
    if upgraded.get('run_id'):
        run, _ = durable.commit(actor=request.user, profile_id=profile.pk, run_id=upgraded['run_id'],
            revision=upgraded['revision'], nonce=upgraded['nonce'], payload=_payload(data), state=upgraded)
        state = durable.hydrate(run, upgraded)
    else:
        intake.advance(state)
    if operation == 'choose':
        replacement = _pin_round(before, state, data['candidate_id'])
        if 'note' in data:
            state.setdefault('notes', {})[data['candidate_id']] = data['note']
    else:
        replacement = ''
    request.session[intake.SESSION_KEY] = state
    request.session.set_expiry(max(1, int(state['expires_at'] - time.time())))
    if _partial(request):
        return _card_response(request, profile, state, data, replacement)
    _select_queue(state, queue)
    request.session[intake.SESSION_KEY] = state
    return _catalogue_redirect(profile, queue, data.get('candidate_id', ''))


def _payload(data):
    payload = {k: str(data.get(k) or '') for k in
               ('action', 'revision', 'run_id', 'medium', 'query', 'candidate_id', 'choice')}
    # Preserve receipt fingerprints for all pre-existing actions.
    if data['action'] == 'browse':
        payload.update(book_category=data['book_category'], page=str(data.get('page') or 1))
    for field in ('note', 'queue'):
        if field in data and (field != 'queue' or data[field]):
            payload[field] = data[field]
    return payload


def _fetch_results(state, data, auth):
    if data['action'] == 'browse':
        page = data.get('page') or 1
        cards, more = catalogue.browse_books(data['book_category'], data['query'], page, **auth)
        state['browse'] = {'query': data['query'], 'page': page,
                           'next_page': page + 1 if more else None}
    else:
        cards = catalogue.search(data['medium'], data['query'], **auth)
        state.pop('browse', None)
    intake.merge(state, cards, queue=False)
    state['search_results'] = [c.key for c in cards if c.key in state['candidates']
                               and c.key not in state['choices']]


def _store_durable(request, state):
    remember_current(state)
    request.session[intake.SESSION_KEY] = state
    # This TTL limits provider metadata only. It never deletes durable choices.
    request.session.set_expiry(max(1, int(state['expires_at'] - time.time())))


def _durable_action(request, profile, state, data):
    if not durable.available():
        return HttpResponse('Durable intake requires explicit operator preparation.\n', status=503)
    _profile(request, profile.pk, 'select')
    payload = _payload(data)
    prior = durable.replay(request.user, profile.pk, data['nonce'], payload)
    queue = data.get('queue') or 'all'
    if prior:
        run, result = prior
        if _partial(request) and run.revision != result['revision']:
            raise intake.IntakeError(409)
        if result.get('next_run_id'):
            run = durable.owned(request.user, profile.pk, result['next_run_id'])
        if not run.archived_at:
            cached = state if state.get('run_id') == str(run.pk) else None
            hydrated = durable.hydrate(run, cached)
            if cached:
                _pin_round(cached, hydrated)
            _store_durable(request, hydrated)
            if _partial(request):
                receipt = hydrated.get('card_receipt', {})
                replacement = receipt.get('replacement_key', '') if receipt.get('nonce') == data['nonce'] else ''
                return _card_response(request, profile, hydrated, data, replacement)
        return _catalogue_redirect(profile, queue, data.get('candidate_id', ''))
    intake.check_revision(state, str(data['revision']), data['nonce'])
    operation = data['action']
    if operation != 'resume' and data.get('run_id') and str(data['run_id']) != state.get('run_id'):
        raise intake.IntakeError(409)
    if operation == 'resume':
        if not data.get('run_id'):
            raise intake.IntakeError()
        run = durable.owned(request.user, profile.pk, data['run_id'])
        if run.archived_at:
            raise intake.IntakeError(409)
    if not state.get('run_id'):
        if operation not in ('continue', 'new_run'):
            raise intake.IntakeError(409)
        # A prototype history must be imported in full, never silently merged
        # with another run. Unsupported provenance stays on the legacy path.
        if state['choices']:
            state = durable.upgrade(request.user, profile.pk, state, request.session.session_key)
            if not state.get('run_id'):
                raise intake.IntakeError(409)
        else:
            run = durable.new_run(request.user, profile.pk, revision=state['revision'], nonce=state['nonce'])
            state = durable.hydrate(run, state)
    durable.owned(request.user, profile.pk, state['run_id'])
    auth = {'actor_id': request.user.pk, 'profile_id': profile.pk}
    error_status = None
    try:
        if operation in ('start', 'refresh'):
            if operation == 'start' and state['started']:
                raise intake.IntakeError(409)
            if operation == 'refresh':
                state = durable.hydrate(durable.owned(request.user, profile.pk, state['run_id']))
            cards, failures = catalogue.initial_deck(**auth)
            intake.merge(state, cards)
            state['started'], state['failures'] = True, failures
        elif operation in ('search', 'browse'):
            _fetch_results(state, data, auth)
        elif operation == 'add':
            key = data['candidate_id']
            if key not in state['search_results'] or key not in state['candidates']:
                raise intake.IntakeError()
            state['search_results'].remove(key)
            if key not in state['queued']:
                state['queued'].append(key)
            state['added_key'] = key
        elif operation == 'neighbors':
            key = data['candidate_id']
            roots = {c['key']: c for c in intake.positive_roots(state)}
            if key not in roots or key in state['expanded']:
                raise intake.IntakeError()
            # Mark before I/O, including failures: one bounded attempt per root
            # per temporary deck. An explicit refresh can begin a new deck.
            state['expanded'].append(key)
            cards = catalogue.neighbors(roots[key], **auth)
            intake.merge(state, cards)
        elif operation == 'choose':
            if state.get('added_key') == data['candidate_id']:
                state.pop('added_key', None)
        elif operation == 'export':
            pass
        elif operation not in ('continue', 'resume', 'new_run', 'reset', 'undo', 'correct'):
            raise intake.IntakeError()
    except catalogue.CatalogueError as error:
        error_status = error.status.value
    _profile(request, profile.pk, 'select')
    if state['expires_at'] <= time.time():
        raise intake.IntakeError(409)
    before = deepcopy(state)
    run, result = durable.commit(actor=request.user, profile_id=profile.pk, run_id=state['run_id'],
        revision=data['revision'], nonce=data['nonce'], payload=payload, state=state)
    if result.get('next_run_id'):
        run = durable.owned(request.user, profile.pk, result['next_run_id'])
        state = durable.hydrate(run, state if state.get('run_id') == str(run.pk) else None)
    else:
        state = durable.hydrate(run, state)
    replacement = ''
    if operation in ('choose', 'correct'):
        replacement = _pin_round(before, state, data['candidate_id'])
        state['card_receipt'] = {'nonce': data['nonce'], 'replacement_key': replacement}
    else:
        state['queue'] = 'all'
        _select_queue(state, queue)
    _store_durable(request, state)
    if _partial(request):
        return _card_response(request, profile, state, data, replacement)
    if operation == 'export':
        import json
        body = json.dumps(durable.export_history(request.user, profile.pk), ensure_ascii=True)
        _profile(request, profile.pk, 'select')
        if len(body) > 2 * 1024 * 1024:
            return HttpResponse('History exceeds the bounded export size.\n', status=413)
        response = HttpResponse(body, content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="human-intake.json"'
        return response
    if error_status:
        request.session.save()
        return render(request, 'cabinet/provider_catalogue.html', _page_context(request, profile, state,
            message='Provider request unavailable: ' + error_status), status=502)
    return _catalogue_redirect(profile, queue, replacement or data.get('candidate_id', ''))


def _card_response(request, profile, state, data, replacement):
    context = _context(profile, state)
    card = next((card for card in context['cards'] if card['key'] == replacement), None)
    html = render_to_string('cabinet/provider_card.html', {**context, 'card': card, 'search_result': False}, request=request) if card else ''
    enhance = _page_context(request, profile, state)
    return JsonResponse({'enhance_context': {key: enhance[key] for key in ('enhance_digest', 'enhance_token', 'enhance_url', 'active_queue', 'enhancement') if key in enhance},
        'explanation_updates': {c['key']: c['for_you'] for c in context['cards']},
        'ok': True, 'operation': data['action'], 'submitted_key': data['candidate_id'],
        'replacement_html': html, 'revision': state['revision'], 'nonce': state['nonce'],
        'run_id': state.get('run_id', ''), 'queue': state.get('queue', 'all'),
        'counts': context['choice_counts'], 'status_message': ('Response updated.' if data['action'] == 'correct' else
            'Response saved.' if card else 'Response saved. No more cached picks in this queue.')})


@login_required
@require_GET
@_guard
def intake_home(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    return render(request, 'cabinet/provider_catalogue.html', _page_context(request, profile, _catalogue_state(request, profile)))


@login_required
@require_POST
@_guard
def durable_action(request, profile_id):
    _profile(request, profile_id, 'select')
    if not durable.available():
        return HttpResponse('Durable intake requires explicit operator preparation.\n', status=503)
    return action(request, profile_id)
