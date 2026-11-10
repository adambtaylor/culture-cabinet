"""Presentation only: Root authorizes and projects state; no request secrets here.

``setup`` calls Root's ``setup_context(request)``. Root's HTML POST handlers
can call ``render_setup(context, status=...)`` for the same form
states. Neither entry point supplies a request to the template engine: adding a
request context processor later must not turn a failed secret POST into output.
"""
from pathlib import Path
from uuid import UUID

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET


NOTICES = {
    'AUTHORITY_DENIED': 'The code or passphrase was not accepted. Check the terminal code and try again. Secret fields have been cleared.',
    'UNAUTHENTICATED': 'Your owner session has ended. Log in again; your model configuration is retained.',
    'REAUTHENTICATION_REQUIRED': 'Confirm your owner passphrase below, then retry your intended action. Your session is still open; secret fields have been cleared.',
    'CONTRACT_INVALID': 'Review the fields below and try again. Secret fields have been cleared.',
    'REVISION_CONFLICT': 'This configuration changed. Review the current setup before submitting again.',
    'RATE_LIMITED': 'Too many attempts. Wait before trying again; no automatic retry will run.',
    'CAPABILITY_UNAVAILABLE': 'Setup is temporarily unavailable. No replacement connection or model was selected.',
    'CREDENTIAL_REQUIRED': 'Add an API key, or unlock its saved encrypted key, before previewing a connection test.',
    'UNKNOWN_RECONCILIATION_REQUIRED': 'A previous test outcome is unknown. Reconcile that attempt before sending another potentially paid request.',
    'CONNECTION_SELECTION_REQUIRED': 'More than one model configuration is saved. Select an owned connection explicitly before continuing; none was chosen automatically.',
}
SUCCESS_NOTICES = {
    'OWNER_CREATED': 'Local owner created. Your model connection is ready to configure.',
    'OWNER_UNLOCKED': 'Your owner session is confirmed. Your saved configuration is available. Retry your intended action explicitly; no key or connection test was resubmitted.',
    'SIGNED_OUT': 'You are logged out. Your saved configuration and encrypted keys are retained on this computer.',
    'CONFIG_SAVED': 'Model configuration saved. Generation has not been verified.',
    'KEY_ADDED': 'Session-only API key updated. It is not stored with the configuration.',
    'KEY_SAVED': 'Encrypted API key saved on this computer. You can close the entry terminal. Provider access has not been verified by saving it.',
    'KEY_REMOVAL_PENDING': 'Removal pending. The key is disabled in Cabinet; deletion from the encrypted store has not been confirmed.',
    'KEY_REMOVED': 'API key removed from Cabinet. Model configuration is retained.',
    'TEST_PASSED': 'Connection test finished. Review the current connection status below; personal Run remains unavailable.',
}
ACTIONS = {name: '/setup/form/' + name for name in
           ('claim', 'login', 'logout', 'reauthenticate', 'configure', 'credential', 'remove', 'preview', 'confirm')}
STATUS_COPY = {
    'unconfigured': 'Connection not tested. Saving a configuration does not prove that generation works.',
    'missing_credential': 'No API key is currently available to this session. Check the saved-key status below before previewing a connection test.',
    'stale': 'The previous connection check is no longer current. Review the configuration and key before testing again.',
    'isolated_test_only': 'Only a synthetic test result is recorded. Working inference has not been established.',
    'unknown': 'A previous test outcome is unknown. Do not send another test blindly; reconcile the existing attempt.',
    'passed': 'The current connection check passed. Personal cabinet runs are still unavailable in this preview.',
    'denied': 'The provider did not accept the credential or test request. Review the credential and current permission before another test.',
    'unsupported': 'The selected endpoint or model is unsupported. No model was substituted.',
    'unauthorized': 'The provider did not accept the credential. Replace it before considering another test.',
    'exhausted': 'The selected provider reports no available capacity or allowance. No fallback was attempted.',
    'unreachable': 'The selected provider could not be reached. No fallback was attempted.',
    'incompatible': 'The model response did not meet the required connection contract.',
    'failed': 'The connection check failed. Review the selected configuration; no automatic retry will run.',
}


CREDENTIAL_STORE_STATES = {'absent', 'available', 'locked', 'unavailable', 'removal_pending'}


def _reference(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('Invalid presentation reference')
    return value


def _boolean(context, name):
    value = context.get(name, False)
    if type(value) is not bool:
        raise ValueError('Invalid presentation boolean')
    return value


def render_setup(context, *, status=200):
    """Copy only primitive, nonsecret projection fields into an escaped template.

    This allowlist is defense in depth, not a secret scrubber: Root must produce
    the owned projection and exact neutral body. Unknown diagnostic fields and
    arbitrary objects never enter template lookup or string conversion. Plain
    strings are required so a SafeString cannot bypass preview escaping.
    """
    if (type(context) is not dict or type(context.get('state')) is not str
            or context.get('state') not in ('claim', 'login', 'model')):
        raise ValueError('Invalid setup presentation state')
    csrf_token = context.get('csrf_token')
    if type(csrf_token) is not str or not 1 <= len(csrf_token) <= 128:
        raise ValueError('Missing presentation CSRF token')
    if type(status) is not int or status not in (200, 400, 401, 403, 409, 422, 429, 503):
        raise ValueError('Invalid presentation response status')
    state = context['state']
    notice_code = context.get('notice', '')
    if type(notice_code) is not str:
        notice_code = ''
    success_notice = SUCCESS_NOTICES.get(notice_code, '')
    notice = ''
    if notice_code and not success_notice:
        notice = NOTICES.get(notice_code,
            'Setup could not finish. Review the current state and try again; secret fields have been cleared.')
    data = {'state': state, 'csrf_token': csrf_token, 'notice': notice, 'success_notice': success_notice,
            # Native form actions are fixed local routes, never a projected URL.
            'actions': ACTIONS,
            'configured': False, 'credential_configured': False,
            'connection_ready': False, 'run_ready': False, 'reauthentication_required': False,
            'credential_saved': False, 'credential_available': False, 'credential_store_state': 'absent',
            'selection_required': notice_code == 'CONNECTION_SELECTION_REQUIRED'}
    if state == 'model':
        # Only Root can distinguish a live-but-not-recent session from expiry.
        # Confirming its password is a new native command, never a replay of the
        # rejected key/preview/paid-confirmation request or a new owner login.
        data['reauthentication_required'] = _boolean(context, 'reauthentication_required')
        if data['reauthentication_required']:
            if notice_code == 'UNAUTHENTICATED':
                data['notice'] = NOTICES['REAUTHENTICATION_REQUIRED']
            elif notice_code == 'AUTHORITY_DENIED':
                data['notice'] = ('The owner passphrase was not accepted. Confirm it below, '
                                  'then retry your intended action. Secret fields have been cleared.')
        connections = context.get('connections', [])
        if type(connections) is not list:
            raise ValueError('Invalid owned connection choices')
        choices = []
        for row in connections:
            if type(row) is not dict or type(row.get('revision')) is not int or row['revision'] < 1:
                raise ValueError('Invalid owned connection choice')
            choices.append({'connection_ref': _reference(row.get('connection_ref')), 'revision': row['revision']})
        if len({row['connection_ref'] for row in choices}) != len(choices):
            raise ValueError('Duplicate owned connection choices')
        data['connections'] = choices
        data['configured'] = _boolean(context, 'configured')
        data['selection_required'] = data['selection_required'] or (len(choices) > 1 and not data['configured'])
        data['credential_configured'] = _boolean(context, 'credential_configured')
        data['connection_ready'] = _boolean(context, 'connection_ready')
        # Storage availability is a nonsecret backend projection, independent
        # of the current RAM lease, provider validation and profile consent.
        # Never inspect the store or a request to fill a missing presentation field.
        for name in ('credential_saved', 'credential_available'):
            data[name] = _boolean(context, name)
        store_state = context.get('credential_store_state', 'absent')
        if type(store_state) is not str or store_state not in CREDENTIAL_STORE_STATES:
            raise ValueError('Invalid credential store presentation state')
        data['credential_store_state'] = store_state
        if data['credential_available'] and (not data['credential_saved'] or store_state != 'available'):
            raise ValueError('Saved credential availability requires a matching saved state')
        if not data['configured'] and (data['credential_saved'] or data['credential_available']):
            raise ValueError('Saved credential status requires a selected configuration')
        data['credential_use_blocked'] = store_state in ('locked', 'unavailable', 'removal_pending')
        data['credential_save_disabled'] = (data['reauthentication_required']
            or data['credential_use_blocked'])
        # K2's persistent save receipt is distinct from the legacy RAM-only
        # receipt, even when another saved binding already exists.
        if notice_code == 'KEY_SAVED' and not data['credential_saved']:
            data['success_notice'] = ''
            data['notice'] = 'Encrypted key saving is not confirmed. Check the saved-key status before continuing.'
        if store_state == 'removal_pending':
            # A deletion intent disables use immediately; it is not proof that
            # the encrypted record was deleted. Do not announce removal early.
            if notice_code == 'KEY_REMOVED':
                data['success_notice'] = ''
            data['connection_ready'] = False

        if data['configured']:
            revision = context.get('revision')
            if type(revision) is not int or not 1 <= revision <= 999999999999:
                raise ValueError('Invalid presentation revision')
            data.update(connection_ref=_reference(context.get('connection_ref')), revision=revision)
        elif data['credential_configured'] or data['connection_ready']:
            raise ValueError('Connection status requires a saved configuration')
        if data['connection_ready'] and not data['credential_configured']:
            raise ValueError('Connection status requires a current credential')
        outcome = context.get('outcome', 'unconfigured')
        if type(outcome) is not str:
            outcome = 'failed'
        data['status_text'] = STATUS_COPY.get(outcome, STATUS_COPY['failed'])
        data['unknown_outcome'] = (outcome == 'unknown'
                                   or notice_code == 'UNKNOWN_RECONCILIATION_REQUIRED')
        preview = context.get('preview')
        if preview is not None and not data['reauthentication_required'] and not data['credential_use_blocked']:
            if not data['configured'] or not data['credential_configured'] or type(preview) is not dict:
                raise ValueError('Preview requires the current configured connection')
            body = preview.get('body')
            if type(body) is not str or not 1 <= len(body.encode('utf-8')) <= 16384:
                raise ValueError('Invalid neutral preview body')
            data['preview'] = {'body': body, 'preview_ref': _reference(preview.get('preview_ref')),
                               'intent_ref': _reference(preview.get('intent_ref'))}
    # Do not pass request here, even for convenient CSRF/context processing.
    # The explicit token is the only form authority supplied to presentation.
    response = HttpResponse(render_to_string('portable-setup.html', data), status=status)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def setup(request):
    from .portable_setup_views import setup_context
    return render_setup(setup_context(request))


@require_GET
def style(request):
    # This fixed bundle-relative asset path cannot select a caller file or cwd.
    source = Path(__file__).resolve().parents[2] / 'static' / 'portable-setup.css'
    response = HttpResponse(source.read_bytes(), content_type='text/css; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
