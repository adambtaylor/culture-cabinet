"""Exact media identities shared by completion listing and receipt validation."""
from copy import deepcopy
from shelf.episode_scope import is_episode_scope

from shelf import completion_registry as registry
from shelf.acquisition_contract import _text

SUPPORTED = {
    ('tmdb', 'movie', 'film'): 'movie',
    ('tmdb', 'tv', 'series_s01e01'): 'episode',
    ('album_request', 'music', 'album_flac'): 'album',
}


def kind(identity):
    registry.require(type(identity) is dict)
    if (identity.get('provider'), identity.get('medium')) == ('tmdb', 'tv'):
        registry.require(is_episode_scope(identity.get('scope')))
        return 'episode'
    value = SUPPORTED.get(tuple(identity.get(key) for key in ('provider', 'medium', 'scope')))
    registry.require(value is not None)
    return value


def source_identity(job):
    result = {key: getattr(job, key) for key in
              ('provider', 'medium', 'source_id', 'scope', 'title', 'edition')}
    kind(result)
    registry.require(type(job.source_id) is int and 0 < job.source_id <= 2147483647)
    _text(job.title, 300)
    _text(job.edition, 500)
    return {**result, 'basis': 'selected_catalogue_identity_not_content_verification'}


def album_identity(job):
    """Read the exact selected request, including its current authorization."""
    from shelf import album_acquisition, obtain_acquisition
    _, preview = obtain_acquisition.exact_result(job, fresh=False)
    request = album_acquisition.current_request(job.actor, job.profile_id, preview)
    registry.require(request.request_id == job.request_id)
    return deepcopy(request.identity)


def selection(complete, entry):
    result = {
        'source_identity': complete['source_identity'],
        'provenance': {key: entry[key] for key in
                       ('request_id', 'result_id', 'result_digest', 'nzb_sha256')},
        'nzo_id': entry['nzo_id'],
        'completion_evidence_sha256': complete['completion_evidence_sha256'],
    }
    if kind(complete['source_identity']) == 'album':
        result['album_identity'] = complete['album_identity']
    return result
