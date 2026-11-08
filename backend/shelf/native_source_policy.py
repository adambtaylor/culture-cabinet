"""Select explicit host-approved source evidence through the existing port.

This module neither discovers approval nor activates a source. Typed entries
are a trusted startup input; credentials and HTTP data cannot supply them.
SourceAuthority still owns disclosure grants, permits and fresh effect checks.
"""
from dataclasses import replace
from types import MappingProxyType

from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .enhance_contract import require, utc
from .source_authority import (ADAPTER, MEDIA, MUSICBRAINZ_SOURCES, APPLE_PODCAST, SourcePolicyProvider, SourceRights,
                               _connection)
from .source_evidence import FieldPolicy


class _NativeSourcePolicy(SourcePolicyProvider):
    def __init__(self, authority, approved_policies):
        ga._require(type(authority) is ga.PortableAuthority, 'AUTHORITY_DENIED')
        require(type(approved_policies) is dict)
        entries = {}
        for adapter, rights in approved_policies.items():
            require(type(adapter) is str and adapter in (ADAPTER, APPLE_PODCAST, *MUSICBRAINZ_SOURCES, *MEDIA))
            require(type(rights) is SourceRights and type(rights.policy) is FieldPolicy)
            require(rights.source == ('moshtix' if adapter == ADAPTER else adapter),
                    'AUTHORITY_DENIED')
            # Copy frozen values, rather than retaining the caller's mutable
            # mapping. Never refresh evidence timestamps or widen field uses.
            entries[adapter] = replace(rights, policy=replace(rights.policy))
        self._authority = authority
        self._entries = MappingProxyType(entries)

    @sensitive_variables()
    def current(self, authority, session_ref, *, profile_ref, connection_ref, now):
        ga._require(authority is self._authority, 'AUTHORITY_DENIED')
        now = utc(now)
        # Resolve the connection using the existing owner/credential binding,
        # under the same lock and recovery ordering as SourceAuthority. Reuse
        # its profile check; do not reconstruct grants or mint a second permit.
        with ga.recovery_guard(authority, session_ref, profile_ref=profile_ref), authority._lock:
            ga._bound(authority, session_ref, profile_ref)
            row, _, _ = _connection(authority, session_ref, connection_ref)
            rights = self._entries.get(row.adapter)
            require(rights is not None, 'CAPABILITY_UNAVAILABLE')
            return rights.validate(now)


def build_native_source_policy(authority, *, approved_policies):
    """Build a source-policy port, without credentials, I/O or activation.

    Keys are native adapter IDs (including ``moshtix_public``); values are
    existing SourceRights with actual host-approved evidence. An empty mapping
    is an unavailable configuration, never evidence of working collection.
    """
    return _NativeSourcePolicy(authority, approved_policies)
