"""Synthetic typed source evidence, never live adaptations or personal tastes."""
import json
from unittest import TestCase

from shelf import provider_catalogue as p
from shelf.cross_media import Cache, node, resolve_documents
from shelf.cross_media_feedback import FactualEdge, FeatureEdge, catalogue_candidates
from shelf.cross_media_ranking import rank
from shelf.discovery_sources import Candidate
from shelf.tests.graphic_novel_fixtures import rows
from shelf.tests.test_cross_media import claim, synthetic_entity
from shelf.tests.test_cross_media_feedback import command, discovery, ranked, selected
from shelf.tests.test_cross_media_ranking import event

WHEN = '2026-01-01T00:00:00+00:00'
CONTEXT = {'actor_id': 7101, 'profile_id': 7102, 'run_id': 'synthetic-graphic-run', 'expected_revision': 3}
GRAPHIC, PROSE, FILM, TV = ('hardcover:book:94001', 'hardcover:book:94002', 'tmdb:movie:96001', 'tmdb:tv:96002')


def cache():
    # These are invented Wikidata-shaped documents with explicit synthetic
    # provenance. P144 is the source's typed based-on property, not a title join.
    projection = resolve_documents([
        synthetic_entity('Q990094001', {'P144': [claim('P144', {'id': 'Q990094002'}, statement='SYNTHETIC-film-based-on-graphic')]}),
        synthetic_entity('Q990094002', {}),
        synthetic_entity('Q990094003', {'P144': [claim('P144', {'id': 'Q990094002'}, statement='SYNTHETIC-tv-based-on-graphic')]}),
    ], provenance='synthetic').snapshot()
    evidence = projection['edges'][0]['evidence']
    nodes = [node(key, 'SYNTHETIC same title', fetched_at=WHEN, provenance='synthetic',
                  slug='synthetic-graphic' if key.startswith('hardcover:') else None)
             for key in (GRAPHIC, PROSE, FILM, TV)]
    neighbors = ['hardcover:book:94101', 'hardcover:book:94102', 'tmdb:movie:96101']
    nodes += [node(key, 'SYNTHETIC neighbor', provenance='synthetic',
                   slug='synthetic-neighbor' if key.startswith('hardcover:') else None) for key in neighbors]
    edges = [dict(source=FILM, target=GRAPHIC, kind='based-on', rank=0, evidence=evidence),
             dict(source=TV, target=GRAPHIC, kind='based-on', rank=0, evidence=projection['edges'][1]['evidence']),
             dict(source=GRAPHIC, target=neighbors[0], kind='hardcover-similar', rank=0, evidence=[]),
             dict(source=GRAPHIC, target=neighbors[1], kind='hardcover-similar', rank=1, evidence=[]),
             dict(source=FILM, target=neighbors[2], kind='tmdb-recommendation', rank=0, evidence=[])]
    return Cache(dict(schema=projection['schema'], nodes=nodes, edges=edges, records=[]))


def run(history):
    return dict(id=CONTEXT['run_id'], actor_id=7101, profile_id=7102, revision=3,
                archived_at=None, history=history)


class GraphicNovelTasteTests(TestCase):
    def test_graphic_and_prose_same_title_remain_distinct_without_sourced_relation(self):
        source = rows()[:2]
        source[1].update(title=source[0]['title'], book_category_id=1)
        books = p.parse_candidates('hardcover', 'book', source, lane='synthetic', lane_label='SYNTHETIC')
        self.assertEqual([c.book_metadata['category']['label'] for c in books], ['Graphic novel', 'Book'])
        result = rank(run([event(GRAPHIC)]), cache(), **CONTEXT)
        selected_keys = {c['key'] for c in result['selection']}
        self.assertIn(FILM, selected_keys)
        self.assertIn(TV, selected_keys)
        self.assertNotIn(PROSE, selected_keys)
        film = next(c for c in result['selection'] if c['key'] == FILM)
        self.assertEqual(film['path'][0]['kind'], 'based-on')
        evidence = film['path'][0]['evidence'][0]
        self.assertEqual(evidence['property'], 'P144')
        self.assertEqual(evidence['provenance'], 'synthetic')
        self.assertEqual(evidence['statement_id'], 'SYNTHETIC-film-based-on-graphic')

    def test_provider_neighbor_order_two_human_clusters_correction_and_restart(self):
        history = [event(GRAPHIC), event(FILM)]
        source = cache()
        first = rank(run(history), source, **CONTEXT)
        self.assertEqual([c['key'] for c in first['selection'][:3]],
                         ['hardcover:book:94101', 'tmdb:movie:96101', 'hardcover:book:94102'])
        self.assertEqual(len(first['lanes']), 2)
        self.assertEqual(first, rank(json.loads(json.dumps(run(history))), Cache.from_json(source.to_json()), **CONTEXT))
        changed = history + [dict(event(GRAPHIC), operation='correct', choice='not_for_me')]
        result = rank(run(changed), source, **CONTEXT)
        self.assertEqual([lane['root']['key'] for lane in result['lanes']], [FILM])
        self.assertNotIn(GRAPHIC, [c['key'] for c in result['selection']])
        cleared = history + [dict(event(GRAPHIC), operation='undo', choice=None), dict(event(FILM), operation='undo', choice=None)]
        self.assertEqual(rank(run(cleared), source, **CONTEXT)['lanes'], [])
        for response in ('unknown', 'not_for_me'):
            self.assertEqual(rank(run([event(GRAPHIC, response)]), source, **CONTEXT)['selection'], [])

    def test_typed_adaptation_feedback_negative_and_inferred_theme_caps(self):
        cards = list(catalogue_candidates(cache()))
        gallery = Candidate('fixture:event:synthetic-graphic-art', 'SYNTHETIC visual art', 'event',
            'SYNTHETIC programme', 'synthetic', 0, WHEN, category='gallery', starts_at='2026-03-01T00:00:00+00:00')
        cards.append(gallery)
        edges = [FactualEdge(FILM, GRAPHIC, 'adaptation', 'SYNTHETIC adaptation credits',
                            'SYNTHETIC statement A1: film adapts graphic work 94001, not prose 94002', 1.0),
                 FactualEdge(TV, GRAPHIC, 'adaptation', 'SYNTHETIC adaptation credits',
                            'SYNTHETIC statement A2: series adapts graphic work 94001', 1.0),
                 FeatureEdge(GRAPHIC, gallery.key, 'visual-art', 'SYNTHETIC theme note',
                             'Inferred visual-art theme; no factual adaptation or like', 1.0)]
        feedback = discovery([command(GRAPHIC, 'enjoyed')])
        result = ranked(cards, edges, [feedback])
        selection = selected(result)
        self.assertEqual(selection[FILM]['points'], 500)
        self.assertEqual(selection[TV]['points'], 500)
        self.assertNotIn(PROSE, selection)
        self.assertEqual(selection[gallery.key]['points'], 100)
        self.assertEqual(selection[gallery.key]['memberships'][0]['path'][0]['kind'], 'inferred-feature')
        self.assertIn('SYNTHETIC adaptation credits', selection[FILM]['memberships'][0]['path'][0]['source'])
        negative = discovery([command(GRAPHIC, 'enjoyed'), command(FILM, 'not_for_me')])
        self.assertNotIn(FILM, selected(ranked(cards, edges, [negative])))
        blocked = discovery([command(FILM, 'enjoyed'), command(GRAPHIC, 'not_for_me')])
        self.assertEqual(ranked(cards, edges, [blocked])['selection'], [])
        for history in ([command(GRAPHIC, 'unknown')], [command(GRAPHIC, operation='save')],
                        [command(GRAPHIC, 'enjoyed'), command(operation='restart')]):
            self.assertEqual(ranked(cards, edges, [discovery(history)])['selection'], [])
        from shelf.cross_media_feedback import FeedbackSnapshot, Reaction
        for response in ('opened', 'saved', 'unknown'):
            snapshot = FeedbackSnapshot(7101, 7102, 'sample', 'synthetic-click', 1,
                (Reaction(GRAPHIC, response, 'synthetic-event', 1, 'direct', WHEN),))
            self.assertEqual(ranked(cards, edges, [snapshot])['lanes'], [])
