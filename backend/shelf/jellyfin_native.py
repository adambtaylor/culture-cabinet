"""Closed selected-TV read: metadata observation, never acquisition completion.

The credential/currentness ports belong to the legitimate backend custodian.
Constructing these DTOs grants no access. No ORM, file reads or status writes.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
from pathlib import PurePosixPath
import re
import socket
import time
from urllib.parse import urlencode

from shelf.episode_scope import parse_scope, ScopeError

ORIGIN = 'http://192.168.0.36:8096'
TV_ROOTS = ('/Users/adamtaylor/Movies/TV/Media.localized/TV Shows',
            '/Users/adamtaylor/JellyfinMedia/TV Shows')
MAX_BYTES = 128 * 1024
MAX_SOURCES = 8
MISSING_EVIDENCE = ('library_membership', 'catalogue_show_identity',
                    'repair_unpack', 'verified_destination_digest')


class Unavailable(ValueError):
    """Sanitized component refusal, containing no upstream/credential text."""


def _require(value):
    if not value:
        raise Unavailable('invalid_binding_or_response')


def _guid(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None
             and value != '0' * 32)
    return value


def _text(value, limit=200):
    _require(type(value) is str and 0 < len(value) <= limit
             and all(ord(char) >= 32 and not 127 <= ord(char) <= 159 for char in value))
    return value


def _path(value, root):
    _text(value, 4096)
    path, base = PurePosixPath(value), PurePosixPath(root)
    _require(path.is_absolute() and str(path) == value and '..' not in path.parts
             and base in path.parents)
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _stamp(value):
    _require(type(value) is datetime and value.utcoffset() is not None)
    return value


@dataclass(frozen=True)
class ServiceBinding:
    origin: str
    server_id: str
    server_version: str
    user_id: str
    library_id: str
    installation_id: str
    actor_id: int
    profile_id: int
    configuration_generation: str
    credential_generation: str
    tv_root: str
    purpose: str
    expires_at: datetime
    connect_seconds: float = 2.0
    request_seconds: float = 10.0
    response_bytes: int = MAX_BYTES

    def validate(self):
        _require(self.origin == ORIGIN and self.tv_root in TV_ROOTS
                 and self.purpose == 'selected_tv_read')
        for value in (self.server_id, self.user_id, self.library_id, self.installation_id,
                      self.configuration_generation, self.credential_generation):
            _guid(value)
        _text(self.server_version, 40)
        _require(type(self.actor_id) is int and self.actor_id > 0
                 and type(self.profile_id) is int and self.profile_id > 0)
        _stamp(self.expires_at)
        for value, maximum in ((self.connect_seconds, 2), (self.request_seconds, 10)):
            _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum)
        _require(type(self.response_bytes) is int and 0 < self.response_bytes <= MAX_BYTES)

    def material(self):
        self.validate()
        return {**self.__dict__, 'expires_at': self.expires_at.isoformat()}


@dataclass(frozen=True)
class SelectedEpisode:
    item_id: str
    scope: str

    def validate(self):
        _guid(self.item_id)
        try:
            _require(parse_scope(self.scope) is not None)
        except ScopeError:
            raise Unavailable('invalid_binding_or_response') from None

    def material(self):
        self.validate()
        return dict(self.__dict__)


@dataclass(frozen=True)
class SelectedRead:
    binding_digest: str
    selection_digest: str
    path: str

    @property
    def material(self):
        return {'origin': ORIGIN, 'method': 'GET', 'path': self.path,
                'binding_digest': self.binding_digest, 'selection_digest': self.selection_digest}


def selected_operation(binding, selection):
    _require(type(binding) is ServiceBinding and type(selection) is SelectedEpisode)
    bound, selected = binding.material(), selection.material()
    # One builder supplies both reviewed material and sent URL. No search,
    # second ID, images/user history, pagination or arbitrary caller parameters.
    query = (('ids', selection.item_id), ('userId', binding.user_id),
             ('includeItemTypes', 'Episode'), ('recursive', 'false'),
             ('startIndex', '0'), ('limit', '1'),
             ('fields', 'Path,MediaSources,ProviderIds,ParentId'),
             ('enableUserData', 'false'), ('enableImages', 'false'),
             ('enableTotalRecordCount', 'false'))
    return SelectedRead(_digest(bound), _digest(selected), '/Items?' + urlencode(query))


@dataclass(frozen=True)
class ScopedCredential:
    """Scoped custodian output; never retained in an observation or repr."""
    binding_digest: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ObservedMediaSource:
    source_id: str
    path: str
    size: int


@dataclass(frozen=True)
class SelectedLibraryObservation:
    status: str
    reason: str
    binding_digest: str
    selection_digest: str
    operation_digest: str
    response_digest: str | None
    observed_at: datetime | None
    server_id: str | None = None
    user_id: str | None = None
    library_id: str | None = None
    item_id: str | None = None
    parent_id: str | None = None
    series_id: str | None = None
    scope: str | None = None
    server_version: str | None = None
    configuration_generation: str | None = None
    credential_generation: str | None = None
    media_sources: tuple[ObservedMediaSource, ...] = ()
    missing_evidence: tuple[str, ...] = MISSING_EVIDENCE


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class _DeadlineSocket(socket.socket):
    """Absolute deadline also covers drip-fed headers/chunked bodies."""
    def __init__(self, connected, deadline, clock):
        super().__init__(connected.family, connected.type, connected.proto,
                         fileno=connected.detach())
        self.deadline, self.clock = deadline, clock

    def _remaining(self):
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.settimeout(remaining)

    def recv_into(self, *args, **kwargs):
        self._remaining()
        return super().recv_into(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._remaining()
        return super().sendall(*args, **kwargs)


class _Connection(http.client.HTTPConnection):
    def __init__(self, *, deadline, connect_timeout, clock):
        super().__init__('192.168.0.36', 8096, timeout=connect_timeout)
        self.deadline, self.clock = deadline, clock

    def connect(self):
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self.deadline, self.clock)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Unavailable('invalid_response')
        result[key] = value
    return result


def _decode(payload, binding, selection):
    value = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(Unavailable('invalid_response')))
    _require(type(value) is dict and type(value.get('Items')) is list)
    items = value['Items']
    if not items:
        return None
    _require(len(items) == 1 and type(items[0]) is dict)
    item = items[0]
    _require(item['Id'] == selection.item_id and item['ServerId'] == binding.server_id
             and item['Type'] == 'Episode' and item.get('IsVirtualItem', False) is False
             and item.get('IsFolder', False) is False
             and item.get('LocationType', 'FileSystem') == 'FileSystem'
             and item.get('MediaType', 'Video') == 'Video')
    season, episode = parse_scope(selection.scope)
    _require(type(item['ParentIndexNumber']) is int and item['ParentIndexNumber'] == season
             and type(item['IndexNumber']) is int and item['IndexNumber'] == episode)
    end = item.get('IndexNumberEnd')
    _require(end is None or (type(end) is int and end == episode))
    parent, series = _guid(item['ParentId']), _guid(item['SeriesId'])
    _path(item['Path'], binding.tv_root)
    sources = item['MediaSources']
    _require(type(sources) is list and 0 < len(sources) <= MAX_SOURCES)
    observed = []
    for source in sources:
        _require(type(source) is dict and source['Protocol'] == 'File'
                 and source.get('IsRemote', False) is False)
        identity = _text(source['Id'])
        path = _path(source['Path'], binding.tv_root)
        size = source['Size']
        _require(type(size) is int and 0 < size <= 128 * 1024**3)
        observed.append(ObservedMediaSource(identity, path, size))
    _require(len({s.source_id for s in observed}) == len(observed)
             and len({s.path for s in observed}) == len(observed)
             and any(s.path == item['Path'] for s in observed))
    # ParentId usually denotes a season: it is NOT proof of selected-library
    # membership. No source hash, import receipt or completion is manufactured.
    return parent, series, tuple(observed)


def _read_payload(binding, operation, token, *, current, clock, deadline):
    """Shared bounded transport for the two closed selected-TV builders."""
    _require(type(operation) is SelectedRead
             and operation.binding_digest == _digest(binding.material()))
    connection = None
    try:
        current()
        connection = _Connection(deadline=deadline,
            connect_timeout=min(binding.connect_seconds, deadline - clock.monotonic()), clock=clock)
        connection.connect()
        current()
        connection.request('GET', operation.path, headers={
            'Accept': 'application/json', 'X-Emby-Token': token})
        response = connection.getresponse()
        if response.status != 200:
            raise Unavailable('upstream_unavailable')
        headers = response.getheaders()
        for name in ('content-length', 'content-encoding', 'content-type', 'transfer-encoding'):
            _require(sum(key.lower() == name for key, _ in headers) <= 1)
        encoding = response.getheader('Content-Encoding', 'identity')
        length = response.getheader('Content-Length')
        transfer = response.getheader('Transfer-Encoding')
        _require(encoding == 'identity' and transfer in (None, 'chunked')
                 and not (length is not None and transfer is not None)
                 and response.getheader('Content-Type', '').split(';')[0].strip().lower() == 'application/json')
        if length is not None:
            _require(re.fullmatch('[0-9]{1,10}', length) is not None and int(length) <= binding.response_bytes)
        payload = response.read(binding.response_bytes + 1)
        _require(type(payload) is bytes and len(payload) <= binding.response_bytes
                 and (length is None or len(payload) == int(length)))
        current()
        return payload
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass  # Cleanup must not replace a sanitized refusal.


def read_selected(binding, selection, *, credential_scope=None, revalidate=None, clock=None,
                  _deadline=None):
    """One bounded read, current before secret release/send and after response.

    The backend revalidator returns the actual current binding/selection pair;
    a bare caller Boolean cannot stand in for that port. No configured ports
    means no connection, including for an otherwise valid DTO.
    """
    clock = clock or SystemClock()
    operation = None
    response_digest = None
    try:
        operation = selected_operation(binding, selection)
        op_digest = _digest(operation.material)
        deadline = clock.monotonic() + min(binding.request_seconds,
            (_stamp(binding.expires_at) - _stamp(clock.now())).total_seconds())
        if _deadline is not None:
            deadline = min(deadline, _deadline)  # Resolver may only shorten this request.

        def current():
            _require(clock.monotonic() < deadline and _stamp(clock.now()) < binding.expires_at
                     and selected_operation(binding, selection) == operation)
            _require(revalidate is not None and revalidate(binding, selection) == (binding, selection))
            _require(selected_operation(binding, selection) == operation
                     and clock.monotonic() < deadline and clock.now() < binding.expires_at)

        if credential_scope is None or revalidate is None:
            raise Unavailable('read_binding_unavailable')
        current()
        with credential_scope(binding) as credential:
            current()
            _require(type(credential) is ScopedCredential
                     and credential.binding_digest == operation.binding_digest)
            token = credential.token
            _require(type(token) is str and 0 < len(token) <= 4096
                     and all(33 <= ord(c) <= 126 for c in token))
            payload = _read_payload(binding, operation, token, current=current,
                                    clock=clock, deadline=deadline)
            response_digest = hashlib.sha256(payload).hexdigest()
            decoded = _decode(payload, binding, selection)
            current()
            observed_at = _stamp(clock.now())
        current()
        values = dict(status='matched' if decoded else 'not_observed', reason='',
            binding_digest=operation.binding_digest, selection_digest=operation.selection_digest,
            operation_digest=op_digest, response_digest=response_digest, observed_at=observed_at,
            server_id=binding.server_id, user_id=binding.user_id, library_id=binding.library_id,
            item_id=selection.item_id, scope=selection.scope, server_version=binding.server_version,
            configuration_generation=binding.configuration_generation,
            credential_generation=binding.credential_generation)
        if decoded:
            values.update(parent_id=decoded[0], series_id=decoded[1], media_sources=decoded[2])
        return SelectedLibraryObservation(**values)
    except Exception as error:
        # Fixed reasons, never repr(error), raw body, URLs, headers or tokens.
        reason = 'read_binding_unavailable' if type(error) is Unavailable and str(error) == 'read_binding_unavailable' else 'selected_read_unavailable'
        return SelectedLibraryObservation('unavailable', reason,
            operation.binding_digest if operation else '', operation.selection_digest if operation else '',
            _digest(operation.material) if operation else '', response_digest, None)


@dataclass(frozen=True)
class EpisodeResolutionTarget:
    request_id: str
    binding_digest: str
    content_identity_digest: str
    destination_binding_digest: str
    show_id: int
    scope: str
    show_name: str | None
    series_id: str | None
    facts_valid_until: datetime

    def material(self):
        from shelf.obtain_tv_flow import tv_content_identity_digest
        selected = parse_scope(self.scope)
        _require(selected is not None)
        season, episode = selected
        _require(tv_content_identity_digest(tmdb_show_id=self.show_id, season=season,
                 episode=episode) == self.content_identity_digest)
        for value in (self.binding_digest, self.content_identity_digest, self.destination_binding_digest):
            _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)
        _text(self.request_id, 64)
        if self.series_id is not None:
            _guid(self.series_id)
        else:
            from shelf.obtain_parser import plain
            _require(plain(self.show_name) == self.show_name)
        _stamp(self.facts_valid_until)
        return {**self.__dict__, 'facts_valid_until': self.facts_valid_until.isoformat()}


@dataclass(frozen=True)
class ResolvedEpisodeBinding:
    status: str
    reason: str
    request_id: str
    job_binding_digest: str
    content_identity_digest: str
    destination_binding_digest: str
    target_digest: str
    service_binding_digest: str
    server_id: str
    user_id: str
    library_id: str
    root: str
    configuration_generation: str
    credential_generation: str
    series_id: str | None
    selection: SelectedEpisode | None
    observation: SelectedLibraryObservation | None
    operation_digests: tuple[str, ...]
    response_digests: tuple[str, ...]
    observed_at: datetime | None
    valid_until: datetime | None
    binding: ServiceBinding | None = None


class _ResolutionStop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _resolution_operation(binding, target, *, series_id=None):
    _require(type(binding) is ServiceBinding and type(target) is EpisodeResolutionTarget)
    bound, selected = binding.material(), target.material()
    common = [('userId', binding.user_id), ('recursive', 'true'), ('startIndex', '0'),
              ('limit', '2'), ('fields', 'Path,ProviderIds,ParentId'),
              ('enableUserData', 'false'), ('enableImages', 'false'),
              ('enableTotalRecordCount', 'false')]
    if series_id is None:
        query = [('parentId', binding.library_id), ('includeItemTypes', 'Series')]
        if target.series_id is not None:
            query.append(('ids', target.series_id))
        else:
            query.append(('nameStartsWith', target.show_name))
    else:
        _guid(series_id)
        season, episode = parse_scope(target.scope)
        query = [('parentId', series_id), ('includeItemTypes', 'Episode'),
                 ('indexNumber', str(episode)), ('parentIndexNumber', str(season))]
    return SelectedRead(_digest(bound), _digest(selected), '/Items?' + urlencode(query + common))


def resolve_episode(binding, target, *, credential_scope=None, revalidate=None, clock=None):
    """Resolve only one current target under its configured library/series.

    A full two-item response is ambiguous: no uniqueness claim is made from a
    truncated prefix result. Original one-ID read supplies the final metadata;
    this companion supplies the separately scoped series/library/show proof.
    """
    clock = clock or SystemClock()
    operations, responses = [], []
    bound_digest = target_digest = ''
    selected = observation = None
    series_id = None
    identity_digest = ''
    try:
        _require(type(binding) is ServiceBinding and type(target) is EpisodeResolutionTarget)
        bound_digest, target_digest = _digest(binding.material()), _digest(target.material())
        deadline = clock.monotonic() + min(binding.request_seconds,
            (binding.expires_at - _stamp(clock.now())).total_seconds(),
            (target.facts_valid_until - clock.now()).total_seconds())
        _require(credential_scope is not None and revalidate is not None)

        def current():
            _require(clock.monotonic() < deadline and clock.now() < binding.expires_at
                     and clock.now() < target.facts_valid_until
                     and _digest(binding.material()) == bound_digest
                     and _digest(target.material()) == target_digest
                     and revalidate(binding, target) == (binding, target))
            _require(clock.monotonic() < deadline and clock.now() < binding.expires_at
                     and clock.now() < target.facts_valid_until
                     and _digest(binding.material()) == bound_digest
                     and _digest(target.material()) == target_digest)

        def query(parent_series=None):
            operation = _resolution_operation(binding, target, series_id=parent_series)
            current()
            with credential_scope(binding) as credential:
                current()
                _require(type(credential) is ScopedCredential and credential.binding_digest == bound_digest
                         and type(credential.token) is str and 0 < len(credential.token) <= 4096
                         and all(33 <= ord(c) <= 126 for c in credential.token))
                payload = _read_payload(binding, operation, credential.token, current=current,
                                        clock=clock, deadline=deadline)
                current()
            current()
            operations.append(_digest(operation.material))
            responses.append(hashlib.sha256(payload).hexdigest())
            result = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(Unavailable('invalid_response')))
            _require(type(result) is dict and type(result.get('Items')) is list
                     and len(result['Items']) <= 2)
            items = result['Items']
            if not items:
                raise _ResolutionStop('not_indexed', 'awaiting_library_index')
            if len(items) != 1:
                raise _ResolutionStop('ambiguous', 'ambiguous_library_item')
            _require(type(items[0]) is dict)
            return items[0]

        series = query()
        series_id = _guid(series['Id'])
        _require(series['ServerId'] == binding.server_id and series['Type'] == 'Series')
        if target.series_id is not None:
            _require(series_id == target.series_id)
        else:
            _require(series['Name'] == target.show_name)
        _path(series['Path'], binding.tv_root)
        provider_id = series['ProviderIds']['Tmdb']
        _require(type(provider_id) is str and str(int(provider_id)) == provider_id
                 and int(provider_id) == target.show_id)
        from shelf.obtain_tv_flow import tv_content_identity_digest
        season, episode_number = parse_scope(target.scope)
        identity_digest = tv_content_identity_digest(tmdb_show_id=int(provider_id),
            season=season, episode=episode_number)
        _require(identity_digest == target.content_identity_digest)
        episode = query(series_id)
        item_id = _guid(episode['Id'])
        _require(episode['ServerId'] == binding.server_id and episode['Type'] == 'Episode'
                 and episode['SeriesId'] == series_id
                 and type(episode['IndexNumber']) is int and episode['IndexNumber'] == episode_number
                 and type(episode['ParentIndexNumber']) is int and episode['ParentIndexNumber'] == season)
        end = episode.get('IndexNumberEnd')
        _require(end is None or (type(end) is int and end == episode_number))
        selected = SelectedEpisode(item_id, target.scope)
        def current_selected(current_binding, current_selection):
            current()
            _require(current_binding == binding and current_selection == selected)
            return current_binding, current_selection
        observation = read_selected(binding, selected, credential_scope=credential_scope,
            revalidate=current_selected, clock=clock, _deadline=deadline)
        current()
        if observation.status == 'not_observed':
            raise _ResolutionStop('not_indexed', 'awaiting_library_index')
        _require(observation.status == 'matched' and observation.series_id == series_id)
        operations.append(observation.operation_digest)
        responses.append(observation.response_digest)
        current()
        return ResolvedEpisodeBinding('resolved', '', target.request_id, target.binding_digest,
            identity_digest, target.destination_binding_digest, target_digest, bound_digest,
            binding.server_id, binding.user_id, binding.library_id, binding.tv_root,
            binding.configuration_generation, binding.credential_generation, series_id,
            selected, observation, tuple(operations), tuple(responses), clock.now(),
            min(binding.expires_at, target.facts_valid_until), binding)
    except Exception as error:
        status = error.status if type(error) is _ResolutionStop else 'unavailable'
        reason = error.reason if type(error) is _ResolutionStop else 'episode_resolution_unavailable'
        # No names, provider IDs, paths from unaccepted results or raw errors.
        return ResolvedEpisodeBinding(status, reason,
            target.request_id if type(target) is EpisodeResolutionTarget else '',
            target.binding_digest if type(target) is EpisodeResolutionTarget else '',
            '', '', target_digest, bound_digest,
            binding.server_id if type(binding) is ServiceBinding else '',
            binding.user_id if type(binding) is ServiceBinding else '',
            binding.library_id if type(binding) is ServiceBinding else '',
            '', '', '', None, None, None, tuple(operations), tuple(responses), None, None)
