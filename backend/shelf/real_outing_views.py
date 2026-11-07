"""Real-source hold review and local exports; no provider writes in HTTP routes."""
import json
from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from shelf import event_plans, real_outings, google_handoff
from shelf.calendar_handoff import CalendarHandoff, instant, zone
from shelf.event_plan_views import _form, _guard


class HoldActionForm(forms.Form):
    action=forms.ChoiceField(choices=[(x,x) for x in ('ics','eml','reserve_calendar','reserve_email')])
    version=forms.IntegerField(min_value=0)
    nonce=forms.CharField(max_length=64)
    preview=forms.RegexField(r'^[a-f0-9]{64}$')
    confirmed=forms.BooleanField(required=True)


def render_detail(request,plan):
    calendar=CalendarHandoff.from_dict(plan.data['calendar'])
    response=render(request,'cabinet/real_outing.html',{'plan':plan,'selected_profile':plan.profile,'calendar':calendar,
        'occupied_start':instant(calendar.event.start).astimezone(zone(calendar.event.timezone)),
        'occupied_end':instant(calendar.event.end).astimezone(zone(calendar.event.timezone)),
        'source':plan.data['source'],'preview_digest':event_plans.preview_digest(plan),
        'handoffs':[{'kind':kind,**receipt,'reconciliation':google_handoff.reconciliation(receipt['packet'])}
            for kind,receipt in plan.data['handoffs'].items()]})
    response['Cache-Control']='private, no-store';response['Referrer-Policy']='no-referrer'
    return response


@login_required
@require_POST
@_guard
def action(request,profile_id,plan_id):
    fields=_form(request,HoldActionForm)
    plan,result=real_outings.apply(request,profile_id,plan_id,fields)
    calendar=CalendarHandoff.from_dict(plan.data['calendar'])
    if fields['action']=='ics':
        if result is None:
            receipt=next(r for r in calendar.receipts if r.operation=='export' and r.revision==calendar.revision)
            result=calendar.export_ics(receipt.key,actor_id=str(request.user.pk))
        response=HttpResponse(result,content_type='text/calendar; charset=utf-8');filename='cabinet-planning-hold.ics'
    elif fields['action']=='eml':
        response=HttpResponse(result or google_handoff.email_file(calendar),content_type='message/rfc822');filename='cabinet-owner-packet.eml'
    else:
        response=HttpResponse(json.dumps(result,indent=2),content_type='application/json');filename='cabinet-supervised-google-handoff.json'
    response['Content-Disposition']='attachment; filename="'+filename+'"'
    response['Cache-Control']='private, no-store';response['Referrer-Policy']='no-referrer'
    return response
