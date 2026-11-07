"""Ordinary authenticated server-rendered rehearsal screens; no live account reads."""
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import messages
from shelf.acquisition_contract import Blocked, ContractError
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import sessions
from shelf.message_provider import ACCESS_STATES, CHATS, MESSAGES, SCENARIOS, chat
from shelf.models import MessageDraft
from shelf.provider_views import _profile


class StartForm(forms.Form):
    kind = forms.ChoiceField(choices=[('invitation', 'Invitation'), ('reply', 'Reply')])
    chat_id = forms.ChoiceField(choices=[(c['id'], c['label']) for c in CHATS])
    session_id = forms.ChoiceField(required=False, choices=[(e.session_id, e.start) for e in sessions()])
    message_id = forms.ChoiceField(required=False, choices=[(m['id'], m['id']) for m in MESSAGES])
    scenario = forms.ChoiceField(choices=[(s, s) for s in SCENARIOS])
    access = forms.ChoiceField(choices=[(s, s) for s in ACCESS_STATES])


class ActionForm(forms.Form):
    action = forms.ChoiceField(choices=[(s, s) for s in ('edit', 'refresh', 'approve', 'dispatch', 'reconcile')])
    version = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=64)
    preview = forms.RegexField(r'^[a-f0-9]{64}$')
    confirmed = forms.BooleanField(required=True)
    # The service bounds canonical LF text after CRLF normalization; the whole
    # request is independently capped before Django parses these fields.
    body = forms.CharField(required=False, strip=False)
    sensitive_confirmed = forms.BooleanField(required=False)
    recipient_confirmation = forms.CharField(required=False, max_length=160, strip=False)


def _form(request, cls):
    if len(request.body) > 16384:
        raise Blocked('Form too large.')
    form = cls(request.POST)
    if (set(request.POST) - {*form.fields, 'csrfmiddlewaretoken'}
            or any(len(request.POST.getlist(k)) != 1 for k in request.POST)
            or not form.is_valid()):
        raise Blocked('Invalid message form. Reload and choose the displayed options.')
    return form.cleaned_data


def _guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except (Blocked, ContractError, Conflict) as error:
            return HttpResponse(str(error) + '\nReload the draft to see its retained state.\n',
                                status=409, content_type='text/plain')
        except (IntegrityError, OperationalError):
            return HttpResponse('Storage unavailable or concurrent change. Reload; never repeat an uncertain send.\n',
                                status=409, content_type='text/plain')
    return call


@login_required
@require_GET
def index(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    ready = messages.available()
    drafts = MessageDraft.objects.filter(actor=request.user, profile_id=profile_id) if ready else []
    return render(request, 'cabinet/messages.html', {'selected_profile': profile, 'ready': ready,
        'drafts': drafts, 'chats': CHATS, 'sessions': sessions(), 'scenarios': SCENARIOS,
        'access_states': ACCESS_STATES})


@login_required
@require_GET
@_guard
def selected_context(request, profile_id, chat_id):
    public_authorized(request, profile_id)
    recipient = chat(chat_id)
    return render(request, 'cabinet/message_context.html', {'selected_profile': _profile(request, profile_id),
        'chat': recipient, 'contexts': [m for m in MESSAGES if m['chat_id'] == chat_id],
        'ready': messages.available(), 'scenarios': SCENARIOS, 'access_states': ACCESS_STATES})


@login_required
@require_POST
@_guard
def start(request, profile_id):
    public_authorized(request, profile_id)
    if not messages.available():
        raise Blocked('Messaging rehearsal requires the isolated candidate storage update.')
    draft = messages.create(request, profile_id, **_form(request, StartForm))
    return redirect('message_draft', profile_id=profile_id, draft_id=draft.pk)


@login_required
@require_GET
@_guard
def detail(request, profile_id, draft_id):
    if not messages.available():
        raise Http404
    draft = messages.owned(request, profile_id, draft_id)
    messages.validate(draft)
    return render(request, 'cabinet/message_draft.html', {'selected_profile': draft.profile,
        'draft': draft, 'preview_digest': messages.preview_digest(draft),
        'can_edit': draft.state in ('draft', 'queued'),
        'uncertain': draft.state in ('sending', 'unknown')})


@login_required
@require_POST
@_guard
def action(request, profile_id, draft_id):
    if not messages.available():
        raise Http404
    draft = messages.apply(request, profile_id, draft_id, _form(request, ActionForm))
    return redirect('message_draft', profile_id=profile_id, draft_id=draft.pk)
