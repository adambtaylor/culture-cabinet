"""Authenticated, reviewed intake of attributed qualitative observations."""
from hashlib import sha256
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import IntegrityError, OperationalError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import authored_discovery as storage, authored_observations as observations
from shelf.discovery_state import Conflict, public_authorized
from shelf.nonfiction_views import headers
from shelf.provider_views import _profile

MAX_PACKET_BYTES = 128 * 1024
REVIEW_SALT = 'taste-observation-review'


def guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return headers(view(request, *args, **kwargs))
        except (Conflict, ValueError) as error:
            return headers(HttpResponse(str(error) + '\nReload observations before continuing.',
                                        status=409, content_type='text/plain'))
        except (IntegrityError, OperationalError):
            return headers(HttpResponse('Observation storage is unavailable or busy. Reload before retrying.',
                                        status=503, content_type='text/plain'))
    return call


class ReviewForm(forms.Form):
    mode = forms.ChoiceField(choices=[('household', 'household')])
    packet = forms.CharField(max_length=MAX_PACKET_BYTES, strip=False)
    revision = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=200)

    def clean_packet(self):
        packet = self.cleaned_data['packet']
        if len(packet.encode('utf-8')) > MAX_PACKET_BYTES:
            raise forms.ValidationError('Packet exceeds 128 KiB.')
        return packet


class ImportForm(ReviewForm):
    review_token = forms.CharField(max_length=1000)
    confirmed = forms.BooleanField(required=True)


def fields(request, form_type):
    """Bound scalar form data, including worst-case browser URL encoding."""
    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        return None
    if not 0 <= length <= MAX_PACKET_BYTES * 3 + 4096 or request.FILES:
        return None
    form = form_type(request.POST)
    if set(request.POST) - {*form.fields, 'csrfmiddlewaretoken'}:
        return None
    if any(len(request.POST.getlist(key)) != 1 for key in request.POST):
        return None
    if sum(len(key.encode()) + len(value.encode()) for key, value in request.POST.items()) > MAX_PACKET_BYTES + 4096:
        return None
    return form.cleaned_data if form.is_valid() else None


def context(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    public_authorized(request, profile_id)
    ready = storage.available()
    state = storage.owned(request, profile_id, 'household') if ready else None
    rows = observations.project(state.data if state else storage.empty_data())
    return {'selected_profile': profile, 'ready': ready, 'stored': rows,
            'sections': [{'label': 'Stored observations', 'rows': rows}],
            'stored_count': len(rows), 'eligible_count': sum(row['eligible'] for row in rows),
            'excluded_count': sum(not row['eligible'] for row in rows),
            'assistant_count': sum(row['provenance']['actor_origin'] == 'assistant'
                                   or row['reaction'] == 'assistant_suggestion' for row in rows),
            'revision': state.revision if state else 0,
            'nonce': state.nonce if state else storage.initial_nonce(request.user.pk, profile_id, 'household')}


def review_identity(request, profile_id, data):
    # Signing proves that this exact packet was displayed for this actor/profile
    # and state revision. It grants no authority: every POST rechecks the session.
    return [request.user.pk, profile_id, data['revision'], data['nonce'],
            sha256(data['packet'].encode('utf-8')).hexdigest()]


@login_required
@require_GET
@guard
def index(request, profile_id):
    if request.GET:
        return HttpResponse('Unknown observation filter.', status=400, content_type='text/plain')
    return render(request, 'cabinet/taste_observations.html', context(request, profile_id))


@login_required
@require_POST
@guard
def review(request, profile_id):
    page = context(request, profile_id)
    data = fields(request, ReviewForm)
    if data is None:
        return HttpResponse('Invalid bounded observation form.', status=400, content_type='text/plain')
    if data['revision'] != page['revision'] or data['nonce'] != page['nonce']:
        raise Conflict('Stale observation review. Reload.')
    pending = observations.project({'observations': observations.validate_packet(data['packet'], request.user.pk)})
    page.update(pending=pending, packet=data['packet'],
                pending_eligible_count=sum(row['eligible'] for row in pending),
                pending_excluded_count=sum(not row['eligible'] for row in pending),
                pending_assistant_count=sum(row['provenance']['actor_origin'] == 'assistant'
                                            or row['reaction'] == 'assistant_suggestion' for row in pending),
                review_token=signing.dumps(review_identity(request, profile_id, data), salt=REVIEW_SALT))
    page['sections'].insert(0, {'label': 'Pending review — not stored', 'rows': pending})
    return render(request, 'cabinet/taste_observations.html', page)


@login_required
@require_POST
@guard
def confirm(request, profile_id):
    public_authorized(request, profile_id)
    data = fields(request, ImportForm)
    if data is None:
        return HttpResponse('Invalid bounded observation confirmation.', status=400, content_type='text/plain')
    try:
        reviewed = signing.loads(data['review_token'], salt=REVIEW_SALT, max_age=1800)
    except signing.BadSignature:
        raise Conflict('Observation review is missing or expired. Review the packet again.')
    if reviewed != review_identity(request, profile_id, data):
        raise Conflict('The observation packet changed. Review it again.')
    if not storage.available():
        raise Conflict('Observation storage is unavailable.')
    observations.apply(request, profile_id, {
        'operation': 'observations_import', 'mode': 'household',
        **{key: data[key] for key in ('packet', 'revision', 'nonce')}})
    return redirect('taste_observations', profile_id=profile_id)
