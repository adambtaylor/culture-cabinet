"""Fixed native action/Undo forms and an owned historical receipt reader."""
from urllib.parse import urlencode
from django.http import HttpResponse
from django.utils.html import format_html
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET,require_POST
from . import grant_authority as ga
from .card_actions import CardAction,CardUndo,CardActions,ACTION_FIELDS,UNDO_FIELDS
from .enhancement_views import _portable_errors
from .portable_setup_views import _form
from .portable_source_views import _revision


def service(request):
    authority=request.portable_authority
    enhance=getattr(authority,'_enhance_service',None)
    ga._require(enhance is not None,'CAPABILITY_UNAVAILABLE',503)
    return CardActions(authority,service=enhance)


def fields(request,names):
    try:size=int(request.META.get('CONTENT_LENGTH') or 0)
    except (ValueError,TypeError):raise ga.AuthorityFault('CONTRACT_INVALID',422) from None
    ga._require(0<=size<=8192 and not request.FILES,'CONTRACT_INVALID',422)
    value=_form(request,names)
    for key in ('interest_revision','saved_revision','publication_revision'):
        if key in value:value[key]=_revision(value[key])
    return value


def redirect(profile_ref,command_id,result):
    obtain=result['obtain']
    if obtain['state'] in ('entry_ready','resume_ready'):
        location=obtain['href']
        ga._require(type(location) is str and location.startswith('/cabinet/obtain/'), 'CONTRACT_INVALID',422)
    else:location='/cabinet?'+urlencode(dict(profile_ref=profile_ref,action_receipt=command_id))
    return HttpResponse(status=303,headers={'Location':location,'Cache-Control':'no-store'})


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def action(request):
    command=CardAction.parse(fields(request,ACTION_FIELDS))
    result=service(request).apply(request.session.get('portable_session'),command)
    return redirect(command.profile_ref,command.command_id,result)


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def undo(request):
    command=CardUndo.parse(fields(request,UNDO_FIELDS))
    result=service(request).undo(request.session.get('portable_session'),command)
    return redirect(command.profile_ref,command.command_id,result)


@require_GET
@_portable_errors
@sensitive_variables()
def receipt(request,command_id):
    from .models import PortableJourneyCommand
    ga._require(not request.GET,'CONTRACT_INVALID',422)
    authority=request.portable_authority;ref=request.session.get('portable_session')
    with ga.recovery_guard(authority,ref),authority._lock:
        installation,session=ga._owner(authority,ref)
        row=PortableJourneyCommand.objects.select_related('cabinet').filter(command_id=command_id,
            cabinet__installation=installation,cabinet__actor_id=session.owner_id,
            cabinet__owner_revision=session.owner_revision,kind__in=['card.action','card.undo'],state='committed').first()
        ga._require(row is not None,'AUTHORITY_DENIED',403)
        profile_ref=str(row.cabinet.profile_ref)
        outcome=service(request).receipt(ref,profile_ref,str(command_id))
        # An expiry-safe personal receipt reveals no old candidate/source/title
        # or obsolete model explanation. The cabinet must resolve a current deck.
        body=format_html('<!doctype html><html lang="en"><meta charset="utf-8"><title>Action receipt</title>'
            '<main><h1>Feedback receipt</h1><p>{}: {}.</p><p>Original outcome: {}. Recommendation ranking: {}.</p>'
            '<a href="{}">Back to cabinet</a></main></html>',outcome['requested_action'],
            outcome['preference_outcome'],outcome['selected_state'],outcome['ranking_state'],
            '/cabinet?'+urlencode({'profile_ref':profile_ref}))
        return HttpResponse(body,headers={'Cache-Control':'no-store'})


def _asset(relative,mime):
    from pathlib import Path
    from django.conf import settings
    path=Path(__file__).resolve().parents[2]/'static'/'cabinet'/relative
    ga._require(path.is_file() and not path.is_symlink() and path.stat().st_size<=65536,
                'CAPABILITY_UNAVAILABLE',503)
    return HttpResponse(path.read_bytes(),content_type=mime,
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


from django.views.decorators.http import require_http_methods


@require_http_methods(['GET','HEAD'])
@_portable_errors
def style(request):
    return _asset('card_actions.css','text/css; charset=utf-8')


@require_http_methods(['GET','HEAD'])
@_portable_errors
def script(request):
    return _asset('card_actions.js','text/javascript; charset=utf-8')


@require_http_methods(['GET','HEAD'])
@_portable_errors
def journey_style(request):
    return _asset('guided_owner_journey.css','text/css; charset=utf-8')
