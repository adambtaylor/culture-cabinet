"""Conventional CSRF-protected forms; GET neither queues nor reads a social page."""
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from shelf import social_discovery as storage, social_sources as sources
from shelf import social_event_intake as event_intake
from shelf.discovery_state import Conflict, public_authorized
from shelf.provider_views import _profile


class ActionForm(forms.Form):
    mode = forms.ChoiceField(choices=[(s, s) for s in ('household', 'sample')])
    action = forms.ChoiceField(choices=[(s, s) for s in ('add_source', 'remove_source', 'queue', 'run',
        'cancel', 'clear_tasks', 'manual', 'import', 'discard', 'withdraw',
        'observe_event', 'event_withdraw')])
    revision = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=200)
    identity = forms.RegexField(r'^[a-f0-9]{32,64}$', required=False)
    platform = forms.ChoiceField(required=False, choices=[(s, s) for s in sources.HOSTS])
    url = forms.CharField(required=False, max_length=512)
    label = forms.CharField(required=False, max_length=100)
    kind = forms.ChoiceField(required=False, choices=[(s, s) for s in ('venue', 'artist', 'page', 'event')])
    scope = forms.ChoiceField(required=False, choices=[(s, s) for s in ('followed', 'public_event')])
    scenario = forms.ChoiceField(required=False, choices=[(s, s) for s in sources.SCENARIOS])
    title = forms.CharField(required=False, max_length=160)
    post_url = forms.CharField(required=False, max_length=512)
    event_url = forms.CharField(required=False, max_length=2048)
    listed_when = forms.CharField(required=False, max_length=160)
    platform_event_id = forms.CharField(required=False, max_length=100)
    canonical_url = forms.CharField(required=False, max_length=512)
    organizer = forms.CharField(required=False, max_length=160)
    venue = forms.CharField(required=False, max_length=160)
    raw_date_time = forms.CharField(required=False, max_length=200)
    timezone_name = forms.CharField(required=False, max_length=64)
    observed_at = forms.CharField(required=False, max_length=64)
    evidence_ref = forms.CharField(required=False, max_length=240)
    actor_origin = forms.ChoiceField(required=False, choices=[(s, s) for s in
        ('owner_confirmed', 'agent_marked', 'unknown')])
    signal_type = forms.ChoiceField(required=False, choices=[(s, s) for s in
        ('interested', 'attending', 'none')])
    confidence = forms.ChoiceField(required=False, choices=[(s, s) for s in
        ('observed', 'reported', 'uncertain')])
    lineage_ref = forms.CharField(required=False, max_length=240)
    supersedes = forms.RegexField(r'^[a-f0-9]{64}$', required=False)
    conflict_note = forms.CharField(required=False, max_length=240)
    public_confirmed = forms.BooleanField(required=False)
    confirmed = forms.BooleanField(required=True)

    def clean(self):
        data = super().clean()
        action = data.get('action')
        event_fields = {'platform', 'platform_event_id', 'canonical_url', 'title', 'organizer',
                        'venue', 'raw_date_time', 'timezone_name', 'observed_at', 'evidence_ref',
                        'actor_origin', 'signal_type', 'confidence', 'lineage_ref', 'supersedes',
                        'conflict_note', 'public_confirmed'}
        permitted = {'observe_event': event_fields, 'add_source': {'platform', 'url', 'label', 'kind', 'scope'},
                     'queue': {'identity', 'scenario'}, 'manual': {'identity', 'title', 'post_url', 'event_url', 'listed_when'},
                     'clear_tasks': set()}.get(action, {'identity'})
        optional = {'platform', 'url', 'label', 'kind', 'scope', 'scenario', 'identity',
                    'title', 'post_url', 'event_url', 'listed_when'} | event_fields
        if any(data.get(key) for key in optional - permitted):
            raise forms.ValidationError('Unexpected fields for this action.')
        required = permitted - {'scenario', 'event_url', 'listed_when', 'organizer', 'venue',
                                'supersedes', 'conflict_note'}
        if any(not data.get(key) for key in required):
            raise forms.ValidationError('Complete the displayed fields.')
        if action == 'event_withdraw' and len(data.get('identity', '')) != 64:
            raise forms.ValidationError('Choose an exact event observation.')
        if data.get('mode') == 'household' and data.get('scenario'):
            raise forms.ValidationError('Synthetic outcomes belong only to the isolated sample.')
        return data


def guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except (Conflict, sources.SourceError, event_intake.EventIntakeError) as error:
            return HttpResponse(str(error) + '\nReload social discovery before continuing.',
                                status=409, content_type='text/plain')
        except (IntegrityError, OperationalError):
            return HttpResponse('Social discovery storage is unavailable or busy. Reload before retrying.',
                                status=503, content_type='text/plain')
    return call


@login_required
@require_GET
@guard
def index(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    public_authorized(request, profile_id)
    mode = request.GET.get('mode', 'household')
    if mode not in ('household', 'sample') or any(k != 'mode' or len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise Http404
    ready = storage.available()
    state = storage.owned(request, profile_id, mode) if ready else None
    data = state.data if state else storage.empty_data()
    selected = {row['id']: row for row in data['sources']}
    tasks = [dict(row, source=selected[row['source_id']], status_label=sources.STATUSES[row['status']])
             for row in reversed(data['tasks'])]
    # Read cached public feed entries only. No provider operation or history write.
    from shelf.discovery_views import public_entries
    existing = {storage.dedup_key(card.url): card for entry in public_entries(request, profile_id).values()
                for card in entry['outcome'].candidates if card.url} if mode == 'household' else {}
    inbox = [dict(row, source=selected[row['source_id']], fresh=sources.fresh(row, timezone.now()),
        duplicate=existing.get(storage.dedup_key(row['event_url'] or row['post_url'])))
        for row in reversed(data['observations'])]
    return render(request, 'cabinet/social_discovery.html', {
        'selected_profile': profile, 'mode': mode, 'ready': ready, 'sources': list(selected.values()),
        'tasks': tasks, 'inbox': inbox, 'scenarios': sources.SCENARIOS,
        'event_intake': event_intake.event_intake_view(data),
        'revision': state.revision if state else 0,
        'nonce': state.nonce if state else storage.initial_nonce(request.user.pk, profile_id, mode),
        'action_url': f'/profiles/{profile_id}/social-discovery/action/',
        'temporary_notice': 'Selected sources, task receipts and short event leads are local and durable. Imported social leads leave current discovery after 24 hours. Removing a source deletes its stored leads; explicit taste responses remain separate.',
    })


@login_required
@require_POST
@guard
def action(request, profile_id):
    public_authorized(request, profile_id)
    if not storage.available():
        raise Conflict('Social discovery requires its isolated candidate migration.')
    if len(request.body) > 8192 or any(k not in {*ActionForm.base_fields, 'csrfmiddlewaretoken'}
            or len(request.POST.getlist(k)) != 1 for k in request.POST):
        return HttpResponse('Invalid bounded social form.', status=400, content_type='text/plain')
    form = ActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Invalid social form. Complete the displayed fields and confirm its scope.',
                            status=400, content_type='text/plain')
    storage.apply(request, profile_id, form.cleaned_data)
    return redirect(f'/profiles/{profile_id}/social-discovery/?mode={form.cleaned_data["mode"]}')
