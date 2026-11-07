"""Live, request-local portable authority for existing Save/Obtain services.

This is not a Django login or a provider grant. Selected-profile generations live
with the actual process-bound PortableAuthority, not a stale request.session copy.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import secrets

from django.core import signing
from django.contrib.auth import get_user_model
from django.db import connection

from . import grant_authority as ga
from .portable_authority import AuthorityFault, PortableAuthority

_ACTIVE = ContextVar('portable_saved_access', default=None)
_SALT = 'portable-saved-selection/v1'


@dataclass(frozen=True)
class BoundIdentity:
    installation_ref: str
    owner_revision: int
    profile_ref: str
    actor_id: int
    profile_id: int


def _selections(authority):
    if not hasattr(authority, '_saved_selections'):
        authority._saved_selections = {}
        # PortableAuthority invokes revokers on logout, replacement and close.
        # This shares its existing lock/session lifetime, not a second auth store.
        authority._revokers.append(lambda ref: authority._saved_selections.pop(ref, None))
    return authority._saved_selections


def _frame(authority, session_ref, profile_ref, *, sensitive=False):
    state, session = ga._bound(authority, session_ref, profile_ref, sensitive=sensitive)
    return state, BoundIdentity(str(state.installation_id), session.owner_revision,
        str(state.profile_ref), session.owner_id, state.profile_id)


def _payload(identity, session_ref, nonce):
    return {**vars(identity), 'session_digest': hashlib.sha256(session_ref.encode()).hexdigest(), 'nonce': nonce}


def select_portable_profile(authority, session_ref, *, profile_ref):
    """Navigation-only selection, called exclusively by the native CSRF POST."""
    ga._require(type(authority) is PortableAuthority)
    with ga.recovery_guard(authority, session_ref), authority._lock:
        _, identity = _frame(authority, session_ref, profile_ref)
        nonce = secrets.token_hex(32)
        _selections(authority)[session_ref] = (identity, nonce)
        return {'profile_ref': identity.profile_ref, 'nonce': nonce}


def selected_context(authority, session_ref, selection):
    """Read an already selected frame; a GET cannot select or rotate anything."""
    ga._require(type(authority) is PortableAuthority)
    ga._require(type(selection) is dict and set(selection) == {'profile_ref', 'nonce'})
    with ga.recovery_guard(authority, session_ref), authority._lock:
        _, identity = _frame(authority, session_ref, selection['profile_ref'])
        live = getattr(authority, '_saved_selections', {}).get(session_ref)
        ga._require(live == (identity, selection['nonce']), 'SELECTION_STALE', 409)
        return signing.dumps(_payload(identity, session_ref, selection['nonce']), salt=_SALT)


class PortableSavedAccess:
    def __init__(self, authority, session_ref, profile_ref, selection_token, *, mutate):
        self.authority, self.session_ref = authority, session_ref
        self.profile_ref, self.selection_token = str(profile_ref), selection_token
        self.mutate, self.closed, self.identity = mutate, False, None
        try:
            ga._require(type(selection_token) is str and len(selection_token) <= 2048)
            self._signed = signing.loads(selection_token, salt=_SALT, max_age=1800)
        except (signing.BadSignature, TypeError, ValueError):
            raise AuthorityFault('SELECTION_STALE', 409) from None

    def _check(self, *, actor_id=None, profile_id=None, action='view'):
        ga._require(not self.closed and _ACTIVE.get() is self)
        ga._require(action in {'view', 'select', 'mutate', 'export'})
        state, identity = _frame(self.authority, self.session_ref, self.profile_ref,
                                sensitive=self.mutate or action == 'mutate')
        live = getattr(self.authority, '_saved_selections', {}).get(self.session_ref)
        ga._require(live is not None and live[0] == identity
            and self._signed == _payload(identity, self.session_ref, live[1]), 'SELECTION_STALE', 409)
        ga._require((actor_id is None or actor_id == identity.actor_id)
                    and (profile_id is None or profile_id == identity.profile_id))
        if self.identity is not None:
            ga._require(identity == self.identity, 'SELECTION_STALE', 409)
        if action == 'export':
            from .models import Grant
            ga._require(Grant.objects.filter(user_id=identity.actor_id, profile_id=identity.profile_id,
                                            can_export=True).exists())
        if self.mutate or action == 'mutate':
            ga._require(state.data_schema_version == 2, 'CAPABILITY_UNAVAILABLE', 503)
        self.identity = identity
        return identity

    def require(self, *, actor_id, profile_id, action='save'):
        action = 'mutate' if action == 'save' else action
        with ga.recovery_guard(self.authority, self.session_ref), self.authority._lock:
            return self._check(actor_id=actor_id, profile_id=profile_id, action=action)

    @property
    def actor(self):
        identity = self.require(actor_id=self.identity.actor_id, profile_id=self.identity.profile_id, action='view')
        return get_user_model().objects.get(pk=identity.actor_id)

    @contextmanager
    def effect_window(self, *, action):
        # No outer atomic: the pipeline must commit unknown BEFORE a possible
        # send. A failed exit check never rolls that receipt back into retryable.
        with ga.recovery_guard(self.authority, self.session_ref), self.authority._lock:
            self._check(action=action)
            ga._require(not connection.in_atomic_block, 'CAPABILITY_UNAVAILABLE', 503)
            yield self.identity
            self._check(action=action)


@contextmanager
def portable_saved_scope(authority, session_ref, *, profile_ref, selection_token, mutate=False):
    ga._require(type(authority) is PortableAuthority and type(mutate) is bool)
    access = PortableSavedAccess(authority, session_ref, profile_ref, selection_token, mutate=mutate)
    token = _ACTIVE.set(access)
    try:
        with ga.recovery_guard(authority, session_ref), authority._lock:
            access._check()
        # Do not retain the owner lock across an entire request: another request
        # can switch selection during caps/retrieval, and the next check sees it.
        yield access
    finally:
        access.closed = True
        _ACTIVE.reset(token)


def active_portable_access():
    access = _ACTIVE.get()
    ga._require(access is None or type(access) is PortableSavedAccess)
    return access


def effect_for(actor, profile_id, *, action='select'):
    """Preserve legacy policy; portable calls share the live enclosing authority."""
    access = active_portable_access()
    if access is None:
        return nullcontext()
    access.require(actor_id=actor.pk, profile_id=profile_id, action=action)
    return access.effect_window(action=action)
