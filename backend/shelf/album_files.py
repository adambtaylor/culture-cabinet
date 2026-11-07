"""Bounded album files: audio tracks, local artwork and opaque CUE evidence."""
from pathlib import PurePosixPath
import re

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png'}
MAX_IMAGE_BYTES = 20 * 1024**2
MAX_CUE_BYTES = 1024**2
MAX_ALBUM_BYTES = 20 * 1024**3


def file_kind(path, size):
    if (type(path) is not str or len(path) > 4096 or '\\' in path
            or any(ord(c) < 32 for c in path) or path.startswith('/')
            or any(part in ('', '.', '..') for part in path.split('/'))
            or len(path.split('/')) > 16 or type(size) is not int or size <= 0):
        raise ValueError('Invalid album file')
    suffix = PurePosixPath(path).suffix.lower()
    kind, limit = (('track', MAX_ALBUM_BYTES) if suffix == '.flac' else
                   ('art', MAX_IMAGE_BYTES) if suffix in IMAGE_SUFFIXES else
                   ('cue', MAX_CUE_BYTES) if suffix == '.cue' else (None, 0))
    if kind is None or size > limit:
        raise ValueError('Unsupported or oversized album sidecar')
    return kind


def validate_manifest(files, track_count):
    if (type(files) is not list or not 0 < len(files) <= 128
            or type(track_count) is not int or not 1 <= track_count <= 128):
        raise ValueError('Bounded complete album manifest required')
    counts = {'track': 0, 'art': 0, 'cue': 0}
    paths, track_hashes, total = set(), set(), 0
    for item in files:
        if type(item) is not dict or set(item) != {'path', 'bytes', 'sha256'}:
            raise ValueError('Exact album manifest fields required')
        kind = file_kind(item['path'], item['bytes'])
        path = item['path'].casefold()
        sha = item['sha256']
        if path in paths or type(sha) is not str or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('Conflicting album manifest')
        paths.add(path)
        if kind == 'track':
            if sha in track_hashes:
                raise ValueError('Duplicate track bytes')
            track_hashes.add(sha)
        total += item['bytes']
        counts[kind] += 1
    if (counts['track'] != track_count or counts['art'] > 16 or counts['cue'] > 4
            or total > MAX_ALBUM_BYTES):
        raise ValueError('Declared complete album track set unavailable')
    return counts
