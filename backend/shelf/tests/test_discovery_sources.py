"""Synthetic public contracts only; no network or private provider data."""
from datetime import datetime, timezone
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from shelf import discovery_sources as s

ARTIST = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
RECORDING = '33333333-3333-4333-8333-333333333333'
FETCHED = '2026-10-28T10:00:00+00:00'


def radio():
    return {OTHER: [{'recording_mbid': RECORDING, 'similar_artist_mbid': OTHER,
                     'similar_artist_name': 'Synthetic artist', 'total_listen_count': 12}]}


def radio_groups(other_count, *, seed_position=None):
    artists = [f'{index:08x}-4444-4444-8444-444444444444'
               for index in range(1, other_count + 1)]
    if seed_position is not None:
        artists.insert(seed_position, ARTIST)
    return {
        artist: [{'recording_mbid': f'{index:08x}-5555-4555-8555-{position:012x}',
                  'similar_artist_mbid': artist,
                  'similar_artist_name': f'Synthetic artist {index}',
                  'total_listen_count': position - 1}
                 for position in range(1, 4)]
        for index, artist in enumerate(artists, 1)
    }


def events():
    return {'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [
        {'id': '123', 'name': 'SYNTHETIC: Night & Day!', 'startDate': '2027-01-01T20:00:00+11:00',
         'venue': {'name': 'Synthetic venue', 'address': {'locality': 'Synthetic locality'}}}
    ]}}}}


class DiscoveryIdentityTests(TestCase):
    def test_artwork_budget_exact_boundary_unknown_and_admission(self):
        self.assertEqual(s.artwork_affordability('2499.99', 'AUD', per='individual artwork'), 'within budget')
        self.assertEqual(s.artwork_affordability('2500', 'AUD', per='individual artwork'), 'outside budget')
        for price, currency, per in [(None, 'AUD', 'individual artwork'), ('10', 'AUD', 'admission'),
                                    ('100', 'USD', 'individual artwork'), ('NaN', 'AUD', 'individual artwork')]:
            self.assertEqual(s.artwork_affordability(price, currency, per=per), 'unknown')

    def test_packaged_updates_preserve_attribution_and_pending_mapping(self):
        updates = s.registry()['verified_updates'][0]
        self.assertEqual(updates['provider_ids'][0]['venue_id'], 2362)
        self.assertFalse(updates['unconfirmed'][0]['mapping_confirmed'])
        self.assertIn('inferred-adjacent', {r['attribution'] for r in updates['sources']})

    def test_namespaces_and_kinds_do_not_collide(self):
        keys = {s.identity('musicbrainz', 'artist', ARTIST), s.identity('musicbrainz', 'recording', ARTIST),
                s.identity('fixture', 'artist', ARTIST), s.identity('moshtix', 'event', 123)}
        self.assertEqual(len(keys), 4)
        for args in [('musicbrainz', 'artist', 123), ('musicbrainz', 'artist', '123'),
                     ('tmdb', 'artist', 123), ('moshtix', 'event', True),
                     ('moshtix', 'event', -1), ('official', 'venue', '../private')]:
            with self.subTest(args=args), self.assertRaises(s.SourceError):
                s.identity(*args)

    def test_reported_seeds_are_unconfirmed_not_ratings(self):
        cards = s.household_candidates()
        self.assertEqual(len([c for c in cards if c.kind == 'artist']), 5)
        self.assertTrue(all(c.provenance == 'manual-attributed' for c in cards))
        self.assertTrue(all(c.retrieved_at is None for c in cards))
        registry = s.registry()
        deribin = next(r for r in registry['event_sources'] if r['raw_label'] == 'deribin theater')
        self.assertEqual(deribin['candidate_mapping'], 'Darebin Arts')
        self.assertEqual(deribin['verification'], 'unconfirmed-do-not-auto-map')
        self.assertEqual({r['kind'] for r in registry['event_sources']},
                         {'venue', 'radio_editorial', 'festival', 'public_API', 'discount_overlay', 'grassroots_music'})

    def test_mindbuffer_is_manual_official_seed_without_provider_or_inventory_join(self):
        cards = s.household_candidates()
        card, = [c for c in cards if c.key == 'official:artist:mindbuffer']
        self.assertEqual(card.title, 'MindBuffer')
        self.assertEqual(card.kind, 'artist')
        self.assertEqual(card.url, 'https://mindbuffer.net/works/')
        self.assertEqual(card.provenance, 'manual-attributed')
        self.assertIn('AV studio/performance seed', card.source)
        self.assertIn('matching provider identity unverified', card.source)
        self.assertIsNone(card.retrieved_at)
        self.assertIsNone(card.starts_at)
        self.assertIsNone(card.artist_key)
        self.assertIsNone(card.venue_key)
        self.assertEqual(card.roots, ())
        self.assertEqual(card.lineage, ())
        self.assertFalse(any(card.key in c.roots or c.artist_key == card.key
                             or c.venue_key == card.key for c in cards))
        with self.assertRaises(s.SourceError):
            s.radio_request('mindbuffer')
        self.assertEqual({c.key for c in cards if c.key.startswith('musicbrainz:artist:')}, {
            'musicbrainz:artist:f59c5520-5f46-4d2c-b2c4-822eabf53419',
            'musicbrainz:artist:d16f9fc8-fbba-4276-9b30-86b8f5ef5938',
            'musicbrainz:artist:0bfb9d2b-e732-4f28-b2fe-e1d3db9665f1',
            'musicbrainz:artist:d530dbf9-69b1-404f-98f2-0431104f5a1e',
        })

    def test_mindbuffer_confirmation_preserves_distinct_people_and_past_credit(self):
        seed, = s.registry()['manual_artists']
        self.assertNotIn('mbid', seed)
        self.assertEqual(seed['confirmation']['by'], 'Example curator')
        self.assertEqual(seed['confirmation']['at'], '2026-01-01T00:00:00Z')
        self.assertIn('no experienced enjoyment or attendance confirmed', seed['confirmation']['scope'])
        self.assertEqual(seed['unresolved_friend']['raw_label'], 'Unresolved example person')
        self.assertFalse(seed['unresolved_friend']['mapping_confirmed'])
        self.assertEqual(seed['studio_reference']['founders'], ['Joshua Batty', 'Mitchell Nordine'])
        past = seed['past_production_reference']
        self.assertIn('/past-productions/', past['url'])
        self.assertEqual(past['credit'], 'Josh Cohen: piano; Joshua Batty of MindBuffer: realtime AV')
        self.assertIn('not a future listing', past['status'])

    def test_sample_is_wholly_separate_and_has_no_fictional_provider_links(self):
        real = {c.key for c in s.household_candidates()}
        sample = s.sample_candidates()
        self.assertTrue(all(c.provenance == 'fixture' and c.key.startswith('fixture:') and c.url is None for c in sample))
        self.assertFalse(real & {c.key for c in sample})
        self.assertTrue(all(r.startswith('fixture:') for c in sample for r in c.roots))

    def test_all_live_sources_honestly_gated(self):
        statuses = {r['name']: r for r in s.source_statuses()}
        self.assertEqual(statuses['MusicBrainz']['status'], 'blocked')
        self.assertIn('operator-approved', statuses['MusicBrainz']['detail'])
        self.assertIn('explicit POST available', statuses['Moshtix']['status'])
        self.assertIn('eventStartDateFrom/eventStartDateTo use Date', statuses['Moshtix']['detail'])
        self.assertEqual(statuses['Ticketmaster']['status'], 'blocked')


class DiscoveryParserTests(TestCase):
    def test_musicbrainz_projects_only_cc0_core_identity(self):
        payload = {'artists': [{'id': ARTIST, 'name': 'Synthetic artist', 'rating': {'value': 5},
                                'tags': [{'name': 'private taste'}], 'artwork': 'https://example.invalid'}]}
        card, = s.parse_musicbrainz(payload, 'artist', FETCHED)
        self.assertEqual(card.key, 'musicbrainz:artist:' + ARTIST)
        self.assertEqual(card.url, 'https://musicbrainz.org/artist/' + ARTIST)
        self.assertNotIn('private taste', repr(card))
        recording, = s.parse_musicbrainz({'id': RECORDING, 'title': 'Synthetic recording'}, 'recording', FETCHED)
        self.assertEqual(recording.kind, 'recording')
        self.assertEqual(s.parse_musicbrainz({'artists': []}, 'artist', FETCHED), [])

    def test_radio_documented_dictionary_and_artist_seed(self):
        card, = s.parse_radio(radio(), ARTIST, FETCHED)
        self.assertEqual(card.roots, ('musicbrainz:artist:' + ARTIST,))
        self.assertEqual(card.key, 'musicbrainz:recording:' + RECORDING)
        self.assertEqual(card.source, 'ListenBrainz artist radio')
        self.assertNotIn('people', card.title)
        self.assertNotIn('12', card.title)

    def test_radio_seed_plus_five_others_projects_all_eighteen_in_source_order(self):
        root = 'musicbrainz:artist:' + ARTIST
        for seed_position in (0, 3, 5):
            with self.subTest(seed_position=seed_position):
                document = radio_groups(5, seed_position=seed_position)
                cards = s.parse_radio(s.strict_json(json.dumps(document).encode()), ARTIST, FETCHED)
                self.assertEqual(len(cards), 18)
                self.assertEqual(len({card.key for card in cards}), 18)
                self.assertEqual(sum(card.artist_key == root for card in cards), 3)
                rows = [(artist, row) for artist, recordings in document.items() for row in recordings]
                for position, (card, (artist, row)) in enumerate(zip(cards, rows), 1):
                    self.assertEqual(card.key, 'musicbrainz:recording:' + row['recording_mbid'])
                    self.assertEqual(card.url, 'https://musicbrainz.org/recording/' + row['recording_mbid'])
                    self.assertEqual(card.artist_key, 'musicbrainz:artist:' + artist)
                    self.assertEqual(card.roots, (root,))
                    self.assertEqual(card.lineage, ((root, position),))
                    self.assertEqual(card.rank, position)
                    self.assertEqual(card.title, f'Recording by {row["similar_artist_name"]} (title unavailable)')
                    self.assertEqual(card.kind, 'recording')
                    self.assertEqual(card.source, 'ListenBrainz artist radio')
                    self.assertEqual(card.provenance, 'live-automated')
                    self.assertEqual(card.retrieved_at, FETCHED)

    def test_radio_five_other_groups_without_seed_are_within_budget(self):
        cards = s.parse_radio(radio_groups(5), ARTIST, FETCHED)
        self.assertEqual(len(cards), 15)
        self.assertEqual(len({card.artist_key for card in cards}), 5)
        self.assertTrue(all(card.artist_key != 'musicbrainz:artist:' + ARTIST for card in cards))

    def test_radio_six_nonseed_groups_exceed_budget(self):
        with self.assertRaises(s.SourceError):
            s.parse_radio(radio_groups(6), ARTIST, FETCHED)

    def test_radio_seed_plus_six_other_groups_exceed_budget(self):
        with self.assertRaises(s.SourceError):
            s.parse_radio(radio_groups(6, seed_position=0), ARTIST, FETCHED)

    def test_radio_sixth_seed_group_keeps_recording_and_identity_validation(self):
        mutations = [
            ('similar_artist_mbid', OTHER),
            ('similar_artist_name', ''),
            ('recording_mbid', 'not-a-uuid'),
            ('recording_mbid', RECORDING.replace('-', '')),
            *[('total_listen_count', count) for count in (-1, True, 1.5, '12', None)],
        ]
        for field, value in mutations:
            document = radio_groups(5, seed_position=5)
            document[ARTIST][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(s.SourceError):
                s.parse_radio(document, ARTIST, FETCHED)
        for recordings in (None, {}, [None], radio_groups(0, seed_position=0)[ARTIST] * 2):
            document = radio_groups(5, seed_position=5)
            document[ARTIST] = recordings
            with self.subTest(recordings=recordings), self.assertRaises(s.SourceError):
                s.parse_radio(document, ARTIST, FETCHED)
        document = radio_groups(5, seed_position=5)
        artist = next(iter(document))
        invalid_artist = artist.replace('-', '')
        document[invalid_artist] = document.pop(artist)
        for row in document[invalid_artist]:
            row['similar_artist_mbid'] = invalid_artist
        with self.assertRaises(s.SourceError):
            s.parse_radio(document, ARTIST, FETCHED)
        with self.assertRaises(s.SourceError):
            s.parse_radio(radio_groups(5, seed_position=5), ARTIST.replace('-', ''), FETCHED)

    def test_radio_seed_plus_five_others_keeps_response_byte_budget(self):
        raw = json.dumps(radio_groups(5, seed_position=5)).encode()
        self.assertLess(len(raw), s.MAX_BYTES)
        bounded = raw + b' ' * (s.MAX_BYTES - len(raw))
        for body, expected, count in ((bounded, 'ready', 18), (bounded + b' ', 'invalid', 0)):
            with self.subTest(bytes=len(body)), patch('shelf.discovery_transport.radio', return_value=(200, body)) as send:
                result = s.fetch_public('radio', ARTIST, 'medium', datetime.fromisoformat(FETCHED))
                self.assertEqual(result.status, expected)
                self.assertEqual(len(result.candidates), count)
                send.assert_called_once_with(ARTIST, 'medium')

    def test_radio_mismatched_artist_and_budgets_fail(self):
        invalid = radio(); invalid[OTHER][0]['similar_artist_mbid'] = ARTIST
        with self.assertRaises(s.SourceError): s.parse_radio(invalid, ARTIST, FETCHED)
        with self.assertRaises(s.SourceError): s.parse_radio({OTHER: radio()[OTHER] * 4}, ARTIST, FETCHED)
        with self.assertRaises(s.SourceError): s.parse_radio([], ARTIST, FETCHED)

    def test_fixed_radio_transport_and_fixture_labels(self):
        transport = Mock(return_value=(200, json.dumps(radio()).encode()))
        result = s.fetch_radio_fixture(transport, ARTIST, FETCHED)
        request = transport.call_args.args[0]
        self.assertEqual(request.url, 'https://api.listenbrainz.org/1/lb-radio/artist/' + ARTIST)
        self.assertEqual(dict(request.params)['max_similar_artists'], 5)
        self.assertEqual(request.timeout_seconds, 5)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.candidates[0].provenance, 'fixture')
        self.assertIsNone(result.candidates[0].url)
        self.assertTrue(result.candidates[0].key.startswith('fixture:'))
        self.assertEqual(transport.call_count, 1)

    def test_empty_errors_rate_timeout_invalid_are_distinct_and_no_retries(self):
        for response, expected in [((200, b'{}'), 'empty'), ((503, b''), 'error'),
                                   ((429, b''), 'rate-limited'), ((200, b'{'), 'invalid'),
                                   ((200, b'X' * (s.MAX_BYTES + 1)), 'invalid')]:
            transport = Mock(return_value=response)
            self.assertEqual(s.fetch_radio_fixture(transport, ARTIST, FETCHED).status, expected)
            self.assertEqual(transport.call_count, 1)
        transport = Mock(side_effect=TimeoutError)
        self.assertEqual(s.fetch_radio_fixture(transport, ARTIST, FETCHED).status, 'timeout')
        self.assertEqual(transport.call_count, 1)

    def test_event_identity_link_and_no_venue_title_join(self):
        card, = s.parse_events(events(), FETCHED)
        self.assertEqual(card.key, 'moshtix:event:123')
        self.assertEqual(card.url, 'https://moshtix.com.au/v2/event/synthetic-night-day/123')
        self.assertIsNone(card.venue_key)
        self.assertEqual(card.roots, ())
        self.assertEqual(card.locality, 'Synthetic locality')
        self.assertEqual(card.category, 'all')

    def test_events_errors_are_not_empty_and_ids_are_positive(self):
        empty = events(); empty['data']['viewer']['getEvents']['items'] = []
        empty['data']['viewer']['getEvents']['totalCount'] = 0
        self.assertEqual(s.parse_events(empty, FETCHED), [])
        with self.assertRaises(s.SourceError): s.parse_events({'errors': [{'message': 'synthetic'}]}, FETCHED)
        bad = events(); bad['data']['viewer']['getEvents']['items'][0]['id'] = -1
        with self.assertRaises(s.SourceError): s.parse_events(bad, FETCHED)

    def test_dates_timezone_staleness_and_unknown_distance(self):
        card, = s.parse_events(events(), FETCHED)
        now = datetime(2026, 10, 28, 10, 10, tzinfo=timezone.utc)
        self.assertIn('Recently retrieved', card.freshness(now))
        self.assertIn('Future', card.date_status(now))
        later = datetime(2027, 2, 1, tzinfo=timezone.utc)
        self.assertIn('Stale', card.freshness(later))
        self.assertIn('Past', card.date_status(later))
        for value in ('2027-01-01T20:00:00', 'invalid', None):
            payload = events(); payload['data']['viewer']['getEvents']['items'][0]['startDate'] = value
            parsed, = s.parse_events(payload, FETCHED)
            self.assertIsNone(parsed.starts_at)
            self.assertEqual(parsed.date_status(now), 'Date/timezone unknown')
        self.assertIsNone(s.timestamp('2027-02-30T20:00:00+11:00'))
