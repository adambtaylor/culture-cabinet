"""Synthetic selection failures and a read-only regression of the successful probe."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

import apple_tv_media as media

EPISODE = {'show': 'Synthetic Show', 'season': 2, 'episode': 7, 'title': 'S02E07',
           'source_id': 1, 'basis': 'explicit_episode_selection'}


def source_probe():
    return {'streams': [
        {'index': 0, 'codec_type': 'video', 'codec_name': 'hevc', 'pix_fmt': 'yuv420p10le',
         'width': 3840, 'height': 1920, 'color_range': 'tv', 'color_space': 'bt2020nc',
         'color_transfer': 'smpte2084', 'color_primaries': 'bt2020',
         'side_data_list': [{'side_data_type': 'DOVI configuration record', 'dv_profile': 8,
                             'dv_bl_signal_compatibility_id': 1, 'bl_present_flag': 1,
                             'el_present_flag': 0, 'rpu_present_flag': 1}]},
        {'index': 1, 'codec_type': 'audio', 'codec_name': 'eac3', 'profile': 'Dolby Atmos',
         'sample_rate': '48000', 'channels': 6, 'channel_layout': '5.1',
         'tags': {'language': 'por'}, 'disposition': {'default': 1}},
        {'index': 2, 'codec_type': 'audio', 'codec_name': 'eac3', 'profile': 'Dolby Atmos',
         'sample_rate': '48000', 'channels': 6, 'channel_layout': '5.1',
         'tags': {'language': 'eng'}, 'disposition': {'default': 0}},
        {'index': 6, 'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'eng'}},
        {'index': 7, 'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'eng'},
         'disposition': {'hearing_impaired': 1}}], 'format': {'duration': '120.032'}}


def derivative_probe(source, plan):
    streams = deepcopy([source['streams'][0], source['streams'][2], *source['streams'][3:]])
    for index, stream in enumerate(streams):
        stream['index'] = index
    streams[0]['codec_tag_string'] = 'hvc1'
    streams[1]['disposition']['default'] = 1
    streams[1]['codec_tag_string'] = 'ec-3'
    for index, stream in enumerate(streams[2:]):
        stream['codec_name'] = 'mov_text'
        stream['tags']['name'] = plan['subtitle_titles'][index]
    return {'streams': streams, 'format': {'duration': '120.032', 'tags': plan['metadata']}}


PACKETS = {label: {'source_packets': 4, 'derivative_packets': 4, 'identical_packet_payload_hashes': True}
           for label in ('video', 'audio')}
ATOMS = {'dec3': '0000000f646563331800200f000110', 'audio_sample_entry': 'ec-3', 'video_sample_entry': 'hvc1', 'faststart': True, 'colr': '00', 'dvvC': '00',
         'joc_extension_retained': True}


class MediaTests(unittest.TestCase):
    def test_explicit_contract_binds_selected_episode_even_after_rehash(self):
        from test_apple_tv_remux import contract_fixture
        from test_mac_episode_consumer import fixture
        completion, selection = fixture('episode_s02e02')
        value = contract_fixture()
        value.update(completion=completion, selection=selection)
        value['episode'].update(season=2, episode=2, title='S02E02')
        def rehash(contract):
            contract['contract_sha256'] = media.digest({k: v for k, v in contract.items() if k != 'contract_sha256'})
        rehash(value)
        self.assertEqual(media.validate_contract(value), value)
        self.assertEqual(media.episode_metadata(value['episode'])['episode_id'], 'S02E02')
        for change in (lambda v: v['episode'].update(episode=1),
                       lambda v: v['episode'].update(show='Foreign show'),
                       lambda v: v['completion']['authorization'].update(scope='series_s01e01')):
            bad = deepcopy(value)
            change(bad)
            rehash(bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                media.validate_contract(bad)

    def test_selects_english_atmos_and_both_text_subtitles(self):
        plan = media.plan_media(source_probe(), EPISODE)
        self.assertEqual((plan['video'], plan['audio'], plan['subtitles']), (0, 2, [6, 7]))
        args = media.ffmpeg_argv('/source', '/output', plan)
        self.assertEqual([args[i + 1] for i, v in enumerate(args) if v == '-map'], ['0:0', '0:2', '0:6', '0:7'])
        self.assertIn('season_number=2', args)
        self.assertIn('episode_sort=7', args)
        self.assertEqual(plan['original_only_streams'], [1])
        self.assertEqual(args[args.index('-c:v') + 1], 'copy')
        self.assertEqual(args[args.index('-c:a') + 1], 'copy')

    def test_refuses_missing_language_commentary_unsupported_codec_subtitle_and_hdr(self):
        mutations = [lambda p: p['streams'][2]['tags'].update(language='und'),
                     lambda p: p['streams'][2]['tags'].update(title='Director Commentary'),
                     lambda p: p['streams'][2].update(codec_name='truehd'),
                     lambda p: p['streams'][3].update(codec_name='hdmv_pgs_subtitle'),
                     lambda p: p['streams'][3]['tags'].update(language='und'),
                     lambda p: p['streams'][0].update(color_primaries='unknown'),
                     lambda p: p['streams'][0]['side_data_list'][0].update(dv_profile=5),
                     lambda p: p['streams'][0]['side_data_list'][0].update(el_present_flag=1)]
        for mutate in mutations:
            probe = source_probe()
            mutate(probe)
            with self.subTest(mutate=mutate), self.assertRaises(media.MediaError):
                media.plan_media(probe, EPISODE)

    def test_contract_sab_id_matches_existing_registry_grammar(self):
        from test_apple_tv_remux import contract_fixture

        def bound_contract(nzo_id):
            value = contract_fixture()
            completion, selection = value['completion'], value['selection']
            selection['nzo_id'] = completion['files'][0]['nzo_id'] = nzo_id
            completion['completion_evidence']['nzo_id'] = nzo_id
            evidence = json.dumps(completion['completion_evidence'], sort_keys=True,
                                  separators=(',', ':'), allow_nan=False) + '\n'
            evidence_hash = hashlib.sha256(evidence.encode()).hexdigest()
            completion['completion_evidence_sha256'] = selection['completion_evidence_sha256'] = evidence_hash
            value['contract_sha256'] = media.digest({k: v for k, v in value.items() if k != 'contract_sha256'})
            return value

        for valid in ('SABnzbd_nzo_runtime_fixture', 'SABnzbd_nzo_runtime-fixture_123',
                      'SABnzbd_nzo_' + 'a' * 100, '11111111-2222-3333-4444-555555555555'):
            with self.subTest(valid=valid):
                value = bound_contract(valid)
                self.assertEqual(media.validate_contract(value), value)
        for invalid in ('SABnzbd_nzo_' + 'a' * 101, 'SABnzbd_nzo_',
                        'SABnzbd_nzo_bad/slash', 'SABnzbd_nzo_bad space',
                        'ABCDEFAB-2222-3333-4444-555555555555'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(media.MediaError, 'Invalid SAB identity'):
                media.validate_contract(bound_contract(invalid))

    def test_policy_refusals_have_specific_actionable_codes(self):
        cases = [('english_audio_required', lambda p: p['streams'][2]['tags'].update(language='und')),
                 ('unsupported_audio_codec', lambda p: p['streams'][2].update(codec_name='truehd')),
                 ('unsupported_video_codec', lambda p: p['streams'][0].update(codec_name='vp9')),
                 ('unsupported_subtitles', lambda p: p['streams'][3].update(codec_name='hdmv_pgs_subtitle')),
                 ('ambiguous_subtitle_language', lambda p: p['streams'][3]['tags'].update(language='und')),
                 ('unsupported_hdr', lambda p: p['streams'][0]['side_data_list'][0].update(dv_profile=5))]
        for code, mutate in cases:
            source = source_probe()
            mutate(source)
            with self.subTest(code=code), self.assertRaises(media.MediaError) as raised:
                media.plan_media(source, EPISODE)
            self.assertEqual(raised.exception.code, code)
            self.assertTrue(media.BLOCKED_REASONS[code])

    def test_ambiguous_english_atmos_refused_and_atmos_preferred(self):
        probe = source_probe()
        alternate = deepcopy(probe['streams'][2])
        alternate['index'] = 9
        probe['streams'].append(alternate)
        with self.assertRaisesRegex(media.MediaError, 'Ambiguous'):
            media.plan_media(probe, EPISODE)
        alternate.update(codec_name='aac', profile='LC')
        self.assertEqual(media.plan_media(probe, EPISODE)['audio'], 2)

    def test_validates_packets_metadata_hdr_dovi_and_atmos(self):
        source = source_probe()
        plan = media.plan_media(source, EPISODE)
        derivative = derivative_probe(source, plan)
        result = media.validate_media(source, derivative, plan, PACKETS, ATOMS)
        self.assertTrue(result['valid'])
        for location, key, value in [('packets', 'identical_packet_payload_hashes', False),
                                     ('atoms', 'joc_extension_retained', False),
                                     ('atoms', 'dvvC', None), ('atoms', 'colr', None),
                                     ('atoms', 'dec3', ''), ('atoms', 'dec3', '0000000f646563331800200f000000')]:
            packets, atoms = deepcopy(PACKETS), deepcopy(ATOMS)
            (packets['audio'] if location == 'packets' else atoms)[key] = value
            with self.subTest(key=key), self.assertRaises(media.MediaError):
                media.validate_media(source, derivative, plan, packets, atoms)
        derivative['format']['tags'] = dict(plan['metadata'], episode_sort='8')
        with self.assertRaisesRegex(media.MediaError, 'Episode metadata'):
            media.validate_media(source, derivative, plan, PACKETS, ATOMS)

    def test_successful_actual_probe_regression_when_evidence_present(self):
        evidence = Path(__file__).resolve().parents[3] / 'task' / 'apple-tv-import'
        if not evidence.exists():
            self.skipTest('Read-only prior diagnostic evidence is not part of distribution.')
        source = json.loads((evidence / 'source-probe.json').read_text())
        plan = media.plan_media(source, dict(EPISODE, show='Dark Matter (2024)', season=2, episode=1, title='S02E01'))
        self.assertEqual((plan['video'], plan['audio'], plan['subtitles']), (0, 2, [6, 7]))
        self.assertEqual(plan['metadata']['episode_id'], 'S02E01')
        self.assertTrue(plan['atmos'] and plan['hdr'])
        self.assertEqual(plan['dovi'][0]['dv_bl_signal_compatibility_id'], 1)
        derivative = json.loads((evidence / 'derivative-probe.json').read_text())
        verification = json.loads((evidence / 'verification.json').read_text())
        atoms = json.loads((evidence / 'mp4-atoms.json').read_text())
        atoms.update(video_sample_entry='hvc1', audio_sample_entry='ec-3', faststart=True, joc_extension_retained=True)
        plan = media.plan_media(source, dict(EPISODE, show='Dark Matter (2024)', season=2, episode=1, title='4K HDR Atmos English'))
        packets = {'video': verification['packet_integrity']['video'],
                   'audio': verification['packet_integrity']['english_audio']}
        self.assertTrue(media.validate_media(source, derivative, plan, packets, atoms)['valid'])


if __name__ == '__main__':
    unittest.main()
