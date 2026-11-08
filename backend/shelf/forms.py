"""Django form contract for M. Visible fields are typed; no JSON editor."""
from dataclasses import asdict
import uuid
from django import forms
from shelf.feedback import MutationCommand, FeedbackError, LABELS, SIGNALS, validate_command
from shelf.models import FeedbackEvent

CHOICES = {'interest':('no','maybe','yes'), 'familiarity':('unknown','unfamiliar','aware','experienced'),
           'completion':('not_started','in_progress','completed'), 'visibility':('visible','hidden')}


class FeedbackForm(forms.Form):
    mutation_id = forms.UUIDField(widget=forms.HiddenInput)
    target_type = forms.ChoiceField(choices=[('item','item'),('session','session')],widget=forms.HiddenInput)
    scope = forms.ChoiceField(choices=[('item','item'),('session','session')],widget=forms.HiddenInput)
    signal = forms.ChoiceField(choices=[(s,LABELS[s]) for s in SIGNALS],widget=forms.HiddenInput)
    operation = forms.ChoiceField(choices=[('set','set'),('reverse','reverse')],widget=forms.HiddenInput)
    base_revision = forms.IntegerField(min_value=0,widget=forms.HiddenInput)
    context_event_id = forms.UUIDField(required=False,widget=forms.HiddenInput)
    corrects_id = forms.UUIDField(required=False,widget=forms.HiddenInput)
    reverses_id = forms.UUIDField(required=False,widget=forms.HiddenInput)

    def __init__(self,*args,target_id,signal=None,operation=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.target_id=str(target_id)
        signal=signal or (self.data.get('signal') if self.is_bound else self.initial.get('signal'))
        operation=operation or (self.data.get('operation') if self.is_bound else self.initial.get('operation'))
        self.signal_label=LABELS.get(signal,'Feedback')
        if operation=='set':
            if signal=='enjoyment': self.fields['rating']=forms.TypedChoiceField(label=LABELS[signal],choices=[(i,str(i)) for i in range(1,6)],coerce=int)
            elif signal in CHOICES: self.fields['choice']=forms.ChoiceField(label=LABELS[signal],choices=[(x,x.replace('_',' ').capitalize()) for x in CHOICES[signal]])
            elif signal in ('saved','skipped','opened'): self.fields['checked']=forms.TypedChoiceField(label=LABELS[signal],choices=[('true','Yes'),('false','No')] if signal!='opened' else [('true','Record opened action')],coerce=lambda x:x=='true')
            elif signal in ('constraint','note'): self.fields['text']=forms.CharField(label=LABELS[signal],max_length=500,required=False,strip=False,widget=forms.Textarea)

    def clean(self):
        data=super().clean()
        if self.is_bound:
            allowed=set(self.fields)|{'csrfmiddlewaretoken'}
            if set(self.data)-allowed or any(len(self.data.getlist(k))!=1 for k in self.data if hasattr(self.data,'getlist')):
                raise forms.ValidationError('Submit each field once using this form.')
        if self.errors: return data
        signal=data['signal']; body=None
        if data['operation']=='set':
            if signal=='enjoyment': body=dict(value=data['rating'],basis='experienced',scale='integer-1-5')
            elif signal=='interest': body=dict(value=data['choice'],basis='anticipated')
            elif signal in CHOICES: body={'value':data['choice']}
            elif signal in ('saved','skipped','opened'): body={'value':data['checked']}
            elif signal=='constraint': body={'text':data['text']} if data['text'] else None
            elif signal=='note': body={'text':data['text']}
        command=MutationCommand(**{k:(str(v) if isinstance(v,uuid.UUID) else v or None if k in ('context_event_id','corrects_id','reverses_id') else v) for k,v in data.items() if k in MutationCommand.__dataclass_fields__},target_id=self.target_id,value=body)
        try: validate_command(command)
        except FeedbackError: raise forms.ValidationError('Choose a valid feedback value.') from None
        self.command=command
        return data


class ExportForm(forms.Form):
    format=forms.ChoiceField(label='Download format',choices=[('json','JSON'),('csv','CSV'),('md','Markdown')])
    def clean(self):
        data=super().clean()
        if set(self.data)-{'csrfmiddlewaretoken','format'} or any(len(self.data.getlist(k))!=1 for k in self.data if hasattr(self.data,'getlist')):
            raise forms.ValidationError('Choose one download format.')
        return data


def forms_for(profile,target,rows):
    forms=[]; undo=[]
    for signal in SIGNALS:
        row=rows.get(signal)
        if signal.startswith(('legacy_','seed_')) or (row and row.erased): continue
        last=FeedbackEvent.objects.filter(profile=profile,target=target,signal=signal,scope=target.kind).order_by('-revision').first()
        initial=dict(mutation_id=uuid.uuid4(),target_type=target.kind,scope=target.kind,signal=signal,operation='set',
            base_revision=row.revision if row else 0,context_event_id=str(last.pk) if last else None,
            corrects_id=str(row.effective_event_id) if row and row.effective_event_id else None)
        body=(row.value if row else None) or {}
        if signal=='enjoyment': initial['rating']=body.get('value')
        elif signal in CHOICES: initial['choice']=body.get('value')
        elif signal in ('saved','skipped','opened'): initial['checked']='true' if body.get('value') else 'false'
        elif signal in ('constraint','note'): initial['text']=body.get('text','')
        forms.append(FeedbackForm(target_id=target.pk,initial=initial))
    for event in FeedbackEvent.objects.filter(profile=profile,target=target,operation='set',erased=False,reversals__isnull=True).order_by('-recorded_at'):
        row=rows[event.signal]
        last=FeedbackEvent.objects.get(profile=profile,target=target,signal=event.signal,scope=target.kind,revision=row.revision)
        f=FeedbackForm(target_id=target.pk,initial=dict(mutation_id=uuid.uuid4(),target_type=target.kind,scope=target.kind,
            signal=event.signal,operation='reverse',base_revision=row.revision,context_event_id=str(last.pk),reverses_id=str(event.pk)))
        f.statement_label=LABELS[event.signal]
        undo.append(f)
    return tuple(forms),tuple(undo)
