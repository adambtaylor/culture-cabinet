"""Pure native card-action projection; no request, authority or preference writes."""
from uuid import UUID


ACTIONS = {'like': ('right', 'Like', '✓'), 'dislike': ('left', 'Don’t like', '×'),
           'favourite': ('up', 'Favourite', '♥'), 'obtain': ('down', 'Obtain', '↓')}
FIELDS = ('profile_ref', 'publication_ref', 'publication_revision', 'target_ref',
          'interest_revision', 'saved_revision', 'command_id', 'action')


def render_card_action_refusal(*, operation, code, status):
    """Render only the native adapter's closed, primitive refusal facts."""
    from django.http import HttpResponse
    from django.template.loader import render_to_string
    from .enhance_contract import SAFE_CODES

    if (type(operation) is not str or operation not in ('response', 'undo')
            or type(code) is not str or code not in SAFE_CODES
            or type(status) is not int or not 400 <= status <= 599):
        raise ValueError('Invalid card refusal presentation')
    # A shared error code/status cannot prove whether an earlier effect exists.
    # Do not infer "not saved", an auth subtype, or a safe replay from this page.
    # No request means no request context or context processors enter rendering.
    html = render_to_string('cabinet/card_action_refusal.html', {
        'undo': operation == 'undo',
    })
    response = HttpResponse(html, status=status)
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def _text(value, maximum=240):
    if type(value) is not str or len(value) > maximum:
        raise ValueError('Invalid card presentation text')
    return value


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('Invalid card presentation reference')
    return value


def _revision(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError('Invalid card presentation revision')
    return value


def card_action_refresh_context(*, profile_ref, interaction, receipt=None,
                                command_id=None, read_available=True):
    """Separate an owned historical outcome from the fresh deck's authority.

    Callers obtain the receipt through CardActions.receipt for this profile.
    A recorded response can survive an unavailable deck; its history must never
    supply a publication or authorize animation of stale cards.
    """
    result = dict(schema='card-action-refresh/v1', profile_ref=_uuid(profile_ref),
                  publication_ref=None, publication_revision=None,
                  read_state='unavailable', receipt=None)
    if type(interaction) is not dict or type(read_available) is not bool:
        raise ValueError('Invalid current card read')
    if read_available and interaction.get('published') is True:
        result.update(publication_ref=_uuid(interaction.get('publication_ref')),
                      publication_revision=_revision(interaction.get('publication_revision'), minimum=1),
                      read_state='current')
    if receipt is None:
        if command_id is not None:
            raise ValueError('Missing owned card receipt')
        return result
    if type(receipt) is not dict or command_id is None:
        raise ValueError('Invalid owned card receipt')
    projected = {key: _uuid(receipt.get(key)) for key in ('command_id', 'target_ref')}
    if projected['command_id'] != _uuid(command_id):
        raise ValueError('Card receipt command differs')
    enums = {'requested_action': ('like', 'dislike', 'favourite', 'obtain', 'undo'),
             'preference_outcome': ('committed', 'unchanged'),
             'selected_state': ('none', 'like', 'dislike', 'favourite'),
             'ranking_state': ('none', 'fresh', 'feedback_pending')}
    for key, choices in enums.items():
        value = receipt.get(key)
        if type(value) is not str or value not in choices:
            raise ValueError('Invalid card receipt outcome')
        projected[key] = value
    projected.update({key: _revision(receipt.get(key)) for key in ('interest_revision', 'saved_revision')})
    result['receipt'] = projected
    return result


def card_action_context(projection):
    """Allowlist server facts into one native four-button form.

    The command identity is shared by the four choices: one explicit selection
    submits it once, and the backend owns replay, CAS, weighting and Obtain's
    Favourite transition. Presentation never composes two preference requests.
    """
    if type(projection) is not dict or projection.get('schema') != 'native-card-actions/v1':
        raise ValueError('Invalid card action projection')
    data = {key: _uuid(projection.get(key)) for key in ('profile_ref', 'publication_ref', 'target_ref')}
    data['publication_revision'] = _revision(projection.get('publication_revision'), minimum=1)
    for key in ('interest_revision', 'saved_revision'):
        data[key] = _revision(projection.get(key))
    selected, ranking = projection.get('selected_state'), projection.get('ranking_state')
    if type(selected) is not str or selected not in ('none', 'dislike', 'like', 'favourite'):
        raise ValueError('Invalid selected preference')
    if type(ranking) is not str or ranking not in ('none', 'fresh', 'feedback_pending'):
        raise ValueError('Invalid ranking state')
    rows = projection.get('actions')
    if type(rows) is dict and set(rows) == set(ACTIONS):
        rows = [{**value, 'action': key} for key, value in rows.items() if type(value) is dict]
    if type(rows) is not list or len(rows) != 4:
        raise ValueError('Four native card actions required')
    normalized, common = {}, None
    for row in rows:
        if type(row) is not dict or type(row.get('action')) is not str or row['action'] not in ACTIONS:
            raise ValueError('Invalid card action')
        action = row['action']
        if action in normalized or row.get('direction') != ACTIONS[action][0]:
            raise ValueError('Invalid action direction')
        if row.get('form_action') != '/cabinet/actions/' or type(row.get('enabled')) is not bool:
            raise ValueError('Invalid native card form')
        fields = row.get('fields')
        if type(fields) is not dict or set(fields) != set(FIELDS) or fields.get('action') != action:
            raise ValueError('Invalid native action fields')
        command = {key: _uuid(fields[key]) for key in ('profile_ref', 'publication_ref', 'target_ref', 'command_id')}
        for key in ('publication_revision', 'interest_revision', 'saved_revision'):
            command[key] = _revision(fields[key], minimum=1 if key == 'publication_revision' else 0)
        if any(command[key] != data[key] for key in data):
            raise ValueError('Card form binding differs')
        if common is not None and common != command:
            raise ValueError('Four choices must share one native command')
        common = command
        reason = _text(row.get('disabled_reason', ''))
        if reason and reason.replace('_', '').isupper():
            reason = {'UNAUTHENTICATED': 'Confirm your owner session before choosing an action.',
                      'AUTHORITY_DENIED': 'Current permissions do not allow this action.',
                      'RUN_STALE': 'This card is no longer current. Review your cabinet.',
                      'SOURCE_UNAVAILABLE': 'The source is unavailable for this action.'}.get(reason,
                          'This action is not currently available. Review your cabinet.')
        obtain = row.get('obtain_state', 'not_requested')
        if type(obtain) is not str or obtain not in ('not_requested', 'navigation_pending', 'entry_ready',
                                                    'resume_ready', 'unavailable', 'blocked'):
            raise ValueError('Invalid Obtain presentation state')
        normalized[action] = dict(action=action, direction=ACTIONS[action][0], label=ACTIONS[action][1],
            icon=ACTIONS[action][2], enabled=row['enabled'], disabled_reason=reason, obtain_state=obtain,
            selected=selected == action)
    receipt = projection.get('receipt_href')
    if receipt is not None:
        _text(receipt, 100)
        if not receipt.startswith('/cabinet/actions/') or not receipt.endswith('/'):
            raise ValueError('Invalid receipt link')
        _uuid(receipt.removeprefix('/cabinet/actions/').removesuffix('/'))
    undo = projection.get('undo')
    undo_data = None
    if undo is not None:
        if type(undo) is not dict or undo.get('form_action') != '/cabinet/actions/undo/' or type(undo.get('enabled')) is not bool:
            raise ValueError('Invalid native Undo form')
        fields = undo.get('fields')
        if type(fields) is not dict or set(fields) != {'profile_ref', 'original_command_id', 'command_id', 'interest_revision', 'saved_revision'}:
            raise ValueError('Invalid Undo fields')
        values = {key: _uuid(fields[key]) for key in ('profile_ref', 'original_command_id', 'command_id')}
        values.update({key: _revision(fields[key]) for key in ('interest_revision', 'saved_revision')})
        if values['profile_ref'] != data['profile_ref']:
            raise ValueError('Undo profile differs')
        undo_data = {'enabled': undo['enabled'], 'fields': [{'name': key, 'value': value} for key, value in values.items()]}
    return {**data, 'schema': 'native-card-actions/v1', 'form_action': '/cabinet/actions/',
            'fields': [{'name': key, 'value': value} for key, value in common.items()],
            'actions': [normalized[key] for key in ACTIONS], 'selected_state': selected,
            'selected_label': {'none': 'No response yet', 'like': 'Like', 'dislike': 'Don’t like', 'favourite': 'Favourite'}[selected],
            'ranking_state': ranking, 'receipt_href': receipt,
            'pending_receipt_href': '/cabinet/actions/' + common['command_id'] + '/', 'undo': undo_data}


def local_event_display_context(projection, *, candidate_key, category, profile_ref,
                                publication_ref, publication_revision):
    """Present the current service-owned local overlay without recomputing it.

    The native caller must obtain it under current rights/location/dataset/CAS
    checks. Compare the independent card/profile/publication bindings here; the
    local digest is a display binding, never new authority or a source fact.
    """
    if type(projection) is not dict or projection.get('schema') != 'local-event-display/v1':
        raise ValueError('Invalid local event display')
    if type(category) is not str or category not in ('event', 'gallery', 'art_fair', 'community'):
        raise ValueError('Local display is not available for this category')
    expected = {'candidate_key': _text(candidate_key, 300), 'profile_ref': _uuid(profile_ref),
                'publication_ref': _uuid(publication_ref),
                'publication_revision': _revision(publication_revision, minimum=1)}
    if not expected['candidate_key']:
        raise ValueError('Missing local display candidate')
    for key, value in expected.items():
        supplied = (_revision(projection.get(key), minimum=1) if key == 'publication_revision' else
                    _uuid(projection.get(key)) if key.endswith('_ref') else _text(projection.get(key), 300))
        if supplied != value:
            raise ValueError('Local event display binding differs')
    digest = _text(projection.get('selection_context_digest'), 64)
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid local selection digest')
    dataset = projection.get('dataset_revision')
    if dataset is not None:
        _text(dataset, 240)
    location_revision = _revision(projection.get('location_revision'))
    refs = projection.get('source_refs')
    if type(refs) is not list or len(refs) > 40:
        raise ValueError('Invalid permitted local source references')
    refs = [_text(ref, 300) for ref in refs]
    choices = {
        'distance_basis': {'coordinate': 'Venue coordinates listed by the source',
            'postcode': 'Venue postcode reference from the local dataset',
            'locality': 'Venue locality reference from the local dataset',
            'unknown': 'Location reference unavailable'},
        'radius_band': {'in_radius': 'Within your preferred radius (approximate).',
            'outside_radius': 'Beyond your preferred radius; still shown.',
            'unknown': 'Radius comparison unavailable.'},
        'opening_status': {'unknown': 'Opening or exhibition timing is not confirmed.',
            'upcoming_opening': 'Source dates indicate an upcoming opening.',
            'current_exhibition': 'Source dates indicate a current exhibition run.',
            'past_exhibition': 'Source dates indicate a past exhibition run.',
            'past_opening': 'The listed opening is in the past; check the exhibition run separately.'},
        'artist_match_status': {'none': '', 'name_match_unconfirmed': 'Unconfirmed artist-name match'},
    }
    data = {**expected, 'schema': 'local-event-display/v1',
        'presentation_schema': 'local-event-display-presentation/v1', 'dataset_revision': dataset,
        'location_revision': location_revision, 'selection_context_digest': digest, 'source_refs': refs,
        'distance_label': _text(projection.get('distance_label'), 240)}
    for key, labels in choices.items():
        value = projection.get(key)
        if type(value) is not str or value not in labels:
            raise ValueError('Invalid local display state')
        data[key] = value
        data[key + '_text'] = labels[value]
    # Keep opening instants and the exhibition calendar run distinct. Missing
    # dates stay absent; neither retrieval time nor run start fills an opening.
    for key in ('opening_starts_at', 'opening_ends_at', 'exhibition_starts_on',
                'exhibition_ends_on', 'date_precision', 'timezone'):
        value = projection.get(key)
        data[key] = None if value is None else _text(value, 120)
    label = _text(projection.get('artist_match_label'), 240)
    data['artist_match_label'] = label if data['artist_match_status'] == 'name_match_unconfirmed' else ''
    return data
