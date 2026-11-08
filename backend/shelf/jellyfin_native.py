"""Selected TV-item readback for the existing acquisition Library port.

This component performs no HTTP requests, registration or status writes. Its
caller must obtain both responses through the currently authorized server/user
binding; browser-supplied JSON is not a library observation. A parsed LibraryItem
does not itself authorize a green button or replace current native authority.
"""
from dataclasses import replace
from pathlib import PurePosixPath
import re

from shelf.acquisition_contract import ContractError
from shelf.acquisition_pipeline import (
    ImportReceipt, LibraryItem, MediaSource, ProviderJob, Snapshot, StagedFile,
    scope_family, verify_staging,
)
from shelf.episode_scope import parse_scope


def _require(condition):
    if not condition:
        raise ContractError('Exact Jellyfin episode readback required')


def _guid(value):
    _require(type(value) is str and re.fullmatch(r'[0-9a-f]{32}', value) is not None)
    return value


def _path(value):
    _require(type(value) is str and 0 < len(value) <= 4096
             and not any(ord(char) < 32 for char in value))
    path = PurePosixPath(value)
    _require(path.is_absolute() and str(path) == value and '..' not in path.parts)
    return path


def episode_readback(imported, episode, series, *, server_id, item_id,
                     series_id, library_root):
    """Validate one exact episode and its series, then recheck imported bytes.

    Episode TMDB IDs identify episodes, whereas the saved source ID identifies
    the show. Checking the separately selected series prevents accidentally
    treating a similarly numbered episode from another show as completion.
    """
    _require(type(imported) is ImportReceipt and type(imported.staged) is StagedFile
             and type(imported.staged.job) is ProviderJob
             and type(imported.staged.job.snapshot) is Snapshot)
    snapshot = imported.staged.job.snapshot
    _require((snapshot.provider, snapshot.medium) == ('tmdb', 'tv')
             and scope_family(snapshot.provider, snapshot.medium, snapshot.scope)[0] == 'episode')
    season_number, episode_number = parse_scope(snapshot.scope)
    root, target = _path(library_root), _path(imported.path)
    _require(str(root) != '/' and root != target and root in target.parents)
    for value in (server_id, item_id, series_id):
        _guid(value)
    _require(item_id != series_id and type(episode) is dict and type(series) is dict)
    try:
        _require(episode['Id'] == item_id and episode['ServerId'] == server_id
                 and episode['Type'] == 'Episode' and episode['SeriesId'] == series_id
                 and episode['IsFolder'] is False and episode['IsVirtualItem'] is False
                 and episode['LocationType'] == 'FileSystem'
                 and episode['MediaType'] == 'Video' and episode['Path'] == imported.path)
        # Multi-episode files cannot prove the single selected episode merely
        # by containing its first episode number.
        _require(type(episode['ParentIndexNumber']) is int
                 and episode['ParentIndexNumber'] == season_number
                 and type(episode['IndexNumber']) is int
                 and episode['IndexNumber'] == episode_number)
        end = episode.get('IndexNumberEnd')
        _require(end is None or (type(end) is int and end == episode_number))
        _require(series['Id'] == series_id and series['ServerId'] == server_id
                 and series['Type'] == 'Series'
                 and type(series['ProviderIds']) is dict
                 and series['ProviderIds'].get('Tmdb') == str(snapshot.source_id))
        sources = episode['MediaSources']
        _require(type(sources) is list and 0 < len(sources) <= 32
                 and all(type(source) is dict for source in sources))
        matches = [source for source in sources if source.get('Path') == imported.path]
        _require(len(matches) == 1)
        source = matches[0]
        source_id = _guid(source['Id'])
        _require(source['IsRemote'] is False and source['Protocol'] == 'File'
                 and type(source['Size']) is int and source['Size'] == imported.staged.size
                 and source.get('Container') in ('mkv', 'mp4', 'webm'))
        _require(sum(candidate.get('Id') == source_id for candidate in sources) == 1)
    except (KeyError, TypeError, AttributeError):
        raise ContractError('Incomplete Jellyfin episode readback') from None
    # Server path/size metadata alone cannot attest the downloaded bytes. Reuse
    # the no-symlink/current-inode verifier at the imported destination too.
    verify_staging(replace(imported.staged, path=imported.path))
    return LibraryItem(snapshot, item_id, (MediaSource(
        imported.staged.job, source_id, imported.path,
        imported.staged.sha256, imported.staged.size),))
