"""Real-source proposals and explicitly saved, unbooked owner planning holds.

Seller/session verification remains necessary for a purchase. Saving a hold or
exporting a local packet never writes to an external account.
"""
from django import forms
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache

from shelf import discovery_views, transport_planning as transport
from shelf.beat_events import BeatEvent
from shelf.discovery_state import public_authorized
from shelf.provider_views import _profile
from shelf import real_outings
from shelf.acquisition_contract import Blocked
from shelf.discovery_state import Conflict


class JourneyForm(forms.Form):
    hold_token = forms.CharField(required=False, widget=forms.HiddenInput(), max_length=1000)
    save_confirmed = forms.BooleanField(required=False, label='Save these source facts and my estimates as an unbooked private planning hold. No calendar write or email is sent.')
    occasion = forms.ChoiceField(label='How you want to use this evening', required=False,
        choices=(('', 'Undecided'), ('date_night', 'Explore as a date night'), ('other', 'Another kind of outing')))
    cycle_origin = forms.RegexField(label='Optional cycle origin for Google Maps links (not saved)',
        regex=r"^[\w .,’'/-]+$", max_length=200, required=False,
        help_text='Leave blank to use stations only. Opening a generated Google Maps link shares this origin and the selected station with Google.')
    origin = forms.ChoiceField(label='Public outward station', choices=transport.ORIGINS)
    arrival = forms.ChoiceField(label='Proposed arrival station (venue connection unverified)', choices=transport.ORIGINS)
    companion_origin = forms.ChoiceField(label='Companion’s example origin (optional)', required=False,
                                        choices=(('', 'No separate origin'), *transport.ORIGINS))
    bike = forms.ChoiceField(label='Bicycle', choices=transport.BIKES)
    carry_bike = forms.BooleanField(label='Take the bicycle on public transport', required=False)
    outward_mode = forms.ChoiceField(label='Outward mode', choices=transport.MODES)
    return_mode = forms.ChoiceField(label='Return mode', choices=transport.MODES)
    outward_minutes = forms.IntegerField(label='Estimated outward minutes, including connections', min_value=1, max_value=240)
    companion_minutes = forms.IntegerField(label='Estimated companion outward minutes', min_value=1, max_value=240)
    return_minutes = forms.IntegerField(label='Estimated return minutes, including connections', min_value=1, max_value=240)
    lock_minutes = forms.IntegerField(label='Extra bicycle parking/handling minutes per direction', min_value=0, max_value=60)
    arrival_buffer = forms.IntegerField(label='Arrival buffer before the show, minutes', min_value=0, max_value=120)
    assumed_show_time = forms.TimeField(label='Your assumed show time, only if source show time is unknown', required=False,
                                       input_formats=['%H:%M'], widget=forms.TimeInput(attrs={'type': 'time'}))
    leave_time = forms.TimeField(label='Preferred time to leave the venue (finish is unknown)', input_formats=['%H:%M'],
                                widget=forms.TimeInput(attrs={'type': 'time'}))
    batman_cycle = forms.IntegerField(label='Batman: estimated cycle minutes to station (optional)', required=False, min_value=1, max_value=120)
    batman_onward = forms.IntegerField(label='Batman: estimated onward minutes including waits/transfers/final leg', required=False, min_value=1, max_value=240)
    coburg_cycle = forms.IntegerField(label='Coburg: estimated cycle minutes to station (optional)', required=False, min_value=1, max_value=120)
    coburg_onward = forms.IntegerField(label='Coburg: estimated onward minutes including waits/transfers/final leg', required=False, min_value=1, max_value=240)
    merlynston_cycle = forms.IntegerField(label='Merlynston: estimated cycle minutes to station (optional)', required=False, min_value=1, max_value=120)
    merlynston_onward = forms.IntegerField(label='Merlynston: estimated onward minutes including waits/transfers/final leg', required=False, min_value=1, max_value=240)
    return_day = forms.TypedChoiceField(label='Return departure day', choices=((0, 'Same calendar day'), (1, 'Next calendar day')), coerce=int)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def detail(request, profile_id):
    public_authorized(request, profile_id)
    if set(request.GET) != {'event'} or len(request.GET.getlist('event')) != 1:
        raise Http404
    key = request.GET['event']
    entries = discovery_views.public_entries(request, profile_id)
    entry = entries.get(('beat', '', ''))
    event = next((c for c in entry['outcome'].candidates if c.key == key), None) if entry else None
    if type(event) is not BeatEvent:
        return HttpResponse('Source details expired or unavailable. Return to Music & events and read Beat again.', status=410)
    initial = dict(origin='flinders', arrival='brunswick', companion_origin='', bike='full_size',
        carry_bike=True, outward_mode='metro_train', return_mode='metro_train', outward_minutes=45,
        companion_minutes=30, return_minutes=45, lock_minutes=10, arrival_buffer=20,
        assumed_show_time='', leave_time='23:00', return_day=0)
    hold_token, existing_hold = real_outings.token(request,profile_id,event)
    initial['hold_token'] = hold_token
    if request.method == 'POST':
        if (len(request.body) > 8192 or set(request.POST) - {*JourneyForm.base_fields, 'csrfmiddlewaretoken','intent'}
                or any(len(request.POST.getlist(k)) != 1 for k in request.POST)):
            return HttpResponse('Invalid bounded journey form.', status=400)
        form = JourneyForm(request.POST)
    else:
        form = JourneyForm(initial=initial)
    plan = None
    if request.method == 'POST' and form.is_valid():
        values = dict(form.cleaned_data)
        for name in ('assumed_show_time', 'leave_time'):
            values[name] = values[name].strftime('%H:%M') if values[name] else ''
        try:
            plan = transport.proposal(event, values, timezone.now())
            intent=request.POST.get('intent','preview')
            if intent not in ('preview','save_hold'):raise ValueError('Unknown planning action.')
            if intent=='save_hold':
                if not values['save_confirmed']:raise ValueError('Explicit consent to save this unbooked hold is required.')
                saved=real_outings.save(request,profile_id,event,plan,values['hold_token'])
                return redirect('event_plan',profile_id=profile_id,plan_id=saved.pk)
        except (ValueError,Blocked,Conflict) as error:
            form.add_error(None, str(error))
    public_authorized(request, profile_id)
    return render(request, 'cabinet/date_night.html', {'event': event, 'form': form, 'proposal': plan,
        'selected_profile': _profile(request, profile_id, 'select'), 'profile_id': profile_id, 'rules_url': transport.RULES_URL,
        'journey_url': transport.JOURNEY_URL, 'rules_reviewed_at': transport.REVIEWED_AT,
        'existing_hold':existing_hold})
