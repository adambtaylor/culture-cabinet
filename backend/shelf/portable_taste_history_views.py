"""Native reference-only history forms; every display comes from current guards."""
from functools import wraps
from urllib.parse import urlencode
import uuid

from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET,require_POST,require_http_methods

from . import grant_authority as ga
from .card_action_views import fields,_asset
from .models import PortableJourneyCommand
from .portable_taste_history import TasteHistory
from .taste_history_presentation import render_taste_history,render_taste_history_compare


PREVIEW={'profile_ref','checkpoint_ref','expected_state_digest','command_id'}
RESTORE={'profile_ref','preview_ref','preview_digest','command_id','confirmed'}
UNDO={'profile_ref','restore_command_ref','expected_state_digest','command_id','confirmed'}


def _errors(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request,*args,**kwargs):
        try:return view(request,*args,**kwargs)
        except ga.AuthorityFault as fault:
            message=('These choices changed or the preview expired. Return to Taste history for a fresh comparison.'
                if fault.status in (409,410) else 'Taste history is unavailable. Please return to your cabinet.')
            return HttpResponse(message,status=fault.status,headers={'Cache-Control':'no-store'})
    return wrapped


def _query(request,names,required):
    ga._require(set(request.GET)<=names and required<=set(request.GET)
        and all(len(request.GET.getlist(key))==1 for key in request.GET),'CONTRACT_INVALID',422)
    return {key:str(ga._uuid(value)) for key,value in request.GET.items()}


def _form(kind,values,enabled=True):
    return {'form_action':'/cabinet/taste/history/'+kind,'fields':values,'enabled':enabled}


def _redirect(profile_ref,checkpoint_ref,**query):
    return HttpResponse(status=303,headers={'Cache-Control':'no-store',
        'Location':'/cabinet/taste/history/'+checkpoint_ref+'?'+urlencode({'profile_ref':profile_ref,**query})})


@require_GET
@_errors
def page(request):
    value=_query(request,{'profile_ref','cursor'},{'profile_ref'})
    result=TasteHistory(request.portable_authority).index(request.session.get('portable_session'),**value)
    return render_taste_history(csrf_token=get_token(request),**{key:result[key] for key in
        ('profile_ref','checkpoints','timezone_name','next_cursor')})


@require_GET
@_errors
def compare(request,checkpoint_ref):
    value=_query(request,{'profile_ref','preview_ref','receipt_ref'},{'profile_ref'})
    profile=value['profile_ref'];checkpoint=str(checkpoint_ref)
    history=TasteHistory(request.portable_authority);session=request.session.get('portable_session')
    result=history.compare(session,profile_ref=profile,checkpoint_ref=checkpoint)
    facts={key:result[key] for key in ('profile_ref','checkpoint','changes','timezone_name')}
    facts['preview_form']=_form('preview',dict(profile_ref=profile,checkpoint_ref=checkpoint,
        expected_state_digest=result['current_state_digest'],command_id=str(uuid.uuid4())),result['restore_available'])
    if 'preview_ref' in value:
        preview=history.preview_projection(session,profile_ref=profile,preview_ref=value['preview_ref'])
        ga._require(preview['checkpoint_ref']==checkpoint,'AUTHORITY_DENIED',403)
        facts.pop('preview_form',None)
        facts.update(result='preview',expires_at=preview['expires_at'],restore_form=_form('restore',
            dict(profile_ref=profile,preview_ref=preview['preview_ref'],preview_digest=preview['preview_digest'],
                 command_id=str(uuid.uuid4()),confirmed='restore_taste_choices'),preview['restore_available']))
    if 'receipt_ref' in value:
        # A receipt UUID supplies no authority. Re-enter the same scope before
        # reading its owned parent and offer Undo only for today's exact state.
        with history._scope(session,profile) as cabinet:
            row=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=value['receipt_ref'],
                kind__in=('taste.restore','taste.undo'),state='committed').first()
            ga._require(row is not None and row.result.get('checkpoint_ref')==checkpoint,'AUTHORITY_DENIED',403)
            facts['result']='restored' if row.kind=='taste.restore' else 'undone'
            if row.kind=='taste.restore' and row.result.get('undo_available'):
                enabled=(result['restore_available'] and result['current_state_digest']==row.result['after_state_digest']
                    and history._authority_digest(cabinet,session)==row.result['authority_after_digest'])
                facts['undo_form']=_form('undo',dict(profile_ref=profile,restore_command_ref=str(row.command_id),
                    expected_state_digest=result['current_state_digest'],command_id=str(uuid.uuid4()),
                    confirmed='undo_taste_restore'),enabled)
    return render_taste_history_compare(csrf_token=get_token(request),**facts)


@require_POST
@csrf_protect
@_errors
def preview(request):
    value=fields(request,PREVIEW)
    result=TasteHistory(request.portable_authority).preview(request.session.get('portable_session'),**value)
    return _redirect(value['profile_ref'],result['checkpoint_ref'],preview_ref=result['preview_ref'])


@require_POST
@csrf_protect
@_errors
def restore(request):
    value=fields(request,RESTORE)
    result=TasteHistory(request.portable_authority).restore(request.session.get('portable_session'),**value)
    return _redirect(value['profile_ref'],result['checkpoint_ref'],receipt_ref=result['restore_command_ref'])


@require_POST
@csrf_protect
@_errors
def undo(request):
    value=fields(request,UNDO)
    result=TasteHistory(request.portable_authority).undo(request.session.get('portable_session'),**value)
    return _redirect(value['profile_ref'],result['checkpoint_ref'],receipt_ref=result['undo_command_ref'])


@require_http_methods(['GET','HEAD'])
@_errors
def style(request):
    return _asset('taste_history.css','text/css; charset=utf-8')
