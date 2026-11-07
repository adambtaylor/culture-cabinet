"""Exact offline identity joins, using normalized synthetic event records."""
from dataclasses import replace
from datetime import timedelta
from unittest import TestCase

from shelf import event_sources as e
from shelf.cross_media_feedback import FactualEdge, FeedbackSnapshot, Reaction, rank
from shelf.discovery_sources import SourceError, Candidate
from shelf.tests.test_event_sources import NOW, official, parse

ARTIST = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


def gig(name='one', **changes):
    event = parse(official(id=name, url=f'https://forummelbourne.com.au/{name}',
        location={'name': 'Synthetic venue', 'address': {'addressLocality': 'Melbourne'}},
        lineup=['Synthetic artist'], **changes), source_id='forum')[0]
    ref = e.LineupReference(ARTIST, event.evidence[0], 'Synthetic verified lineup row 1', .9, verified=True)
    return replace(event, lineup_references=(ref,))


def upcoming(*events, artist=ARTIST, localities=('Melbourne',)):
    return e.artist_upcoming_gigs(artist, list(events), now=NOW, localities=localities)


class ArtistGigTests(TestCase):
    def test_generated_lineup_edge_is_accepted_by_unchanged_reinforcement(self):
        match, = upcoming(gig())
        artist = Candidate('musicbrainz:artist:' + ARTIST, 'Synthetic artist', 'artist',
                           'MusicBrainz', 'fixture', 1, NOW.isoformat())
        snapshot = FeedbackSnapshot(1, 1, 'sample', 'fixture-feedback', 1, (
            Reaction(artist.key, 'enjoyed', 'fixture-response', 1, 'direct', NOW.isoformat()),))
        result = rank([artist, match.event], match.edges, [snapshot], actor_id=1, profile_id=1,
            mode='sample', expected_revisions={'fixture-feedback': 1}, now=NOW)
        self.assertEqual([row['card'].key for row in result['selection']], [match.event.key])
        self.assertEqual(result['selection'][0]['memberships'][0]['path'][0]['relation'], 'lineup')

    def test_exact_artist_join_emits_existing_typed_factual_edge(self):
        item = gig(); match, = upcoming(item)
        edge, = match.edges
        self.assertIsInstance(edge, FactualEdge)
        self.assertEqual((edge.source_key, edge.target_key, edge.relation),
                         (item.key, 'musicbrainz:artist:' + ARTIST, 'lineup'))
        self.assertEqual((edge.source, edge.evidence, edge.confidence),
                         ('forum', item.lineup_references[0].reference, .9))
        self.assertEqual(match.event.evidence, item.evidence)

    def test_names_and_unverified_mbid_assertions_never_join(self):
        item = gig()
        self.assertFalse(upcoming(replace(item, lineup_references=())))
        ref = replace(item.lineup_references[0], verified=False)
        self.assertFalse(upcoming(replace(item, lineup_references=(ref,))))
        self.assertFalse(upcoming(item, artist=OTHER))

    def test_recording_id_namespace_or_noncanonical_artist_id_rejected(self):
        for value in ('musicbrainz:recording:' + ARTIST, 'same artist name', ARTIST.upper().replace('1', 'A', 1), None):
            with self.subTest(value=value), self.assertRaises(SourceError):
                upcoming(gig(), artist=value)

    def test_reference_must_belong_to_exact_event_record(self):
        item = gig(); other = gig('two')
        self.assertFalse(upcoming(replace(item, lineup_references=other.lineup_references)))
        wrong = replace(item.lineup_references[0], record=replace(item.evidence[0], source_id='rrr'))
        self.assertFalse(upcoming(replace(item, lineup_references=(wrong,))))

    def test_duplicate_record_and_reference_produce_one_gig_and_edge(self):
        item = gig()
        duplicated = replace(item, lineup_references=item.lineup_references * 2)
        match, = upcoming(duplicated, duplicated)
        self.assertEqual(len(match.edges), 1)

    def test_exact_url_bridge_deduplicates_guide_venue_and_seller(self):
        item = gig()
        # Reconciled official references are supplied facts, never a title join.
        evidence = replace(item.evidence[0], source_id='rrr', event_id='guide-one')
        guide = replace(item, key='fixture:event:guide-one', source_id='rrr', source_event_id='guide-one',
                        evidence=(evidence,), lineup_references=(replace(item.lineup_references[0], record=evidence),))
        seller_evidence = replace(evidence, source_id='moshtix', event_id='123')
        seller = replace(guide, key='fixture:event:seller-one', source_id='moshtix', source_event_id='123',
            evidence=(seller_evidence,), lineup_references=(replace(guide.lineup_references[0], record=seller_evidence),))
        match, = upcoming(item, guide, seller)
        self.assertEqual({r.source_id for r in match.event.evidence}, {'forum', 'rrr', 'moshtix'})
        self.assertEqual(len(match.edges), 1)

    def test_same_name_and_date_without_source_aliases_remain_separate(self):
        self.assertEqual(len(upcoming(gig('one'), gig('two'))), 2)

    def test_cancellation_and_newer_omitted_lineup_remove_old_match(self):
        item = gig()
        later = (NOW + timedelta(minutes=1)).isoformat()
        evidence = replace(item.evidence[0], retrieved_at=later)
        updated = replace(item, retrieved_at=later, evidence=(evidence,), lineup_references=())
        self.assertFalse(upcoming(item, updated))
        self.assertFalse(upcoming(item, replace(updated, status='cancelled')))

    def test_old_reference_cannot_be_attached_to_new_event_snapshot(self):
        item = gig()
        updated = replace(item, retrieved_at=(NOW + timedelta(minutes=1)).isoformat())
        self.assertFalse(upcoming(updated))

    def test_locality_is_explicit_and_no_distance_is_invented(self):
        self.assertTrue(upcoming(gig(), localities=('melbourne',)))
        self.assertFalse(upcoming(gig(), localities=('Ballarat',)))
        with self.assertRaises(SourceError):
            upcoming(gig(), localities=())

    def test_existing_date_eligibility_and_category_rules_are_reused(self):
        item = gig()
        for change in ({'status': 'cancelled'}, {'status': 'postponed'}, {'eligibility': 'invite'},
                       {'searchable': False}, {'starts_at': '2020-01-01T10:00:00+11:00'},
                       {'starts_at': None, 'date_precision': 'unknown'}, {'category': 'gallery'}):
            with self.subTest(change=change):
                self.assertFalse(upcoming(replace(item, **change)))
        self.assertTrue(upcoming(replace(item, starts_at='2026-11-01', date_precision='date')))

    def test_bounded_inputs_and_unconnected_bandsintown(self):
        item = gig()
        with self.assertRaises(SourceError):
            upcoming(replace(item, lineup_references=item.lineup_references * 26))
        with self.assertRaises(SourceError):
            upcoming(*([item] * 101))
        self.assertNotIn('bandsintown', e.SOURCES)
        self.assertIn('unconnected', e.UNCONNECTED_SERVICES['bandsintown'])
        self.assertTrue({'rrr', 'pbs', 'forum'} <= e.SOURCES.keys())
