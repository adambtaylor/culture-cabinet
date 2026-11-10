"""Pure native card-action projection; no request, authority or preference writes."""
from uuid import UUID


ACTIONS = {'like': ('right', 'Like', '✓'), 'dislike': ('left', 'Don’t like', '×'),
           'favourite': ('up', 'Favourite', '♥'), 'obtain': ('down', 'Obtain', '↓')}
FIELDS = ('profile_ref', 'publication_ref', 'publication_revision', 'target_ref',
          'interest_revision', 'saved_revision', 'command_id', 'action')


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
