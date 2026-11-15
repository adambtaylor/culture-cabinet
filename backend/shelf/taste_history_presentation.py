"""Pure display of currently permitted taste history; no snapshot reconstruction.

Native callers must enforce current profile/view/recovery authority before passing
these facts. This module cannot grant restore, infer a checkpoint or fetch a note.
"""
import re
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from django.http import HttpResponse
from django.template.loader import render_to_string


SIGNALS = {'interest': 'Interest', 'saved': 'Favourite',
           'enjoyment': 'Experienced enjoyment', 'note': 'Preference note'}
KEPT_CURRENT = ('Constraints and visibility, erasure and restrictions',
                'Familiarity, completion, opened and skipped history',
                'Legacy and imported statements; separate saved-library lists',
                'Location and Noise', 'Credentials, permissions and acquisition jobs',
                'Provider attempts and their recorded outcomes')
FORM_FIELDS = {
    'preview': {'profile_ref', 'checkpoint_ref', 'expected_state_digest', 'command_id'},
    'restore': {'profile_ref', 'preview_ref', 'preview_digest', 'command_id', 'confirmed'},
    'undo': {'profile_ref', 'restore_command_ref', 'expected_state_digest', 'command_id', 'confirmed'},
}
CONFIRMATIONS = {'restore': ('restore_taste_choices', 'restore_available_choices'),
                 'undo': ('undo_taste_restore',)}


def _text(value, maximum=240):
    if type(value) is not str or len(value) > maximum:
        raise ValueError('Invalid history presentation text')
    return value


def _ref(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('Invalid history reference')
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise ValueError('Invalid history digest')
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError('Invalid history boolean')
    return value


def noise_control_context(next_noise):
    """An admitted next-round default, not a live run's mutable setting.

    The native start caller owns admission/persistence. An input edit has no
    service call and cannot change the already frozen run selection policy.
    """
    if type(next_noise) is not int or not 0 <= next_noise <= 100:
        raise ValueError('Invalid next-round Noise')
    return {'value': next_noise, 'minimum': 0, 'maximum': 100, 'step': 1}


def noise_run_context(*, policy_version, noise, categories):
    """Display frozen native run facts, never relabel an old run with default25."""
    if policy_version == 'legacy-ranked/v1':
        if noise is not None or categories:
            raise ValueError('Legacy run has no recorded Noise policy')
        return {'legacy': True, 'value': None, 'categories': []}
    if type(policy_version) is not str or policy_version != 'selection-policy/v1':
        raise ValueError('Invalid recorded selection policy')
    control = noise_control_context(noise)
    if type(categories) is not list:
        raise ValueError('Invalid recorded selection report')
    rows = []
    for row in categories:
        if type(row) is not dict:
            raise ValueError('Invalid recorded category report')
        requested, used = row.get('requested'), row.get('used')
        if type(requested) is not int or type(used) is not int or not 0 <= used <= requested <= 9007199254740991:
            raise ValueError('Invalid recorded exploratory counts')
        reason = _text(row.get('reason'), 500)
        if used < requested and not reason:
            raise ValueError('Missing reduced-variety explanation')
        rows.append({'label': _text(row.get('label'), 80), 'requested': requested, 'used': used, 'reason': reason})
    return {'legacy': False, 'value': control['value'], 'categories': rows}


def _point(row, *, profile_ref, timezone_name):
    if type(row) is not dict:
        raise ValueError('Invalid checkpoint summary')
    ref = _ref(row.get('checkpoint_ref'))
    if _ref(row.get('profile_ref')) != profile_ref:
        raise ValueError('History profile differs')
    captured = _text(row.get('captured_at'), 80)
    instant = datetime.fromisoformat(captured.replace('Z', '+00:00'))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Checkpoint timestamp needs timezone')
    local = instant.astimezone(ZoneInfo(timezone_name))
    return {'checkpoint_ref': ref, 'captured_at': instant.isoformat(),
            'date_label': local.strftime('%d %b %Y, %H:%M:%S %Z (%z)'),
            'origin_label': _text(row.get('origin_label'), 160),
            'current': _bool(row.get('current')), 'complete': _bool(row.get('complete')),
            'coverage_text': _text(row.get('coverage_text'), 500),
            'href': '/cabinet/taste/history/' + ref + '?' + urlencode({'profile_ref': profile_ref})}


def history_context(*, profile_ref, checkpoints, timezone_name='UTC', next_cursor=None):
    """Render only actual checkpoint rows: GET never creates a history point."""
    profile = _ref(profile_ref)
    _text(timezone_name, 80)
    ZoneInfo(timezone_name)
    if type(checkpoints) is not list or len(checkpoints) > 20:
        raise ValueError('History page exceeds its bounded rows')
    rows = [_point(row, profile_ref=profile, timezone_name=timezone_name) for row in checkpoints]
    if len({r['checkpoint_ref'] for r in rows}) != len(rows) or sum(r['current'] for r in rows) > 1:
        raise ValueError('Ambiguous checkpoint page')
    next_href = None
    if next_cursor is not None:
        _text(next_cursor, 2048)
        if not next_cursor or any(ord(c) < 32 for c in next_cursor):
            raise ValueError('Invalid history cursor')
        next_href = '/cabinet/taste/history?' + urlencode({'profile_ref': profile, 'cursor': next_cursor})
    return {'profile_ref': profile, 'checkpoints': rows, 'timezone_name': timezone_name,
            'next_href': next_href, 'kept_current': KEPT_CURRENT,
            'history_href': '/cabinet/taste/history?' + urlencode({'profile_ref': profile}),
            'cabinet_href': '/cabinet?' + urlencode({'profile_ref': profile})}


def _value(signal, value):
    # Absence, false and zero have different original meanings. In particular,
    # do not use truthiness, a new star scale, or a stripped note approximation.
    if value is None:
        return {'text': 'Not set / cleared', 'absent': True, 'note': False}
    if signal == 'saved':
        _bool(value)
        text = 'Favourite' if value else 'Not a favourite'
    elif signal == 'interest':
        if type(value) is not str or value not in ('yes', 'maybe', 'no'):
            raise ValueError('Invalid original interest value')
        # Preserve the original anticipated-interest enum, including Maybe;
        # uncertainty is neither a positive response nor a cleared preference.
        text = {'yes': 'Interested', 'maybe': 'Maybe', 'no': 'Not interested'}[value]
    elif signal == 'enjoyment':
        if type(value) is not int or not -9007199254740991 <= value <= 9007199254740991:
            raise ValueError('Invalid original enjoyment value')
        text = str(value) + ' (original scale)'
    else:
        text = _text(value, 65536)
    return {'text': text, 'absent': False, 'note': signal == 'note'}


def _form(row, kind, *, profile_ref, checkpoint_ref, complete):
    if row is None:
        return None
    if type(row) is not dict or row.get('form_action') != '/cabinet/taste/history/' + kind:
        raise ValueError('Invalid native history form')
    fields = row.get('fields')
    if type(fields) is not dict or set(fields) != FORM_FIELDS[kind]:
        raise ValueError('Invalid native history fields')
    normalized = []
    confirmation = None
    for name, value in fields.items():
        if name == 'confirmed':
            if type(value) is not str or value not in CONFIRMATIONS[kind]:
                raise ValueError('Invalid restore acknowledgement')
            if kind == 'restore' and (value == 'restore_taste_choices') != complete:
                raise ValueError('Restore acknowledgement differs from coverage')
            confirmation = value
            continue
        value = _digest(value) if name.endswith('_digest') else _ref(value)
        if name == 'profile_ref' and value != profile_ref or name == 'checkpoint_ref' and value != checkpoint_ref:
            raise ValueError('History form binding differs')
        normalized.append({'name': name, 'value': value})
    return {'form_action': row['form_action'], 'fields': normalized,
            'enabled': _bool(row.get('enabled')), 'confirmation': confirmation}


def history_compare_context(*, profile_ref, checkpoint, changes, preview_form=None,
                            restore_form=None, undo_form=None, result='none', timezone_name='UTC',
                            expires_at=None):
    """No posted change list: native commands carry references/digests only.

    Services recompute permitted historical/current values and exact coverage.
    Presentation never replays an old projection or chooses a partial subset.
    """
    data = history_context(profile_ref=profile_ref, checkpoints=[checkpoint], timezone_name=timezone_name)
    point = data['checkpoints'][0]
    if type(changes) is not list:
        raise ValueError('Invalid permitted comparison')
    data['changes'] = []
    for row in changes:
        if type(row) is not dict or type(row.get('signal')) is not str or row['signal'] not in SIGNALS:
            raise ValueError('Invalid typed history change')
        if 'historical_value' not in row or 'current_value' not in row:
            raise ValueError('Missing comparison is not an unset preference')
        signal = row['signal']
        data['changes'].append({'target_label': _text(row.get('target_label'), 240),
            'signal_label': SIGNALS[signal], 'historical': _value(signal, row['historical_value']),
            'current': _value(signal, row['current_value'])})
    if type(result) is not str or result not in ('none', 'preview', 'restored', 'undone', 'conflict', 'expired', 'unavailable'):
        raise ValueError('Invalid history result')
    # On conflict/expiry/unavailable, stale mutation controls are not rendered.
    forms_allowed = result not in ('conflict', 'expired', 'unavailable')
    for kind, row in (('preview', preview_form), ('restore', restore_form), ('undo', undo_form)):
        normalized = _form(row, kind, profile_ref=profile_ref, checkpoint_ref=point['checkpoint_ref'], complete=point['complete'])
        data[kind + '_form'] = normalized if forms_allowed else None
    if restore_form is not None and preview_form is not None:
        raise ValueError('Review and restore are separate native states')
    expiry_label = None
    if restore_form is not None:
        expiry = datetime.fromisoformat(_text(expires_at, 80).replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError('Restore preview expiry needs timezone')
        expiry_label = expiry.astimezone(ZoneInfo(timezone_name)).strftime('%d %b %Y, %H:%M:%S %Z (%z)')
    if result == 'unavailable':
        data['changes'] = []
        point.update(origin_label='Checkpoint unavailable', coverage_text='Current access or recovery restrictions apply.')
    data.update(checkpoint=point, result=result, expiry_label=expiry_label)
    return data


def _response(template, data, *, csrf_token, status):
    if type(csrf_token) is not str or not 1 <= len(csrf_token) <= 128:
        raise ValueError('Missing native CSRF')
    if type(status) is not int or status not in (200, 400, 401, 403, 409, 410, 422, 503):
        raise ValueError('Invalid history response status')
    response = HttpResponse(render_to_string(template, {**data, 'csrf_token': csrf_token}), status=status)
    response['Cache-Control'] = 'no-store'
    return response


def render_taste_history(*, csrf_token, status=200, **facts):
    return _response('cabinet/taste_history.html', history_context(**facts), csrf_token=csrf_token, status=status)


def render_taste_history_compare(*, csrf_token, status=200, **facts):
    return _response('cabinet/taste_history_compare.html', history_compare_context(**facts), csrf_token=csrf_token, status=status)
