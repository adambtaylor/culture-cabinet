"""Concrete selected-profile state; no private-store discovery or default adoption."""
from dataclasses import asdict
from datetime import datetime

from . import grant_authority as ga, profile_snapshot
from .category_registry import registry_view
from .disclosure import canonical_bytes
from .enhance_contract import EnhanceBindings, EnhanceFault, digest
from .models import PortableConnection, PortableDisclosureGrant, ProfileCabinetState
from .portable_cabinet_store import CurrentState, StateProvider
from .source_authority import SourceAuthority


class CurrentProfile(StateProvider):
    def __init__(self, authority, *, sources=(), registry=None):
        ga._require(type(authority) is ga.PortableAuthority)
        ga._require(type(sources) is tuple and all(type(source) is SourceAuthority
                    and source.authority is authority for source in sources))
        self.authority, self.sources = authority, sources
        self.registry = registry or registry_view()
        ga._require(self.registry == registry_view(self.registry.categories))

    def current(self, authority, session_ref, *, profile_ref, connection_ref):
        ga._require(authority is self.authority)
        with ga.recovery_guard(authority, session_ref, profile_ref=profile_ref), authority._lock:
            state, _ = ga._bound(authority, session_ref, profile_ref)
            ga._require(state.data_schema_version == 2, 'CAPABILITY_UNAVAILABLE', 503)
            view = ga.current_view(authority, session_ref, profile_ref=profile_ref,
                                   connection_ref=connection_ref, purpose='inference')
            snapshot = profile_snapshot.capture(view)
            ga._require(not any(row['verification_required'] for row in snapshot['hard_constraints']),
                        'CAPABILITY_UNAVAILABLE', 503)
            ga._require(not any(row['blocks_use'] for row in snapshot.get('excluded_history', [])),
                        'CAPABILITY_UNAVAILABLE', 503)
            ga._require(not snapshot.get('excluded_history') or bool(snapshot['evidence']),
                        'CAPABILITY_UNAVAILABLE', 503)
            bindings = EnhanceBindings(view.installation_id, view.actor_ref, view.profile_ref,
                view.profile_revision_digest, view.policy_epoch, view.restriction_watermark,
                self.registry.revision, view.context['config_revision'], digest(canonical_bytes(view.grants)),
                digest(canonical_bytes(view.connection_binding)))
            source_views, rights, ends = [], [], [view.expires_at]
            for source in self.sources:
                ga._require(source.session_ref == session_ref and source.profile_ref == str(profile_ref))
                try:
                    current = source._view(); evidence = source._rights(authority.now())
                    template = source.operation_template()
                except (ga.AuthorityFault, EnhanceFault) as error:
                    # A missing/revoked source stays in the source revision;
                    # it cannot hide other permitted categories or old grants.
                    rights.append({'connection': source.connection_ref, 'unavailable': error.code})
                    continue
                source_views.append(current); ends.extend((current.expires_at, evidence.policy.expires_at))
                rights.append({'connection': source.connection_ref, 'policy': asdict(evidence.policy),
                    'revision': evidence.revision, 'terms': evidence.terms_ref, 'privacy': evidence.privacy_ref,
                    'locale': evidence.locale_ref, 'processor': evidence.processor_ref,
                    'operation_template_digest': digest(canonical_bytes(template))})
            # Include removed/disabled configurations and revoked grants as well.
            # They cannot disappear from freshness merely by being unavailable.
            configurations = list(PortableConnection.objects.filter(installation_id=state.installation_id)
                .exclude(adapter='openai_responses').order_by('pk').values())
            grants = list(PortableDisclosureGrant.objects.filter(cabinet=state,
                purpose='catalogue_lookup').order_by('pk').values())
            material = {'sources': configurations, 'grants': grants,
                        'rights': sorted(rights, key=lambda value: value['connection'])}
            import json
            source_revision = digest(json.dumps(material, sort_keys=True, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False, default=ga._local_default).encode())
            return CurrentState(view, bindings, self.registry, source_revision, min(ends), tuple(source_views))

    def _operation_digest(self, identity):
        from .discovery_transport import MOSHTIX_QUERY
        # Stable configured-operation identity; the dated operation's actual bytes
        # are independently bound to its plan approval and rechecked by the issuer.
        return digest(canonical_bytes({'method': 'POST', 'origin': 'https://api.moshtix.com',
            'path': '/v1/graphql', 'query_template': MOSHTIX_QUERY, 'page_size': 9,
            'window_weeks': 8, 'locale': 'VIC', 'application_identity': identity}))

    def filter_manifest(self, current, manifest):
        ga.assert_current(current.view)
        fresh = self.current(self.authority, current.view._session,
            profile_ref=current.view.profile_ref, connection_ref=current.view.connection_ref)
        ga._require(fresh.source_revision == current.source_revision, 'PREVIEW_STALE', 409)
        # Stored content may only be retained while its original complete binding
        # is current. Filtering never reconstitutes deleted or newly allowed facts.
        return manifest

    def invalidate_content(self):
        for source in self.sources:
            source.close()
