"""Bounded rehearsal choices on the existing owned EventPlan."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from shelf import event_plans, outing_plans
from shelf.event_plan_views import _form, _guard


class OutingForm(forms.Form):
    version=forms.IntegerField(min_value=0)
    nonce=forms.CharField(max_length=64)
    preview=forms.RegexField(r'^[a-f0-9]{64}$')
    confirmed=forms.BooleanField(required=True,label='Save these estimates and rebuild the packet; this sends nothing.')
    dinner_minutes=forms.IntegerField(min_value=0,max_value=180,initial=60,label='Meal allowance if no table selected (minutes)')
    transfer_minutes=forms.IntegerField(min_value=0,max_value=180,initial=20,label='Meal-to-event travel allowance (minutes)')
    arrival_buffer=forms.IntegerField(min_value=0,max_value=180,initial=20,label='Event arrival buffer (minutes)')
    drink_minutes=forms.IntegerField(min_value=0,max_value=180,initial=30,label='Optional after-event drink allowance (minutes)')
    return_buffer=forms.IntegerField(min_value=0,max_value=180,initial=10,label='Return departure buffer (minutes)')

    def __init__(self,*args,plan,**kwargs):
        self.plan=plan
        super().__init__(*args,**kwargs)
        self.initial.update(version=plan.version,nonce=plan.nonce,preview=event_plans.preview_digest(plan))
        for key in ('version','nonce','preview'):self.fields[key].widget=forms.HiddenInput()
        old=plan.data.get('outing',{}).get('choices',{})
        for key in ('dinner_minutes','transfer_minutes','arrival_buffer','drink_minutes','return_buffer'):
            if key in old:self.initial[key]=old[key]
        for person in outing_plans.participants(plan):
            prefix=person['id']+'__';label=person['label']
            choices={'origin':forms.ChoiceField(choices=list(outing_plans.ORIGINS.items()),initial='batman'),
                'out_mode':forms.ChoiceField(choices=list(outing_plans.MODES.items()),initial='bike_train'),
                'return_mode':forms.ChoiceField(choices=list(outing_plans.MODES.items()),initial='bike_train'),
                'out_minutes':forms.IntegerField(min_value=1,max_value=240,initial=40),
                'return_minutes':forms.IntegerField(min_value=1,max_value=240,initial=45),
                'handling_minutes':forms.IntegerField(min_value=0,max_value=240,initial=10),
                'park_bike':forms.BooleanField(required=False,initial=True),
                'parking':forms.CharField(required=False,max_length=200,initial='Storage availability unverified'),
                'fare':forms.DecimalField(required=False,min_value=0,max_value=10000,decimal_places=2,max_digits=7)}
            for key,field in choices.items():
                field.label=label+' — '+key.replace('_',' ')
                self.fields[prefix+key]=field
                if key in old.get('people',{}).get(person['id'],{}):self.initial[prefix+key]=old['people'][person['id']][key]

    def clean(self):
        data=super().clean()
        if self.errors:return data
        choices={k:data[k] for k in ('dinner_minutes','transfer_minutes','arrival_buffer','drink_minutes','return_buffer')}
        choices['people']={}
        for person in outing_plans.participants(self.plan):
            prefix=person['id']+'__'
            row={key[len(prefix):]:value for key,value in data.items() if key.startswith(prefix)}
            row['fare']='' if row['fare'] is None else str(row['fare'])
            choices['people'][person['id']]=row
        return {**{k:data[k] for k in ('version','nonce','preview','confirmed')},'choices':choices}


@login_required
@require_POST
@_guard
def configure(request,profile_id,plan_id):
    if not event_plans.available():raise Http404
    plan=event_plans.owned(request,profile_id,plan_id)
    fields=_form(request,lambda *a,**k:OutingForm(*a,plan=plan,**k))
    outing_plans.apply(request,profile_id,plan_id,fields)
    return redirect('event_plan',profile_id=profile_id,plan_id=plan_id)
