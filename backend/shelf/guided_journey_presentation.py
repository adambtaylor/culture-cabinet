"""Allowlisted presentation for the native guided-owner-journey/v2 port.

Only primitive projections enter this module. Native services own progress,
credentials, provider checks and command receipts; rendering never reads a request.
"""
import re
from uuid import UUID
from urllib.parse import urlsplit, parse_qs

from django.http import HttpResponse
from django.template.loader import render_to_string
from .taste_history_presentation import noise_control_context

STEPS = ('identity', 'providers', 'favourites', 'prepare', 'finish')
A1_STEPS = STEPS[:3] + ('events_location',) + STEPS[3:]
CATEGORIES = ('book', 'movie', 'tv', 'artist', 'recording', 'podcast_episode', 'event',
              'gallery', 'art_fair', 'community', 'regional_day_trip', 'outing', 'dining')
PROVIDERS = ('openai', 'tmdb', 'hardcover', 'musicbrainz', 'apple')
# An owner-stated visual artist is an intake kind, never a fourteenth output
# queue or a MusicBrainz identity. Preserve its spelling and UUID unchanged.
INTAKE_KINDS = CATEGORIES + ('visual_artist',)
CATEGORY_LABELS = {'artist': 'Music artists', 'visual_artist': 'Visual artists',
                   'gallery': 'Exhibitions & openings', 'art_fair': 'Art fairs'}
FIELD_SETS = {
    'name': {'expected_owner_revision', 'label', 'command_id'},
    'configure': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'confirmed'},
    'save': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'secret', 'storage'},
    'test': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'confirmed'},
    'step': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'step', 'action'},
    'prepare': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'connection_ref', 'client_key', 'refresh_nonce'},
    'add': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'category', 'slot', 'entry_ref', 'mutation_id', 'base_revision', 'title'},
    'remove': {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'category', 'slot', 'entry_ref', 'mutation_id', 'base_revision'},
}
STORE_COPY = {'absent': 'No encrypted key saved', 'pending_write': 'Saving is not confirmed yet',
              'saved_available': 'Encrypted key saved and available', 'saved_locked': 'Encrypted key store locked',
              'saved_unavailable': 'Saved key unavailable', 'pending_delete': 'Removal pending; key disabled',
              'removed': 'Key removed'}
TEST_COPY = {'not_tested': 'Not tested yet', 'awaiting_confirmation': 'Review and confirm the test',
             'sending': 'Test in progress; do not start another', 'passed': 'Connection check passed',
             'failed': 'Connection check failed', 'unknown': 'Test outcome unknown; check the recorded attempt',
             'stale': 'Previous check is no longer current', 'not_applicable': 'No API key test is needed'}
CATEGORY_COPY = {'not_started': 'Not prepared yet', 'pending': 'Preparation in progress',
                 'fresh': 'Selections available', 'empty_bounded': 'No selections within this round’s limits',
                 'unavailable': 'Source unavailable', 'failed': 'Collection failed'}


def _text(value, maximum=240):
    if type(value) is not str or len(value) > maximum:
        raise ValueError('Invalid journey presentation text')
    return value


def _ref(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('Invalid journey presentation reference')
    return value


def _number(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError('Invalid journey presentation revision')
    return value


def _boolean(row, name):
    value = row.get(name)
    if type(value) is not bool:
        raise ValueError('Invalid journey presentation boolean')
    return value


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        raise ValueError('Invalid journey presentation state')
    return value


def _groups(rows):
    groups = []
    for key, label, kinds in (('other', 'Other categories', set(CATEGORIES) - {'artist', 'recording', 'gallery', 'art_fair'}),
                              ('music', 'Music', {'artist', 'recording'}),
                              ('art', 'Art', {'visual_artist', 'gallery', 'art_fair'})):
        members = [row for row in rows if row['category'] in kinds]
        if members:
            groups.append({'key': key, 'label': label, 'categories': members})
    return groups


def _href(value):
    if value is None:
        return None
    _text(value, 500)
    parsed = urlsplit(value)
    if any(ord(c) < 32 for c in value) or parsed.scheme or parsed.netloc or parsed.fragment or not value.startswith('/') or value.startswith('//') or '\\' in value:
        raise ValueError('Invalid native journey link')
    if (parsed.path not in ('/setup', '/setup/journey', '/setup/cabinet', '/cabinet', '/enhance')
            and re.fullmatch(r'/enhance/runs/[a-f0-9-]{36}/(?:status|preview)', parsed.path) is None):
        raise ValueError('Unlisted native journey link')
    if parsed.path.startswith('/enhance/runs/'):
        _ref(parsed.path.split('/')[3])
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {'profile_ref', 'connection_ref', 'run_ref', 'category', 'step', 'review'} or any(len(v) != 1 for v in query.values()):
        raise ValueError('Invalid native navigation fields')
    for key, values in query.items():
        if key.endswith('_ref'):
            _ref(values[0])
        elif key == 'category':
            _enum(values[0], CATEGORIES)
        elif key == 'step':
            _enum(values[0], A1_STEPS)
        elif values[0] != '1':
            raise ValueError('Invalid request review link')
    return value


def _form(value, kind, *, bound, provider=None, step_action=None):
    if type(value) is not dict or type(value.get('fields')) is not dict:
        raise ValueError('Invalid native journey form')
    fields = value['fields']
    expected = FIELD_SETS[kind] | ({'noise'} if kind == 'prepare' and 'noise' in fields else set())
    if set(fields) != expected:
        raise ValueError('Invalid native journey fields')
    path = '/cabinet/journey/' + ('favourites/' + kind if kind in ('add', 'remove') else kind)
    if provider:
        path = '/cabinet/journey/providers/' + provider + '/' + kind
    if value.get('form_action') != path:
        raise ValueError('Invalid native journey action')
    enabled = _boolean(value, 'enabled')
    reason = _text(value.get('disabled_reason', ''))
    hidden = []
    for key, raw in fields.items():
        if key == 'noise':
            noise_control_context(raw)
            continue
        if key in ('secret', 'label', 'title', 'confirmed'):
            # Secret input is always newly entered and never retained as a
            # hidden default, including a failed save or replayed save intent.
            if key == 'secret' and raw is not None and (type(raw) is not str or raw != ''):
                raise ValueError('Secret entered presentation')
            if key in ('label', 'title'):
                _text(raw, 80 if key == 'label' else 200)
            continue
        if key.endswith('_ref') or key.endswith('_id') or key in ('client_key', 'refresh_nonce'):
            val = _ref(raw, nullable=not enabled)
        elif key in ('expected_revision', 'expected_owner_revision', 'base_revision'):
            val = _number(raw)
        elif key == 'slot':
            val = _number(raw, 1)
            if val > 5:
                raise ValueError('Too many favourite slots')
        elif key == 'category':
            val = _enum(raw, INTAKE_KINDS)
        elif key == 'step':
            val = _enum(raw, A1_STEPS)
        elif key == 'action':
            val = _enum(raw, ('continue', 'back', 'skip'))
            if val != step_action:
                raise ValueError('Step action differs')
        elif key == 'storage' and type(raw) is str and raw == 'persistent':
            val = raw
        else:
            raise ValueError('Unlisted journey field')
        if enabled and key in ('profile_ref', 'journey_ref', 'expected_revision') and val != bound[key]:
            raise ValueError('Journey form binding differs')
        hidden.append({'name': key, 'value': '' if val is None else val})
    return {'form_action': path, 'fields': hidden, 'enabled': enabled,
            'disabled_reason': reason, 'title': fields.get('title', '')}


def _location_context(value, *, bound):
    if value is None:
        return None
    if type(value) is not dict or type(value.get('actions')) is not dict or set(value['actions']) != {'save', 'skip', 'remove'}:
        raise ValueError('Invalid native location projection')
    state = _enum(value.get('state'), ('unset', 'mapped', 'unmapped', 'dataset_unavailable'))
    country, postcode, radius = value.get('country'), value.get('postcode'), value.get('radius_km')
    if state == 'unset':
        if any(v is not None for v in (country, postcode, radius)):
            raise ValueError('Unset location contains a saved setting')
    elif country != 'AU' or type(country) is not str or type(postcode) is not str or re.fullmatch('[0-9]{4}', postcode) is None:
        raise ValueError('Invalid saved postcode')
    elif type(radius) is not int or not 1 <= radius <= 500:
        raise ValueError('Invalid saved radius')
    dataset = value.get('dataset_revision')
    if dataset is not None:
        _text(dataset, 240)
    result = {'country': country, 'postcode': postcode, 'radius_km': radius,
        'location_revision': _number(value.get('location_revision')), 'state': state,
        'dataset_revision': dataset, 'approximation_notice': _text(value.get('approximation_notice'), 2048),
        'attribution': _text(value.get('attribution'), 2048), 'actions': {}}
    common = {'profile_ref', 'journey_ref', 'expected_revision', 'command_id', 'action'}
    for action, row in value['actions'].items():
        if type(row) is not dict or row.get('form_action') != '/cabinet/journey/location':
            raise ValueError('Invalid native location action')
        fields = row.get('fields')
        expected = common | ({'country', 'postcode', 'radius_km'} if action == 'save' else set())
        if type(fields) is not dict or set(fields) != expected or fields.get('action') != action:
            raise ValueError('Invalid native location fields')
        hidden = []
        for key in ('profile_ref', 'journey_ref', 'expected_revision', 'command_id'):
            normalized = _number(fields[key]) if key == 'expected_revision' else _ref(fields[key])
            if key in bound and normalized != bound[key]:
                raise ValueError('Location form binding differs')
            hidden.append({'name': key, 'value': normalized})
        hidden.append({'name': 'action', 'value': action})
        enabled = _boolean(row, 'enabled')
        if action == 'remove' and state == 'unset' and enabled:
            raise ValueError('No location exists to remove')
        form = {'form_action': '/cabinet/journey/location', 'fields': hidden, 'enabled': enabled,
                'disabled_reason': _text(row.get('disabled_reason', ''))}
        if action == 'save':
            # Use the producer's actual suggested/current fields. Displaying25
            # is not a save; skip carries no location fields and retains state.
            if fields['country'] != 'AU' or type(fields['country']) is not str:
                raise ValueError('Unsupported location country')
            candidate = _text(fields['postcode'], 4)
            if candidate and re.fullmatch('[0-9]{4}', candidate) is None:
                raise ValueError('Invalid location entry default')
            suggested = fields['radius_km']
            if type(suggested) is not int or not 1 <= suggested <= 500:
                raise ValueError('Invalid radius entry default')
            form['fields'].append({'name': 'country', 'value': 'AU'})
            form.update(postcode=candidate, radius_km=suggested)
        result['actions'][action] = form
    return result


def guided_journey_context(projection):
    """Detach only safe display facts; never invent readiness from navigation."""
    if type(projection) is not dict or projection.get('schema') != 'guided-owner-journey/v2':
        raise ValueError('Invalid guided journey projection')
    data = {'profile_ref': _ref(projection.get('profile_ref'), nullable=True),
            'journey_ref': _ref(projection.get('journey_ref'), nullable=True),
            'profile_label': _text(projection.get('profile_label'), 80),
            'revision': _number(projection.get('revision')), 'step': _enum(projection.get('step'), A1_STEPS)}
    bound = {'profile_ref': data['profile_ref'], 'journey_ref': data['journey_ref'], 'expected_revision': data['revision']}
    steps = projection.get('steps')
    if type(steps) is not list or len(steps) not in (len(STEPS), len(A1_STEPS)):
        raise ValueError('Complete journey progress required')
    data['steps'] = []
    for row in steps:
        if type(row) is not dict:
            raise ValueError('Invalid journey progress')
        data['steps'].append({'step': _enum(row.get('step'), A1_STEPS), 'label': _text(row.get('label'), 80),
                              'state': _enum(row.get('state'), ('current', 'completed', 'skipped', 'pending'))})
    expected_steps = A1_STEPS if len(steps) == len(A1_STEPS) else STEPS
    if [row['step'] for row in data['steps']] != list(expected_steps) or data['step'] not in expected_steps:
        raise ValueError('Journey progress order differs')
    actions = projection.get('actions')
    if type(actions) is not dict or type(actions.get('step')) is not dict or set(actions['step']) != {'continue', 'back', 'skip'}:
        raise ValueError('Invalid journey navigation')
    data['name_form'] = _form(actions.get('name'), 'name', bound=bound)
    data['prepare_form'] = _form(actions.get('prepare'), 'prepare', bound=bound)
    # Only a native producer's admitted default enables the new start field.
    # Legacy projections remain unchanged until their caller supports Noise.
    data['noise_control'] = noise_control_context(projection['next_noise']) if 'next_noise' in projection else None
    prepare_noise = actions['prepare']['fields'].get('noise')
    if prepare_noise is not None and (data['noise_control'] is None or prepare_noise != data['noise_control']['value']):
        raise ValueError('Prepare Noise binding differs')
    data['navigation'] = [{**_form(actions['step'][action], 'step', bound=bound, step_action=action),
                           'label': {'back': 'Back', 'skip': 'Skip this step', 'continue': 'Continue'}[action]}
                          for action in ('back', 'skip', 'continue')]
    data['location'] = _location_context(projection.get('location'), bound=bound)
    if data['step'] == 'events_location' and data['location'] is None:
        raise ValueError('Location step needs its actual safe projection')
    providers = projection.get('providers')
    if type(providers) is not list or len(providers) > len(PROVIDERS):
        raise ValueError('Invalid provider choices')
    data['providers'] = []
    for row in providers:
        if type(row) is not dict:
            raise ValueError('Invalid provider projection')
        provider = _enum(row.get('provider_id'), PROVIDERS)
        store, test = _enum(row.get('storage_state'), STORE_COPY), _enum(row.get('test_state'), TEST_COPY)
        saved, available, current = (_boolean(row, k) for k in ('credential_saved', 'credential_available', 'test_current'))
        refs, revisions = row.get('connection_refs'), row.get('config_revisions')
        if type(refs) is not list or type(revisions) is not list or len(refs) != len(revisions):
            raise ValueError('Invalid provider configuration projection')
        for ref, revision in zip(refs, revisions):
            _ref(ref); _number(revision, 1)
        forms = row.get('actions')
        if type(forms) is not dict or set(forms) - {'configure', 'save', 'test'}:
            raise ValueError('Invalid provider form projection')
        normalized = {}
        for kind, form in forms.items():
            if provider in ('musicbrainz', 'apple') and kind in ('save', 'test') or provider == 'openai' and kind == 'test':
                raise ValueError('Provider test uses a separate normal caller')
            normalized[kind] = _form(form, kind, bound=bound, provider=provider)
        data['providers'].append({'provider_id': provider, 'label': _text(row.get('label'), 80),
            'optional': _boolean(row, 'optional'), 'required_for_inference': _boolean(row, 'required_for_inference'),
            'credential_saved': saved, 'credential_available': available, 'storage_state': store, 'storage_text': STORE_COPY[store],
            'test_state': test, 'test_text': (TEST_COPY[test] if test != 'passed' or current else 'Previous check passed; it is no longer current'),
            'test_current': current, 'test_scope': _text(row.get('test_scope', ''), 160),
            'actions': normalized, 'openai_href': '/setup?connection_ref=' + refs[0] if provider == 'openai' and refs else '/setup'})
    if len({row['provider_id'] for row in data['providers']}) != len(data['providers']):
        raise ValueError('Duplicate guided provider')
    favourites = projection.get('favourite_categories')
    if type(favourites) is not list or len(favourites) > len(INTAKE_KINDS):
        raise ValueError('Invalid favourites projection')
    data['favourite_categories'] = []
    for row in favourites:
        if type(row) is not dict or type(row.get('slots')) is not list or len(row['slots']) > 5:
            raise ValueError('Invalid favourite category')
        category, slots = _enum(row.get('category'), INTAKE_KINDS), []
        for slot in row['slots']:
            if type(slot) is not dict or type(slot.get('actions')) is not dict:
                raise ValueError('Invalid favourite slot')
            number = _number(slot.get('slot'), 1)
            if number > 5 or number in [s['slot'] for s in slots]:
                raise ValueError('Invalid favourite slot number')
            entry, revision = _ref(slot.get('entry_ref')), _number(slot.get('current_saved_revision'))
            _ref(slot.get('mutation_id'))
            add = _form(slot['actions'].get('add'), 'add', bound=bound)
            remove = _form(slot['actions'].get('remove'), 'remove', bound=bound)
            for form in (slot['actions']['add'], slot['actions']['remove']):
                fields = form['fields']
                if fields['category'] != category or fields['slot'] != number or fields['entry_ref'] != entry or fields['base_revision'] != revision:
                    raise ValueError('Favourite form binding differs')
            slots.append({'slot': number, 'entry_ref': entry, 'title': _text(slot.get('title'), 200),
                          'removable': _boolean(slot, 'removable'), 'add': add, 'remove': remove})
        label = _text(row.get('label'), 80)
        data['favourite_categories'].append({'category': category, 'label': CATEGORY_LABELS.get(category, label), 'slots': slots})
    if len({row['category'] for row in data['favourite_categories']}) != len(data['favourite_categories']):
        raise ValueError('Duplicate favourite category')
    data['favourite_groups'] = _groups(data['favourite_categories'])
    categories = projection.get('categories')
    if type(categories) is not list or len(categories) != len(CATEGORIES):
        raise ValueError('Every enabled category needs an outcome')
    data['categories'] = []
    for row in categories:
        if type(row) is not dict:
            raise ValueError('Invalid category outcome')
        category = _enum(row.get('category'), CATEGORIES)
        state = _enum(row.get('state'), CATEGORY_COPY)
        label = _text(row.get('label'), 80)
        data['categories'].append({'category': category, 'label': CATEGORY_LABELS.get(category, label),
            'state': state, 'status': CATEGORY_COPY[state], 'count': _number(row.get('count')),
            'href': _href(row.get('href')), 'ranking_state': _enum(row.get('ranking_state'), ('none', 'fresh', 'feedback_pending'))})
    if {row['category'] for row in data['categories']} != set(CATEGORIES):
        raise ValueError('Category outcome missing or repeated')
    data['category_groups'] = _groups(data['categories'])
    preparation = projection.get('preparation')
    if type(preparation) is not dict:
        raise ValueError('Invalid preparation outcome')
    state = _enum(preparation.get('state'), ('not_started', 'pending', 'authorized', 'collecting', 'sealed', 'awaiting_preview',
        'ready', 'reserved', 'in_flight', 'validated', 'staged', 'published', 'blocked', 'no_candidates', 'failed', 'cancelled', 'stale', 'unknown'))
    # Native publication, not a button click, is the completion fact. A partial
    # category outcome stays visible and never becomes an invented recommendation.
    published = state == 'published'
    if published:
        _ref(preparation.get('publication_ref')); _number(preparation.get('publication_revision'), 1)
    partial = _boolean(preparation, 'partial')
    data['preparation'] = {'state': state, 'published': published, 'partial': partial,
        'href': _href(preparation.get('href') or preparation.get('preview_href') or preparation.get('status_href')),
        'text': 'Your first selections are ready.' if published else 'Preparation has not published new selections yet.'}
    receipts = projection.get('completion_receipts')
    if type(receipts) is not list or len(receipts) > 50:
        raise ValueError('Invalid completion summaries')
    data['completion_receipts'] = []
    for row in receipts:
        if type(row) is not dict:
            raise ValueError('Invalid completion summary')
        message = _text(row.get('message', row.get('label', '')), 500)
        if message:
            data['completion_receipts'].append(message)
    return data


def render_guided_journey(projection, *, csrf_token, status=200):
    if type(csrf_token) is not str or not 1 <= len(csrf_token) <= 128:
        raise ValueError('Missing native CSRF token')
    if type(status) is not int or status not in (200, 400, 401, 403, 409, 422, 429, 503):
        raise ValueError('Invalid presentation status')
    data = guided_journey_context(projection)
    if status >= 400:
        data['command_notice'] = ('Your setup changed. Review the current values before trying again.' if status == 409 else
            'That change was not accepted. Review the current values and form instructions. Nothing is retried automatically.')
    # No request/context processors: a failed secret command must never make its
    # body, credential object or request accessible through template resolution.
    response = HttpResponse(render_to_string('cabinet/guided_owner_journey.html', {**data, 'csrf_token': csrf_token}), status=status)
    response['Cache-Control'] = 'no-store'
    return response
