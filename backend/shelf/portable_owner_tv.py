"""Issuer-held owner TV queries and finite factual selections, never preferences."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from django import forms

from . import grant_authority as ga
from .disclosure import canonical_bytes
from .enhance_contract import EnhanceBindings, digest, require, utc
from .episode_scope import make_scope

ENTRY_URL = '/cabinet/obtain/tv/'
PREVIEW_URL = ENTRY_URL + 'preview/'
LOOKUP_URL = ENTRY_URL + 'lookup/'
SELECT_URL = ENTRY_URL + 'select/'
OWNER_QUERY_SCHEMA = 'catalogue-owner-query/v1'
OWNER_ORIGIN_SCHEMA = 'obtain-owner-selection-origin/v1'
OWNER_SUBMISSION_SCHEMA = 'obtain-owner-selection-submission/v1'
_SEAL = object()


class OwnerTVForm(forms.Form):
    title = forms.CharField(max_length=300, strip=True, label='TV series title')
    series_year = forms.RegexField(r'^[1-9][0-9]{3}$', label='Series first-air year')
    season = forms.IntegerField(min_value=0, max_value=999)
    episode = forms.IntegerField(min_value=1, max_value=999)

    def clean_title(self):
        from .provider_catalogue import validate_query, CatalogueError
        title = self.cleaned_data['title']
        if any(ord(char) < 32 or ord(char) == 127 for char in title):
            raise forms.ValidationError('Use one plain series title.')
        try:
            if validate_query(title) != title:
                raise CatalogueError('invalid')
        except CatalogueError:
            raise forms.ValidationError('Use one exact TV title within the source query limit.') from None
        return title

    def clean_series_year(self):
        return int(self.cleaned_data['series_year'])


class _ReferenceForm(forms.Form):
    def _canonical(self, key):
        raw = self.cleaned_data[key]
        try:
            if str(UUID(raw)) != raw or UUID(raw).int == 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise forms.ValidationError('Use the current native form.') from None
        return raw


class OwnerLookupForm(_ReferenceForm):
    intent_ref = forms.CharField(widget=forms.HiddenInput)
    source_preview_ref = forms.CharField(widget=forms.HiddenInput)
    plan_digest = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput)
    operation_digest = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label='I approve this exact title lookup with TMDB.')

    def clean_intent_ref(self):
        return self._canonical('intent_ref')

    def clean_source_preview_ref(self):
        return self._canonical('source_preview_ref')


class OwnerMatchForm(_ReferenceForm):
    intent_ref = forms.CharField(widget=forms.HiddenInput)
    match_ref = forms.CharField(widget=forms.HiddenInput)
    match_digest = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label='This is the intended series and episode.')

    def clean_intent_ref(self):
        return self._canonical('intent_ref')

    def clean_match_ref(self):
        return self._canonical('match_ref')


class ObtainOriginSelectorForm(forms.Form):
    publication_revision = forms.IntegerField(required=False, min_value=1, max_value=2**53-1,
                                             widget=forms.HiddenInput)
    candidate_key = forms.CharField(required=False, max_length=300, widget=forms.HiddenInput)
    owner_selection_ref = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            if self.initial.get('owner_selection_ref'):
                self.fields.pop('publication_revision'); self.fields.pop('candidate_key')
            else:
                self.fields.pop('owner_selection_ref')

    def clean(self):
        values = super().clean()
        owner_keys = 'owner_selection_ref' in self.data
        publication_keys = bool({'publication_revision', 'candidate_key'} & set(self.data))
        if owner_keys == publication_keys:
            raise forms.ValidationError('Do not mix selection fields.')
        owner = values.get('owner_selection_ref')
        publication = values.get('publication_revision') is not None or bool(values.get('candidate_key'))
        if bool(owner) == publication:
            raise forms.ValidationError('Use exactly one current selection.')
        if owner:
            try:
                if str(UUID(owner)) != owner or UUID(owner).int == 0:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise forms.ValidationError('Use the current source selection.') from None
        elif not (values.get('publication_revision') and values.get('candidate_key')):
            raise forms.ValidationError('Current publication selection required.')
        return values


class ObtainOptionsForm(ObtainOriginSelectorForm):
    search_review_ref = forms.UUIDField(widget=forms.HiddenInput)
    search_confirmed = forms.BooleanField(label='I approve this exact query and Hydra destination.')
    season = forms.IntegerField(required=False, min_value=0, max_value=999)
    episode = forms.IntegerField(required=False, min_value=1, max_value=999)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.initial.get('owner_selection_ref'):
            self.fields.pop('season'); self.fields.pop('episode')

    def clean(self):
        values = super().clean()
        if values.get('owner_selection_ref'):
            if {'season', 'episode'} & set(self.data):
                raise forms.ValidationError('The confirmed owner selection already fixes the episode.')
        elif (values.get('season') is None) != (values.get('episode') is None):
            raise forms.ValidationError('Choose both season and episode.')
        return values


@dataclass(frozen=True, repr=False)
class OwnerTVIntent:
    intent_ref: str
    title: str
    series_year: int
    scope: str
    created_at: datetime
    expires_at: datetime
    bindings: EnhanceBindings
    selection_digest: str
    source_binding_digest: str
    _state: object = field(repr=False)
    _seal: object = field(repr=False)

    @property
    def intent_digest(self):
        return digest(canonical_bytes({'schema': 'owner-tv-intent/v1', 'intent_ref': self.intent_ref,
            'title': self.title, 'series_year': self.series_year, 'scope': self.scope,
            'created_at': ga._stamp(self.created_at), 'expires_at': ga._stamp(self.expires_at),
            'bindings': asdict(self.bindings), 'selection_digest': self.selection_digest,
            'source_binding_digest': self.source_binding_digest}))

    @property
    def parent_ref(self):
        return 'owner-query:' + self.intent_ref + ':' + self.intent_digest

    def query_input(self, grants, context, categories, *, now):
        require(self._seal is _SEAL and categories == ('tv',), 'AUTHORITY_DENIED')
        view = self._state.validate(self, now=now)
        require(grants == view.grants and context == view.context, 'PREVIEW_STALE')
        return {'schema': OWNER_QUERY_SCHEMA, 'intent_ref': self.intent_ref,
            'intent_digest': self.intent_digest, 'categories': ['tv'],
            'queries': [{'category': 'tv', 'terms': [self.title]}]}


def _view_bindings(view, registry):
    return EnhanceBindings(view.installation_id, view.actor_ref, view.profile_ref,
        view.profile_revision_digest, view.policy_epoch, view.restriction_watermark,
        registry.revision, view.context['config_revision'], digest(canonical_bytes(view.grants)),
        digest(canonical_bytes(view.connection_binding)))


def _source_digest(view, rights):
    return digest(canonical_bytes({'connection': view.connection_binding, 'grants': view.grants,
        'rights': {'source': rights.source, 'revision': rights.revision, 'terms': rights.terms_ref,
            'privacy': rights.privacy_ref, 'locale': rights.locale_ref, 'processor': rights.processor_ref,
            'policy': {'ref': rights.policy.ref, 'revision': rights.policy.revision,
                'checked_at': ga._stamp(rights.policy.checked_at),
                'expires_at': ga._stamp(rights.policy.expires_at),
                'permissions': [[name, list(uses)] for name, uses in rights.policy.permissions]}}}))


class OwnerTVState:
    """One existing source issuer owns these opaque handles and their lineage.

    An intent is current human input, not a profile signal or serialized grant.
    Raw source replies never enter this store or a completed-result cache.
    """
    def __init__(self, issuer):
        self.issuer = issuer
        self.intents, self.attempted, self.matches, self.selections = {}, set(), {}, {}

    def _access(self):
        from .portable_saved_authority import active_portable_access, PortableSavedAccess
        access = active_portable_access()
        require(type(access) is PortableSavedAccess and access.authority is self.issuer.authority
            and access.session_ref == self.issuer.session_ref
            and access.profile_ref == self.issuer.profile_ref, 'AUTHORITY_DENIED')
        access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='mutate')
        return access

    def create(self, *, title, series_year, season, episode):
        from .category_registry import registry_view
        form = OwnerTVForm({'title': title, 'series_year': str(series_year),
                            'season': season, 'episode': episode})
        require(form.is_valid(), 'CONTRACT_INVALID')
        self.purge()
        require(len(self.intents) < 32, 'LIMIT_EXCEEDED')
        access = self._access()
        view = self.issuer._view()
        require(self.issuer.source_key() == 'tmdb_tv', 'AUTHORITY_DENIED')
        now = utc(self.issuer.authority.now()); rights = self.issuer._rights(now)
        end = min(now + timedelta(minutes=5), view.expires_at, rights.policy.expires_at)
        intent = OwnerTVIntent(str(uuid4()), form.cleaned_data['title'],
            form.cleaned_data['series_year'], make_scope(form.cleaned_data['season'], form.cleaned_data['episode']),
            now, end, _view_bindings(view, registry_view(('tv',))),
            digest(canonical_bytes(access._signed)), _source_digest(view, rights), self, _SEAL)
        self.intents[intent.intent_ref] = intent
        return intent

    def validate(self, intent, *, now):
        from .category_registry import registry_view
        require(type(intent) is OwnerTVIntent and intent._seal is _SEAL
            and intent._state is self and self.intents.get(intent.intent_ref) is intent,
            'AUTHORITY_DENIED')
        try:
            require(utc(now) < intent.expires_at, 'PREVIEW_STALE')
            access = self._access(); view = self.issuer._view()
            rights = self.issuer._rights(utc(self.issuer.authority.now()))
            require(digest(canonical_bytes(access._signed)) == intent.selection_digest
                and _view_bindings(view, registry_view(('tv',))) == intent.bindings
                and _source_digest(view, rights) == intent.source_binding_digest, 'PREVIEW_STALE')
            return view
        except Exception:
            self._discard(intent.intent_ref)
            raise

    def _discard(self, ref):
        self.intents.pop(ref, None)
        self.attempted.discard(ref)
        self.matches.pop(ref, None)
        self.selections = {key: value for key, value in self.selections.items() if value['intent_ref'] != ref}

    def purge(self):
        for ref, intent in list(self.intents.items()):
            try:
                self.validate(intent, now=utc(self.issuer.authority.now()))
            except Exception:
                self._discard(ref)
        now = utc(self.issuer.authority.now())
        self.matches = {key: value for key, value in self.matches.items() if now < value['end']}
        self.selections = {key: value for key, value in self.selections.items() if now < value['end']}

    def close(self):
        self.intents.clear(); self.attempted.clear(); self.matches.clear(); self.selections.clear()

    def plan(self, intent):
        from .category_registry import registry_view
        from .inference_body import serialize_catalogue
        view = self.validate(intent, now=utc(self.issuer.authority.now()))
        return serialize_catalogue(None, view.grants, view.context, registry_view(('tv',)),
            now=utc(self.issuer.authority.now()), plan_ref=intent.intent_ref,
            bindings=intent.bindings, owner_intent=intent)

    def preview(self, **values):
        intent = self.create(**values)
        try:
            result = self.issuer.preview(self.plan(intent))
            return {'intent_ref': intent.intent_ref, 'operation': self.issuer._previews[result['preview_ref']].operation.material,
                    'source_preview_ref': result['preview_ref'], **result}
        except Exception:
            self.intents.pop(intent.intent_ref, None)
            raise

    def lookup(self, values, backend):
        from .source_adapters import MediaBackend, SourceRequest, normalize, registered_source
        from .source_collection import checkpoint_end
        from .disclosure import SCALARS
        from .obtain_parser import words
        form = OwnerLookupForm(values)
        require(form.is_valid() and type(backend) is MediaBackend and backend.issuer is self.issuer,
                'AUTHORITY_DENIED')
        data = form.cleaned_data; self.purge()
        intent = self.intents.get(data['intent_ref'])
        require(intent is not None, 'PREVIEW_STALE')
        plan = self.plan(intent)
        approval = self.issuer._previews.get(data['source_preview_ref'])
        require(approval is not None and approval.plan == plan, 'PREVIEW_STALE')
        self.issuer.confirm(data['source_preview_ref'], plan_digest=data['plan_digest'],
            operation_digest=data['operation_digest'], confirmed=data['confirmed'])
        request = SourceRequest('tmdb_tv', 'tv', 'search_tv', (intent.title,))
        require(intent.intent_ref not in self.attempted, 'AUTHORITY_DENIED')
        # Consume before authorizing transport. A refused/uncertain attempt is
        # never silently replayed by refreshing or posting the same form again.
        self.attempted.add(intent.intent_ref)
        permit = backend.authorize(request, plan, now=utc(self.issuer.authority.now()))
        policy = backend.policy(permit, now=utc(self.issuer.authority.now()))
        require(all(policy.allows(target, use) for target in SCALARS | {'tv.first_air_date'}
                    for use in ('fetch', 'display', 'retain')), 'AUTHORITY_DENIED')
        with self._access().effect_window(action='mutate'):
            self.validate(intent, now=utc(self.issuer.authority.now()))
            reply = backend.request(request, permit, response_bytes=registered_source('tmdb_tv').response_bytes,
                                    request_seconds=5, parent_seconds=6)
            now = utc(self.issuer.authority.now())
            require(backend.revalidate(permit, request, plan, now=now)
                    and backend.policy(permit, now=now) == policy, 'PREVIEW_STALE')
            self.validate(intent, now=now)
            require(reply.status == 200, 'SOURCE_UNAVAILABLE')
            directives = dict(reply.headers).get('cache-control', '').lower()
            require(not any(value in directives for value in ('no-store', 'no-cache')), 'AUTHORITY_DENIED')
            end = checkpoint_end(reply, expires_at=min(intent.expires_at, permit.expires_at,
                policy.expires_at, reply.observed_at + timedelta(minutes=5)))
            require(reply.observed_at <= now < end, 'PREVIEW_STALE')
            parsed = normalize(registered_source('tmdb_tv'), request, reply, now=now)
            # The parser alone validates dates and associates them by validated
            # IDs. Missing or ambiguous versions cannot become a title guess.
            cards = [card for card in parsed.cards if words(card.title) == words(intent.title)
                and dict(card.facts).get('tv.first_air_date', '')[:4] == str(intent.series_year)]
            if len(cards) != 1:
                from .portable_authority import AuthorityFault
                raise AuthorityFault('SOURCE_IDENTITY_UNRESOLVED', 409)
            card = cards[0]; ref = str(uuid4())
            material = {'card': {**asdict(card), 'facts': [[key, value] for key, value in card.facts]},
                        'intent_digest': intent.intent_digest,
                        'observed_at': ga._stamp(reply.observed_at), 'valid_until': ga._stamp(end)}
            self.matches[intent.intent_ref] = {'intent_ref': intent.intent_ref, 'match_ref': ref,
                'match_digest': digest(canonical_bytes(material)), 'card': card, 'end': end}
            return self.match_views(intent.intent_ref)

    def match_views(self, intent_ref):
        self.purge()
        value = self.matches.get(intent_ref)
        require(value is not None, 'PREVIEW_STALE')
        intent = self.intents[intent_ref]
        require(utc(self.issuer.authority.now()) < value['end'], 'PREVIEW_STALE')
        fields = {key: value[key] for key in ('intent_ref', 'match_ref', 'match_digest')}
        return ({**fields, 'title': value['card'].title, 'series_year': intent.series_year,
            'source_url': value['card'].source_url, 'scope': intent.scope,
            'expires_at': ga._stamp(value['end']), 'selectable': True,
            'select_form': OwnerMatchForm(initial=fields)},)

    def select(self, values):
        form = OwnerMatchForm(values)
        require(form.is_valid(), 'CONTRACT_INVALID')
        self.purge(); data = form.cleaned_data
        value = self.matches.get(data['intent_ref'])
        require(value is not None and all(data[key] == value[key]
                for key in ('intent_ref', 'match_ref', 'match_digest')), 'PREVIEW_STALE')
        require(utc(self.issuer.authority.now()) < value['end'], 'PREVIEW_STALE')
        ref = str(uuid4())
        # Only this chosen factual record survives. The raw response and all
        # other candidates were discarded before returning the match page.
        self.selections[ref] = value
        self.matches.pop(data['intent_ref'])
        return ref

    def resolve(self, selection_ref, *, now):
        from .portable_obtain_origin import CurrentObtainTarget, validate_obtain_origin
        from .obtain_tv_flow import tv_content_identity_digest
        from .episode_scope import parse_scope
        self.purge(); value = self.selections.get(selection_ref)
        require(value is not None, 'PREVIEW_STALE')
        intent = self.intents[value['intent_ref']]
        self.validate(intent, now=now)
        require(utc(now) < value['end'], 'PREVIEW_STALE')
        access = self._access(); card = value['card']
        policy = self.issuer._rights(utc(self.issuer.authority.now())).policy
        season, episode = parse_scope(intent.scope)
        end = value['end']
        origin = {'schema': OWNER_ORIGIN_SCHEMA,
            'installation_ref': access.identity.installation_ref, 'profile_ref': access.profile_ref,
            'selection_ref': selection_ref, 'source_connection_ref': self.issuer.connection_ref,
            'owner_intent_digest': intent.intent_digest, 'source_binding_digest': intent.source_binding_digest,
            'target_digest': tv_content_identity_digest(tmdb_show_id=int(card.source_id), season=season,
                                                       episode=episode), 'valid_until': ga._stamp(end)}
        validate_obtain_origin(origin)
        year = int(dict(card.facts)['tv.first_air_date'][:4])
        target = CurrentObtainTarget(card.provider, card.medium, int(card.source_id), card.title,
            intent.scope, end, policy.allows('title', 'export'), year,
            policy.allows('tv.first_air_year', 'export'))
        return origin, target


def get_owner_tv(access):
    from .portable_saved_authority import active_portable_access
    from .portable_enhance_service import get_service
    require(active_portable_access() is access, 'AUTHORITY_DENIED')
    issuer, _ = get_service(access.authority).owner_tv_source(access.session_ref, access.profile_ref)
    issuer.owner_tv._access()
    return issuer.owner_tv


def owner_backend(access):
    from .portable_enhance_service import get_service
    return get_service(access.authority).owner_tv_source(access.session_ref, access.profile_ref)[1]
