"""Pure Apple TV stream selection and evidence validation; never transcodes A/V."""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import uuid
import hashlib
import json
import math
import re
from episode_scope import parse_scope, is_episode_scope


BLOCKED_REASONS = {
    'unsupported_video_codec': 'Choose a source with supported HEVC or H264 video; video transcoding is unavailable.',
    'unsupported_audio_codec': 'Choose English AAC, AC3, or EAC3 audio; Atmos requires EAC3 JOC.',
    'english_audio_required': 'Choose a source with explicitly tagged English non-commentary audio.',
    'ambiguous_audio': 'Resolve the English audio selection before retrying.',
    'unsupported_subtitles': 'Choose English text subtitles; image subtitles need manual treatment.',
    'ambiguous_subtitle_language': 'Resolve untagged subtitle languages before retrying.',
    'unsupported_hdr': 'Choose an unambiguous supported SDR, HDR10, HLG, or HDR10-compatible Dolby Vision source.',
    'insufficient_disk_space': 'Free private remux space for twice the source size plus 256 MiB before reconciliation.',
    'source_verification_failed': 'Reconcile the exact registered source tree and verified bytes before retrying.',
    'remux_reconciliation_required': 'Inspect the durable remux reservation before any retry; a second remux is blocked.',
    'session_expired': 'The approved remux session has expired; obtain renewed authorization.',
    'media_validation_failed': 'Inspect the remux evidence; stream, packet, metadata, or atom validation failed.',
    'invalid_contract': 'Export a fresh authoritative Apple TV completion contract before retrying.',
}


class MediaError(ValueError):
    """A fixed public code accompanies internal diagnostic text."""

    def __init__(self, reason, code='media_validation_failed'):
        super().__init__(reason)
        self.code = code if code in BLOCKED_REASONS else 'media_validation_failed'


def require(condition, reason, code='media_validation_failed'):
    if not condition:
        raise MediaError(reason, code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def validate_contract(contract):
    require(type(contract) is dict and set(contract) == {
        'version', 'destination', 'completion', 'selection', 'episode', 'source', 'contract_sha256'},
        'Exact Apple TV contract required.')
    require(type(contract['version']) is int and contract['version'] == 1
            and contract['destination'] == 'apple_tv', 'Unsupported Apple TV contract.')
    body = {k: v for k, v in contract.items() if k != 'contract_sha256'}
    require(contract['contract_sha256'] == digest(body), 'Contract hash mismatch.')
    selection, completion, source = (contract[k] for k in ('selection', 'completion', 'source'))
    require(type(selection) is dict and set(selection) == {'source_identity', 'provenance', 'nzo_id', 'completion_evidence_sha256'},
            'Exact selected completion binding required.')
    require(type(completion) is dict and type(completion.get('version')) is int and completion['version'] == 2
            and completion.get('state') == 'COMPLETED_FILES_VERIFIED'
            and completion.get('sab_history_completed') is True, 'Verified v2 SAB completion required.')
    identity = selection['source_identity']
    require(type(identity) is dict and set(identity) == {'provider', 'medium', 'source_id', 'scope', 'title', 'edition', 'basis'}
            and identity == completion.get('source_identity') and identity['provider'] == 'tmdb'
            and identity['medium'] == 'tv' and is_episode_scope(identity['scope'])
            and type(identity['source_id']) is int and 0 < identity['source_id'] <= 2147483647
            and identity['basis'] == 'selected_catalogue_identity_not_content_verification'
            and type(identity['edition']) is str and 0 < len(identity['edition'].strip()) <= 500
            and not any(ord(c) < 32 for c in identity['edition']),
            'Selected show identity mismatch.')
    auth = completion.get('authorization', {})
    require(auth.get('authorized_content') is True and auth.get('edition') == identity['edition']
            and auth.get('scope') == identity['scope'], 'Authorized selected scope required.')
    files = completion.get('files')
    require(type(files) is list and len(files) == 1 and type(files[0]) is dict, 'Exactly one verified source required.')
    item = files[0]
    provenance = item.get('provenance')
    require(type(provenance) is dict and set(provenance) == {'request_id', 'result_id', 'result_digest', 'nzb_sha256'}
            and provenance == selection['provenance'] and item.get('nzo_id') == selection['nzo_id'],
            'Exact completion provenance mismatch.')
    for key in ('request_id', 'result_id'):
        require(type(provenance[key]) is str and str(uuid.UUID(provenance[key])) == provenance[key], 'Invalid request/result UUID.')
    for key in ('result_digest', 'nzb_sha256'):
        require(type(provenance[key]) is str and re.fullmatch('[0-9a-f]{64}', provenance[key]), 'Invalid provenance hash.')
    require(type(selection['nzo_id']) is str and len(selection['nzo_id']) <= 112 and re.fullmatch(
        r'(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|SABnzbd_nzo_[A-Za-z0-9_-]{1,100})', selection['nzo_id']), 'Invalid SAB identity.')
    require(item.get('selected_scope') == identity['scope'] and item.get('observed_media') == 'video'
            and item.get('observed_media_basis') == 'filename_extension_only'
            and item.get('media_identity') == 'sha256:' + item['sha256']
            and item.get('content_identity') == 'unverified' and item.get('full_work_verified') is False
            and item.get('playback_started') is False and item.get('indexed') is False
            and item.get('kodi_music_catalog_eligible') is False and item.get('reverify_before_playback') is True
            and type(item.get('test_only')) is bool, 'Unexpected completion semantics.')
    checked, verified, acquired = (datetime.fromisoformat(value.replace('Z', '+00:00')) for value in
                                   (completion['checked_at'], item['verified_at'], item['acquired_at']))
    require(all(value.tzinfo is not None for value in (checked, verified, acquired))
            and acquired.microsecond == 0 and int(verified.timestamp()) <= acquired.timestamp()
            and verified <= checked <= datetime.now(timezone.utc) and acquired <= checked
            and item.get('acquired_at_basis') == 'sab_history_completed_at', 'Invalid SAB completion times.')
    evidence = {'source': 'sab_history', 'nzo_id': item['nzo_id'], 'acquired_at': item['acquired_at'],
                'acquired_at_basis': 'sab_history_completed_at', 'provenance': provenance,
                'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')}], 'source_identity': identity}
    evidence_hash = hashlib.sha256((json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()).hexdigest()
    require(completion.get('completion_evidence') == evidence and evidence_hash == completion.get('completion_evidence_sha256')
            == selection['completion_evidence_sha256'], 'Completion evidence hash mismatch.')
    require(source == {'relative_path': item['relative_path'], 'sha256': item['sha256'], 'bytes': item['size']},
            'Contract source differs from verified completion.')
    episode = contract['episode']
    identity = selection['source_identity']
    require(type(episode) is dict and set(episode) == {'show', 'season', 'episode', 'title', 'source_id', 'basis'}
            and episode['basis'] == 'selected_job_scope' and episode['show'] == identity['title']
            and type(episode['source_id']) is int and episode['source_id'] == identity['source_id'] and type(episode['season']) is int
            and type(episode['episode']) is int and (episode['season'], episode['episode']) == parse_scope(identity['scope']),
            'Authoritative selected episode binding required.')
    episode_metadata(episode)
    relative = PurePosixPath(source['relative_path'])
    request = selection['provenance']['request_id']
    require(str(uuid.UUID(request)) == request and not relative.is_absolute()
            and relative.parts[0] == 'CC-' + request and '..' not in relative.parts and relative.suffix.lower() in ('.mkv', '.mp4', '.webm')
            and str(relative) == source['relative_path'] and 2 <= len(relative.parts) <= 16
            and len(str(relative)) <= 4096 and not any(ord(c) < 32 for c in str(relative)),
            'Source must be in the exact completed request tree.')
    require(type(source['bytes']) is int and 0 < source['bytes'] <= 100 * 1024**3
            and re.fullmatch('[0-9a-f]{64}', source['sha256']), 'Invalid source bytes or hash.')
    return contract


def english(stream):
    return stream.get('tags', {}).get('language', '').lower() in ('eng', 'en')


def atmos(stream):
    return 'atmos' in stream.get('profile', '').lower()


def dovi(stream):
    return [s for s in stream.get('side_data_list', [])
            if s.get('side_data_type') == 'DOVI configuration record']


def episode_metadata(episode):
    require(type(episode) is dict, 'Explicit episode selection required.')
    for key in ('season', 'episode'):
        require(type(episode.get(key)) is int and (0 if key == 'season' else 1) <= episode[key] <= 999,
                'Explicit supported season and episode required.')
    for key in ('show', 'title'):
        require(type(episode.get(key)) is str and 0 < len(episode[key].strip()) <= 300
                and not any(ord(c) < 32 for c in episode[key]), 'Invalid episode ' + key)
    code = 'S%02dE%02d' % (episode['season'], episode['episode'])
    title = episode['show'] + ' - ' + code
    if episode['title'] != code:
        title += ' - ' + episode['title']
    return {'title': title,
            'show': episode['show'], 'season_number': str(episode['season']),
            'episode_sort': str(episode['episode']), 'episode_id': code, 'media_type': '10'}


def plan_media(probe, episode):
    streams = probe.get('streams', [])
    require(type(streams) is list and 0 < len(streams) <= 256, 'Invalid stream inventory.')
    indices = [s.get('index') for s in streams]
    require(all(type(i) is int and i >= 0 for i in indices) and len(set(indices)) == len(indices),
            'Invalid or duplicate stream indices.')
    videos = [s for s in streams if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    require(len(videos) == 1, 'Exactly one video stream required; select a different source.', 'unsupported_video_codec')
    video = videos[0]
    require(video.get('codec_name') in ('hevc', 'h264'), 'Unsupported video codec; no transcode authorized.', 'unsupported_video_codec')
    require(video.get('pix_fmt') in ('yuv420p', 'yuv420p10le'), 'Unsupported video pixel format.', 'unsupported_video_codec')
    if video['codec_name'] == 'h264':
        require(video['pix_fmt'] == 'yuv420p' and video.get('profile') in
                ('Baseline', 'Constrained Baseline', 'Main', 'High'), 'Unsupported H264 profile.', 'unsupported_video_codec')
    else:
        require(video.get('profile') in (None, 'Main', 'Main 10'), 'Unsupported HEVC profile.', 'unsupported_video_codec')
    dv = dovi(video)
    require(len(dv) <= 1, 'Ambiguous Dolby Vision records.', 'unsupported_hdr')
    hdr = video.get('color_transfer') in ('smpte2084', 'arib-std-b67')
    if hdr:
        require(video.get('codec_name') == 'hevc' and video.get('pix_fmt') == 'yuv420p10le'
                and video.get('color_primaries') == 'bt2020'
                and video.get('color_space') == 'bt2020nc', 'Incomplete or unsupported HDR signaling.', 'unsupported_hdr')
    else:
        require(video.get('color_transfer') in (None, 'unknown', 'bt709', 'smpte170m', 'bt470bg')
                and video.get('color_primaries') not in ('bt2020',), 'Ambiguous HDR signaling.', 'unsupported_hdr')
    if dv:
        require(hdr and video.get('color_transfer') == 'smpte2084'
                and dv[0].get('dv_profile') == 8 and dv[0].get('dv_bl_signal_compatibility_id') == 1
                and dv[0].get('bl_present_flag') == 1 and dv[0].get('el_present_flag') == 0
                and dv[0].get('rpu_present_flag') == 1, 'Unsupported Dolby Vision; HDR10 compatible profile 8 required.', 'unsupported_hdr')
    audio = [s for s in streams if s.get('codec_type') == 'audio' and english(s)
             and not s.get('disposition', {}).get('comment')
             and not re.search(r'commentary|audio description|descriptive', s.get('tags', {}).get('title', ''), re.I)
             and not s.get('disposition', {}).get('visual_impaired')]
    require(audio, 'Explicit English non-commentary audio required.', 'english_audio_required')
    preferred = [s for s in audio if atmos(s)] or audio
    if len(preferred) > 1:
        preferred = [s for s in preferred if s.get('disposition', {}).get('default') == 1]
    require(len(preferred) == 1, 'Ambiguous English audio; explicit selection needed.', 'ambiguous_audio')
    audio = preferred[0]
    require(audio.get('codec_name') in ('eac3', 'ac3', 'aac'), 'Unsupported English audio codec; no silent fallback.', 'unsupported_audio_codec')
    require(not atmos(audio) or audio.get('codec_name') == 'eac3', 'Atmos must be EAC3 JOC for this pipeline.', 'unsupported_audio_codec')
    subtitles = [s for s in streams if s.get('codec_type') == 'subtitle' and english(s)]
    require(all(s.get('codec_name') in ('subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text') for s in subtitles),
            'English image or unsupported subtitles need manual treatment.', 'unsupported_subtitles')
    require(all(s.get('tags', {}).get('language', '').lower() not in ('', 'und')
                for s in streams if s.get('codec_type') == 'subtitle'), 'Subtitle language is ambiguous.', 'ambiguous_subtitle_language')
    plan = {'version': 1, 'video': video['index'], 'audio': audio['index'],
            'subtitles': [s['index'] for s in subtitles], 'video_codec': video['codec_name'],
            'audio_codec': audio['codec_name'], 'atmos': atmos(audio), 'hdr': hdr, 'dovi': dv,
            'metadata': episode_metadata(episode), 'episode': dict(episode),
            'subtitle_titles': ['English SDH' if s.get('disposition', {}).get('hearing_impaired')
                                or 'sdh' in s.get('tags', {}).get('title', '').lower() else
                                'English Forced' if s.get('disposition', {}).get('forced') else 'English'
                                for s in subtitles],
            'original_only_streams': [s['index'] for s in streams if s['index'] not in
                                      [video['index'], audio['index']] + [t['index'] for t in subtitles]],
            'subtitle_note': 'English text converted to mov_text; styling may normalize. Other languages remain only in original.',
            'hdr_display_verified': False}
    plan['plan_sha256'] = digest(plan)
    return plan


def ffmpeg_argv(source, output, plan):
    args = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'warning', '-n', '-i', str(source)]
    for index in [plan['video'], plan['audio']] + plan['subtitles']:
        args += ['-map', '0:' + str(index)]
    args += ['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'mov_text', '-tag:v',
             'hvc1' if plan['video_codec'] == 'hevc' else 'avc1', '-disposition:a:0', 'default',
             '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0',
             'title=English Dolby Atmos' if plan['atmos'] else 'title=English']
    for n, title in enumerate(plan['subtitle_titles']):
        args += ['-disposition:s:' + str(n), '0', '-metadata:s:s:' + str(n), 'language=eng',
                 '-metadata:s:s:' + str(n), 'title=' + title]
    args += ['-map_metadata', '-1', '-map_chapters', '0']
    for key, value in plan['metadata'].items():
        args += ['-metadata', key + '=' + value]
    return args + ['-movflags', '+faststart', '-strict', 'unofficial', '-f', 'mp4', str(output)]


def joc_extension(payload):
    """Parse EC3SpecificBox conditional fields before the optional JOC extension."""
    bits = ''.join(format(b, '08b') for b in payload)
    require(len(bits) >= 16, 'Truncated dec3 atom.')
    count, offset = int(bits[13:16], 2) + 1, 16
    for _ in range(count):
        require(len(bits) >= offset + 23, 'Truncated EAC3 substream.')
        dependencies = int(bits[offset + 19:offset + 23], 2)
        offset += 23 + (9 if dependencies else 1)
    return len(bits) >= offset + 16 and bits[offset + 7] == '1' and int(bits[offset + 8:offset + 16], 2) > 0


def validate_media(source, derivative, plan, packet_evidence, atoms):
    """Packet evidence is generated by comparing ordered SHA256 packet payload hashes."""
    require(plan_media(source, plan['episode']) == plan, 'Source selection or plan changed.')
    src = {s['index']: s for s in source['streams']}
    videos = [s for s in derivative['streams'] if s.get('codec_type') == 'video']
    audios = [s for s in derivative['streams'] if s.get('codec_type') == 'audio']
    subs = [s for s in derivative['streams'] if s.get('codec_type') == 'subtitle']
    require(len(videos) == len(audios) == 1 and len(subs) == len(plan['subtitles']), 'Derivative stream inventory mismatch.')
    v, a = videos[0], audios[0]
    require(v.get('codec_tag_string') == ('hvc1' if plan['video_codec'] == 'hevc' else 'avc1'), 'Wrong video sample entry.')
    for label, original, result, keys in (
            ('video', src[plan['video']], v, ('codec_name', 'width', 'height', 'pix_fmt', 'color_range',
                                           'color_space', 'color_transfer', 'color_primaries')),
            ('audio', src[plan['audio']], a, ('codec_name', 'profile', 'sample_rate', 'channels', 'channel_layout'))):
        require(all(original.get(k) == result.get(k) for k in keys), label + ' metadata changed.')
        proof = packet_evidence.get(label, {})
        require(type(proof.get('source_packets')) is int and proof['source_packets'] > 0
                and proof.get('derivative_packets') == proof['source_packets']
                and proof.get('identical_packet_payload_hashes') is True, label + ' packet payload mismatch.')
    require(english(a) and a.get('disposition', {}).get('default') == 1, 'English default audio not retained.')
    require(all(english(s) and s.get('codec_name') == 'mov_text' for s in subs), 'Subtitle conversion mismatch.')
    require([s.get('tags', {}).get('name', s.get('tags', {}).get('title')) for s in subs]
            == plan['subtitle_titles'], 'Subtitle role metadata changed.')
    for key, value in plan['metadata'].items():
        require(derivative.get('format', {}).get('tags', {}).get(key) == value, 'Episode metadata mismatch: ' + key)
    durations = [float(p.get('format', {}).get('duration', 0)) for p in (source, derivative)]
    require(all(math.isfinite(d) and d > 0 for d in durations) and abs(durations[0] - durations[1]) < 1,
            'Duration changed or unavailable.')
    hdr_types = {'Mastering display metadata', 'Content light level metadata', 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}
    original_hdr = [item for item in src[plan['video']].get('side_data_list', []) if item.get('side_data_type') in hdr_types]
    derivative_hdr = [item for item in v.get('side_data_list', []) if item.get('side_data_type') in hdr_types]
    require(original_hdr == derivative_hdr, 'HDR mastering/content metadata changed.')
    require(dovi(v) == plan['dovi'], 'Dolby Vision metadata changed.')
    require(atoms.get('video_sample_entry') == v['codec_tag_string']
            and atoms.get('faststart') is True, 'MP4 sample entry/faststart missing.')
    if plan['hdr']:
        require(bool(atoms.get('colr')), 'HDR color atom missing.')
    if plan['dovi']:
        require(bool(atoms.get('dvvC') or atoms.get('dvcC')), 'Dolby Vision configuration atom missing.')
    audio_entry = {'eac3': 'ec-3', 'ac3': 'ac-3', 'aac': 'mp4a'}[plan['audio_codec']]
    require(a.get('codec_tag_string') == audio_entry and atoms.get('audio_sample_entry') == audio_entry,
            'Wrong audio sample entry.')
    if plan['atmos']:
        dec3 = bytes.fromhex(atoms.get('dec3', ''))
        require(len(dec3) >= 8 and int.from_bytes(dec3[:4], 'big') == len(dec3)
                and dec3[4:8] == b'dec3' and joc_extension(dec3[8:])
                and atmos(a) and atoms.get('joc_extension_retained') is True, 'Atmos JOC dec3 extension missing.')
    return {'valid': True, 'plan_sha256': plan['plan_sha256'], 'packet_integrity': packet_evidence,
            'duration_seconds': durations[1], 'metadata': plan['metadata'], 'atmos': plan['atmos'],
            'hdr': plan['hdr'], 'hdr_display_verified': False, 'dovi': plan['dovi'], 'atoms': atoms,
            'subtitle_note': plan['subtitle_note']}
