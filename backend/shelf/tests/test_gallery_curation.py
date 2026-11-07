"""Editorial reasons remain scoped hypotheses, never durable taste data."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase

from shelf import gallery_curation as c


def card(key, artists=(), url='https://example.test/show'):
    return SimpleNamespace(key=key, category='gallery', url=url,
                           gallery_data={'artists': list(artists), 'source_topics': [], 'media': []})


class GalleryCurationTests(TestCase):
    def test_owner_profile_boundary_includes_boolean_identity_trap(self):
        for actor, profile in ((2, 1), (1, 2), (1, 3), (1, '1'), (True, 1), (1, True), (None, 1)):
            with self.subTest(actor=actor, profile=profile):
                self.assertEqual(c.reasons(card('x'), actor_id=actor, profile_id=profile), ())
                self.assertEqual(c.principle_quotes(actor_id=actor, profile_id=profile), ())
                self.assertEqual(c.select([card('x')], actor_id=actor, profile_id=profile), ())

    def test_principle_has_three_verbatim_quotes_and_brief_lineage(self):
        quotes = c.principle_quotes(actor_id=1, profile_id=1)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[0], 'I want to fall into images and explore them.')
        self.assertIn('shared experience again.', quotes[1])
        self.assertIn("It's interpretational.", quotes[2])
        self.assertTrue(c.BRIEF_LIBRARY_ID.startswith('libfile_'))
        self.assertEqual(len(c.BRIEF_SHA256), 64)

    def test_source_artist_names_drive_qualitative_affinity_without_invented_favourites(self):
        source = card('x', ['Damian Elwes'])
        before = deepcopy(vars(source))
        text = ' '.join(c.reasons(source, actor_id=1, profile_id=1))
        self.assertIn('artist-level like', text)
        self.assertIn('specific work remains an encounter to test', text)
        self.assertEqual(vars(source), before)
        self.assertEqual(c.discovery_mode(source), 'affinity')
        # A depicted studio owner in a title is not the artist.
        source = card('Basquiat-studio', ['Unknown artist'])
        self.assertEqual(c.discovery_mode(source), 'unfamiliar')

    def test_unfamiliar_and_bridge_are_explicit_experiments_not_presumed_likes(self):
        text = ' '.join(c.reasons(card('x', ['Etel Adnan']), actor_id=1, profile_id=1))
        self.assertIn('Curatorial hypothesis', text)
        self.assertIn('not an established like', text)
        self.assertIn('without elaborate narrative', text)
        self.assertEqual(c.discovery_mode(card('x', ['Etel Adnan'])), 'bridge')
        text = ' '.join(c.reasons(card('new', ['Unfamiliar artist']), actor_id=1, profile_id=1))
        self.assertIn('Unfamiliar discovery experiment', text)
        self.assertIn('quiet space, expressive or symbolic depth, and awe or storms', text)
        self.assertIn('busyness is not a score', text)
        self.assertIn('without assuming a shared preference', text)

    def test_rotation_preserves_source_cards_and_does_not_fake_revisits(self):
        cards = [card('a1', ['Kandinsky']), card('a2', ['Klimt']),
                 card('a3', ['Cézanne']), card('b', ['Peter Doig']), card('u')]
        selected = c.select(cards + cards[:1], actor_id=1, profile_id=1, limit=4)
        self.assertEqual([item.key for item in selected], ['a1', 'b', 'u', 'a2'])
        self.assertIs(selected[0], cards[0])
        self.assertEqual(c.select(cards, actor_id=1, profile_id=1, limit=True), ())

    def test_venue_context_is_hypothesis_and_does_not_claim_unseen_image_qualities(self):
        text = ' '.join(c.reasons(card('x', url='https://www.seventhgallery.org/programs'), actor_id=1, profile_id=1))
        self.assertIn('Curatorial hypothesis', text)
        self.assertIn('conversation', text)
        unrelated = card('music')
        unrelated.category = 'music'
        self.assertEqual(c.reasons(unrelated, actor_id=1, profile_id=1), ())
