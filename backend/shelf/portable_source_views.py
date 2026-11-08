"""Native fixed MusicBrainz configuration, separate consent and artist input."""
from datetime import timedelta
from urllib.parse import urlencode
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from . import grant_authority as ga
from .enhancement_views import _native_cabinet_html, _portable_errors, _cabinet_url
from .models import PortableConnection, PortableDisclosureGrant
from .portable_authority import AuthorityFault
from .portable_setup_views import _form
from .portable_taste_inputs import PortableTasteInputs
from .source_authority import (MUSICBRAINZ, MUSICBRAINZ_ARTIST, MUSICBRAINZ_SOURCES,
                               configure_source, source_identity, _connection)

SOURCE_CHOICES = (('canonical_seed', 'Existing artist seed titles'),
    ('explicit_favourites', 'Explicit artist favourites'),
    ('anticipated_reactions', 'Anticipated artist interest'),
    ('experienced_enjoyment', 'Experienced artist enjoyment'))


def _revision(raw):
    ga._require(type(raw) is str and raw.isascii() and raw.isdecimal() and len(raw) <= 16
        and str(int(raw)) == raw and int(raw) < 2**53, 'CONTRACT_INVALID', 422)
    return int(raw)


def _profile(request):
    ga._require(set(request.GET) == {'profile_ref'} and len(request.GET.getlist('profile_ref')) == 1,
                'CONTRACT_INVALID', 422)
    return str(ga._uuid(request.GET['profile_ref']))


def _redirect(profile_ref, *, artist=False, source_adapter=MUSICBRAINZ):
    route = '/cabinet/taste/artist' if artist else '/cabinet/source/musicbrainz'
    if not artist and source_adapter == MUSICBRAINZ_ARTIST:
        route += '/artist'
    return HttpResponse(status=303, headers={'Location': route + '?' + urlencode({'profile_ref': profile_ref}),
                                            'Cache-Control': 'no-store'})


@require_GET
@_portable_errors
@sensitive_variables()
def musicbrainz(request, *, adapter=MUSICBRAINZ):
    ga._require(adapter in MUSICBRAINZ_SOURCES, 'CONTRACT_INVALID', 422)
    profile_ref = _profile(request)
    authority, ref = request.portable_authority, request.session.get('portable_session')
    with ga.recovery_guard(authority, ref, profile_ref=profile_ref), authority._lock:
        state, _ = ga._bound(authority, ref, profile_ref)
        rows = list(PortableConnection.objects.filter(installation=state.installation,
                    adapter=adapter, removed=False))
        ga._require(len(rows) <= 1, 'REVISION_CONFLICT', 409)
        row = rows[0] if rows else None
        grant = PortableDisclosureGrant.objects.filter(cabinet=state, connection=row,
            purpose='catalogue_lookup').first() if row else None
        binding = _connection(authority, ref, str(row.pk))[1] if row else None
        now = authority.now()
        active = bool(grant and grant.state == 'active' and grant.issued_at <= now < grant.expires_at
                      and grant.config_digest == binding['config_digest'] and grant.config_revision == row.config_revision)
        _, origin, path = source_identity(adapter)
        page = '/cabinet/source/musicbrainz' + ('/artist' if adapter == MUSICBRAINZ_ARTIST else '')
        context = {'source_setup': True, 'profile_ref': profile_ref, 'profile_label': str(state.profile.label)[:80],
            'source_configured': row is not None, 'source_connection_ref': str(row.pk) if row else '',
            'source_revision': row.config_revision if row else 0,
            'source_consent_active': active, 'source_consent_record_active': bool(grant and grant.state == 'active'),
            'source_consent_revision': grant.revision if grant else 0,
            'source_consent_action': 'regrant' if grant else 'grant',
            'source_consent_groups': [dict(SOURCE_CHOICES).get(code, code) for code in grant.allowed_data_classes] if grant else [],
            'source_consent_expires': grant.expires_at.isoformat() if grant else '',
            'source_choices': [{'code': code, 'label': label} for code, label in SOURCE_CHOICES],
            'source_origin': origin, 'source_path': path, 'cabinet_url': _cabinet_url(profile_ref),
            'source_adapter': adapter, 'source_category': 'artist' if adapter == MUSICBRAINZ_ARTIST else 'recording',
            'source_label': 'MusicBrainz artists' if adapter == MUSICBRAINZ_ARTIST else 'MusicBrainz recordings',
            'source_page_url': page + '?' + urlencode({'profile_ref': profile_ref}),
            'source_configure_action': page + '/configure', 'source_consent_action_url': page + '/consent'}
    return _native_cabinet_html(request, context)


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def musicbrainz_configure(request, *, adapter=MUSICBRAINZ):
    ga._require(adapter in MUSICBRAINZ_SOURCES, 'CONTRACT_INVALID', 422)
    value = _form(request, {'profile_ref', 'expected_revision', 'confirmed'})
    ga._require(value['confirmed'] == 'yes', 'CONTRACT_INVALID', 422)
    profile_ref = str(ga._uuid(value['profile_ref']))
    authority, ref = request.portable_authority, request.session.get('portable_session')
    with ga.recovery_guard(authority, ref, profile_ref=profile_ref), authority._lock:
        ga._bound(authority, ref, profile_ref, sensitive=True)
        configure_source(authority, ref, confirmed=True, adapter=adapter,
                         expected_revision=_revision(value['expected_revision']))
    return _redirect(profile_ref, source_adapter=adapter)


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def musicbrainz_consent(request, *, adapter=MUSICBRAINZ):
    ga._require(adapter in MUSICBRAINZ_SOURCES, 'CONTRACT_INVALID', 422)
    required = {'profile_ref', 'source_connection_ref', 'expected_revision', 'action', 'confirmed'}
    flags = {'allow_' + code: code for code, _ in SOURCE_CHOICES}
    supplied = set(request.POST) - {'csrfmiddlewaretoken'}
    ga._require(required <= supplied and not supplied - required - set(flags), 'CONTRACT_INVALID', 422)
    value = _form(request, supplied)
    ga._require(value['confirmed'] == 'yes' and value['action'] in ('grant', 'regrant', 'revoke')
        and all(value[key] == 'yes' for key in supplied & set(flags)), 'CONTRACT_INVALID', 422)
    classes = [flags[key] for key in supplied & set(flags)]
    ga._require(value['action'] != 'revoke' or not classes, 'CONTRACT_INVALID', 422)
    profile_ref, connection_ref = str(ga._uuid(value['profile_ref'])), str(ga._uuid(value['source_connection_ref']))
    authority, ref = request.portable_authority, request.session.get('portable_session')
    with ga.recovery_guard(authority, ref, profile_ref=profile_ref), authority._lock:
        state, session = ga._bound(authority, ref, profile_ref, sensitive=True)
        row, _, _ = _connection(authority, ref, connection_ref)
        ga._require(row.adapter == adapter, 'AUTHORITY_DENIED', 403)
        # This fixed source purpose cannot reuse or create a processor grant.
        ga.change_grant(authority, ref, profile_ref=profile_ref, connection_ref=connection_ref,
            expected_revision=_revision(value['expected_revision']), purpose='catalogue_lookup',
            allowed_data_classes=classes, expires_at=min(authority.now() + timedelta(minutes=5),
                ga._end(authority, session)), action=value['action'])
    return _redirect(profile_ref, source_adapter=adapter)


# The URL chooses the fixed operation. No posted adapter, origin or policy can
# select a source, and each connection keeps its own catalogue-purpose grant.
def musicbrainz_artist(request):
    return musicbrainz(request, adapter=MUSICBRAINZ_ARTIST)


def musicbrainz_artist_configure(request):
    return musicbrainz_configure(request, adapter=MUSICBRAINZ_ARTIST)


def musicbrainz_artist_consent(request):
    return musicbrainz_consent(request, adapter=MUSICBRAINZ_ARTIST)


@require_http_methods(['GET', 'POST'])
@csrf_protect
@_portable_errors
@sensitive_variables()
def artist(request):
    authority, ref = request.portable_authority, request.session.get('portable_session')
    service = PortableTasteInputs(authority)
    if request.method == 'GET':
        profile_ref = _profile(request)
        with ga.recovery_guard(authority, ref, profile_ref=profile_ref), authority._lock:
            state, _ = ga._bound(authority, ref, profile_ref)
            entries = service.read(ref, profile_ref)
            context = {'artist_input': True, 'profile_ref': profile_ref,
                'profile_label': str(state.profile.label)[:80], 'entry_ref': str(uuid.uuid4()),
                'newmutation_id': str(uuid.uuid4()), 'base_revision': 0,
                'artist_entries': entries, 'cabinet_url': _cabinet_url(profile_ref)}
        return _native_cabinet_html(request, context)
    base = {'profile_ref', 'entry_ref', 'mutation_id', 'base_revision', 'operation', 'signal'}
    operation = request.POST.get('operation')
    fields = base | ({'title', 'value'} if operation == 'set' else {'reverses_id'})
    if operation == 'set' and 'experienced_ack' in request.POST:
        fields.add('experienced_ack')
    value = _form(request, fields)
    ga._require('experienced_ack' not in value or value['experienced_ack'] == 'yes', 'CONTRACT_INVALID', 422)
    profile_ref = str(ga._uuid(value['profile_ref']))
    raw = value.get('value')
    if operation == 'set' and value['signal'] == 'enjoyment':
        ga._require(raw in ('1', '2', '3', '4', '5'), 'CONTRACT_INVALID', 422)
        raw = int(raw)
    service.mutate(ref, profile_ref, entry_ref=value['entry_ref'], mutation_id=value['mutation_id'],
        base_revision=_revision(value['base_revision']), operation=value['operation'], signal=value['signal'],
        title=value.get('title'), value=raw, experienced_ack=value.get('experienced_ack') == 'yes',
        reverses_id=value.get('reverses_id'))
    return _redirect(profile_ref, artist=True)
