"""Authenticated event-planning rehearsal, with no live external actions."""
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import event_plans
from shelf.acquisition_contract import Blocked, ContractError
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import CONTACTS, SCENARIOS, sessions
from shelf.models import EventPlan
from shelf.provider_views import _profile


class StartForm(forms.Form):
    session_id = forms.ChoiceField(choices=[(e.session_id, e.start) for e in sessions()])
    quantity = forms.IntegerField(min_value=1, max_value=10)
    contacts = forms.MultipleChoiceField(required=False, choices=[(r.recipient_id, r.label) for r in CONTACTS])
    purchase_scenario = forms.ChoiceField(choices=[(s, s) for s in SCENARIOS])
    calendar_scenario = forms.ChoiceField(choices=[(s, s) for s in SCENARIOS])


class ActionForm(forms.Form):
    action = forms.ChoiceField(choices=[(s, s) for s in (
        'refresh_quote', 'approve_purchase', 'handoff', 'reconcile_purchase',
        'calendar', 'reconcile_calendar', 'reschedule', 'cancel_event', 'packet_email', 'reconcile_email')])
    version = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=64)
    preview = forms.RegexField(r'^[a-f0-9]{64}$')
    recipient = forms.CharField(max_length=160, required=False)
    receipt = forms.CharField(max_length=160, required=False)
    packet_digest = forms.RegexField(r'^[a-f0-9]{64}$', required=False)
    confirmed = forms.BooleanField(required=True)


class RestaurantForm(forms.Form):
    action = forms.ChoiceField(choices=[(s, s) for s in ('search', 'choose', 'approve', 'reserve', 'reconcile')])
    version = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=64)
    preview = forms.RegexField(r'^[a-f0-9]{64}$')
    confirmed = forms.BooleanField(required=True)
    wish = forms.CharField(max_length=500, required=False, strip=False)
    party_size = forms.IntegerField(min_value=1, max_value=10, required=False)
    vegetarian = forms.BooleanField(required=False)
    date_from = forms.ChoiceField(choices=[(s, s) for s in ('2027-01-15', '2027-01-16')], required=False)
    date_to = forms.ChoiceField(choices=[(s, s) for s in ('2027-01-15', '2027-01-16')], required=False)
    earliest = forms.ChoiceField(choices=[(s, s) for s in ('17:30', '18:00', '18:30')], required=False)
    latest = forms.ChoiceField(choices=[(s, s) for s in ('17:30', '18:00', '18:30')], required=False)
    preferred = forms.ChoiceField(choices=[(s, s) for s in ('17:30', '18:00', '18:30')], required=False)
    meal_minutes = forms.IntegerField(min_value=45, max_value=150, required=False)
    travel_minutes = forms.IntegerField(min_value=0, max_value=120, required=False)
    buffer_minutes = forms.IntegerField(min_value=0, max_value=90, required=False)
    scenario = forms.ChoiceField(choices=[(s, s) for s in SCENARIOS], required=False)
    offer_key = forms.RegexField(r'^[a-f0-9]{64}$', required=False)

    def clean(self):
        data = super().clean()
        action = data.get('action')
        constraints = ('party_size', 'vegetarian', 'date_from', 'date_to', 'earliest',
                       'latest', 'preferred', 'meal_minutes', 'travel_minutes', 'buffer_minutes')
        allowed = {'action', 'version', 'nonce', 'preview', 'confirmed', 'csrfmiddlewaretoken'}
        if action == 'search':
            allowed.update((*constraints, 'wish', 'scenario'))
            if any(data.get(k) in (None, '') for k in (*constraints, 'scenario') if k != 'vegetarian'):
                raise forms.ValidationError('Complete the bounded restaurant search.')
            if (data['date_from'] > data['date_to'] or not data['earliest'] <= data['preferred'] <= data['latest']):
                raise forms.ValidationError('Invalid date or preferred-time window.')
        elif action == 'choose':
            allowed.add('offer_key')
            if not data.get('offer_key'):
                raise forms.ValidationError('Choose the displayed table.')
        if set(self.data) - allowed:
            raise forms.ValidationError('Unexpected restaurant action fields.')
        result = {k: data[k] for k in ('action', 'version', 'nonce', 'preview', 'confirmed') if k in data}
        if action == 'search':
            result.update(constraints={k: data[k] for k in constraints}, wish=data['wish'], scenario=data['scenario'])
        elif action == 'choose':
            result['offer_key'] = data['offer_key']
        return result


def _form(request, cls):
    if len(request.body) > 8192:
        raise Blocked('Form too large.')
    form = cls(request.POST)
    if (set(request.POST) - {*form.fields, 'csrfmiddlewaretoken'}
            or any(len(request.POST.getlist(k)) != 1 for k in request.POST if k != 'contacts')
            or not form.is_valid()):
        raise Blocked('Invalid event-plan form. Reload and choose the displayed options.')
    return form.cleaned_data


def _guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except (Blocked, ContractError, Conflict) as error:
            # Plain text cannot interpret provider or user content as markup.
            return HttpResponse(str(error) + '\nReload the plan to see its retained state.\n',
                                status=409, content_type='text/plain')
        except (IntegrityError, OperationalError):
            return HttpResponse('Concurrent or unavailable storage. Reload; do not repeat an uncertain purchase.\n',
                                status=409, content_type='text/plain')
        except OSError:
            return HttpResponse('Provider unavailable. The outcome remains unknown; reconcile the saved receipt.\n',
                                status=503, content_type='text/plain')
    return call


@login_required
@require_GET
def index(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    ready = event_plans.available()
    plans = EventPlan.objects.filter(actor=request.user, profile_id=profile_id) if ready else []
    return render(request, 'cabinet/event_plans.html', {'selected_profile': profile,
        'ready': ready, 'plans': plans, 'sessions': sessions(), 'contacts': CONTACTS, 'scenarios': SCENARIOS})


@login_required
@require_POST
@_guard
def start(request, profile_id):
    public_authorized(request, profile_id)
    if not event_plans.available():
        raise Blocked('Event planning requires the isolated candidate migration.')
    plan = event_plans.create(request, profile_id, **_form(request, StartForm))
    return redirect('event_plan', profile_id=profile_id, plan_id=plan.pk)


@login_required
@require_GET
def detail(request, profile_id, plan_id):
    from shelf.outing_views import OutingForm
    if not event_plans.available():
        raise Http404
    plan = event_plans.owned(request, profile_id, plan_id)
    from shelf import real_outing_views, real_outings
    if real_outings.is_hold(plan):
        return real_outing_views.render_detail(request,plan)
    if plan.mode != 'sample':
        raise Http404
    response = render(request, 'cabinet/event_plan.html', {
        **event_plans.context(plan), 'selected_profile': plan.profile, 'outing_form': OutingForm(plan=plan)})
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@login_required
@require_POST
@_guard
def action(request, profile_id, plan_id):
    if not event_plans.available():
        raise Http404
    plan = event_plans.apply(request, profile_id, plan_id, _form(request, ActionForm))
    return redirect('event_plan', profile_id=profile_id, plan_id=plan.pk)


@login_required
@require_POST
@_guard
def restaurant_action(request, profile_id, plan_id):
    from shelf import restaurant_plans
    if not event_plans.available():
        raise Http404
    plan = restaurant_plans.apply(request, profile_id, plan_id, _form(request, RestaurantForm))
    return redirect('event_plan', profile_id=profile_id, plan_id=plan.pk)
