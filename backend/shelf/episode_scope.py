"""Canonical selected TV episodes, shared with offline consumers (Python 3.9+).

Scope is authoritative persisted identity. Release filenames never select episodes.
The legacy unqualified series request retains its original pilot identity.
"""
import re


class ScopeError(ValueError):
    """A TV scope is malformed or outside the supported single-episode range."""


def make_scope(season, episode):
    if (type(season) is not int or not 0 <= season <= 999
            or type(episode) is not int or not 1 <= episode <= 999):
        raise ScopeError('Invalid selected episode')
    return 'episode_s%02de%02d' % (season, episode)


def parse_scope(scope):
    if scope == 'series_s01e01':
        return (1, 1)
    if type(scope) is not str:
        raise ScopeError('Invalid selected scope')
    match = re.fullmatch(r'episode_s([0-9]{2,3})e([0-9]{2,3})', scope)
    if match:
        season, episode = map(int, match.groups())
        if make_scope(season, episode) == scope:
            return season, episode
        raise ScopeError('Noncanonical selected episode')
    if scope.startswith(('episode', 'series')):
        raise ScopeError('Invalid selected episode')
    return None


def episode_code(scope):
    selected = parse_scope(scope)
    return 'S%02dE%02d' % selected if selected else None


def is_episode_scope(scope):
    return parse_scope(scope) is not None
