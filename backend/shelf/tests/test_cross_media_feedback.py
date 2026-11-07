"""Synthetic identities and human histories only; no live-coverage assertions."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from unittest import TestCase
from unittest.mock import patch

from shelf.cross_media import Cache, InvalidData, node
from shelf.cross_media_feedback import (
    FactualEdge, FeatureEdge, FeedbackSnapshot, Reaction, catalogue_candidates,
    from_discovery, from_intake, rank,
)
from shelf.cross_media_ranking import ContextMismatch
from shelf.discovery_sources import Candidate
from shelf.tests.test_cross_media_ranking import event as intake_event

WHEN = '2026-01-01T00:00:00+00:00'
NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)
FUTURE = '2026-03-01T00:00:00+00:00'
CONTEXT = {'actor_id': 7101, 'profile_id': 7102, 'mode': 'sample'}


def card(kind, name, **kwargs):
    return Candidate(f'fixture:{kind}:{name}', f'SYNTHETIC {name}', kind,
                     'Synthetic source', 'fixture', 0, WHEN, **kwargs)


def fact(source, target, relation, **kwargs):
    return FactualEdge(source.key, target.key, relation, 'Synthetic credits',
                       'Synthetic statement; not a real lineup', kwargs.get('confidence', 1.0))


def command(key='', choice='', operation='respond'):
    return dict(key=key, choice=choice, operation=operation, at=WHEN,
                origin='authenticated-human-post', mode='sample')


def discovery(history, *, revision=20, state_id='synthetic-discovery', attended=()):
    snapshot = dict(CONTEXT, id=state_id, revision=revision, history=history)
    return from_discovery(snapshot, **CONTEXT, state_id=state_id, expected_revision=revision,
                          attended_keys=attended)


def ranked(cards, edges, snapshots, **kwargs):
    return rank(cards, edges, snapshots, **CONTEXT, now=NOW,
                expected_revisions={s.source: s.revision for s in snapshots}, **kwargs)


def selected(result):
    return {row['card'].key: row for row in result['selection']}


class FeedbackBridgeTests(TestCase):
    def setUp(self):
        self.past = card('event', 'attended', starts_at=WHEN)
        self.artist = card('artist', 'performer')
        self.music = card('recording', 'recording')
        self.future = card('event', 'future', starts_at=FUTURE)
        self.cards = [self.past, self.artist, self.music, self.future]
        self.edges = [fact(self.past, self.artist, 'lineup'),
                      fact(self.future, self.artist, 'lineup'),
                      fact(self.music, self.artist, 'recording-artist')]

    def test_attended_enjoyed_event_transfers_to_artist_and_music(self):
        feedback = discovery([command(self.past.key, 'enjoyed')], attended=(self.past.key,))
        rows = selected(ranked(self.cards, self.edges, [feedback]))
        self.assertEqual(rows[self.artist.key]['points'], 500)
        self.assertEqual(rows[self.music.key]['points'], 250)
        self.assertEqual(rows[self.future.key]['points'], 250)
        path = rows[self.music.key]['memberships'][0]['path']
        self.assertEqual([step['relation'] for step in path], ['lineup', 'recording-artist'])
        self.assertEqual([step['traversal'] for step in path], ['forward', 'reverse'])

    def test_music_enjoyment_transfers_to_future_event_but_not_past(self):
        feedback = discovery([command(self.music.key, 'enjoyed')])
        rows = selected(ranked(self.cards, self.edges, [feedback]))
        self.assertIn(self.future.key, rows)
        self.assertNotIn(self.past.key, rows)
        self.assertEqual(rows[self.future.key]['points'], 250)
        self.assertEqual(rows[self.future.key]['memberships'][0]['fact_hops'], 2)

    def test_artist_enjoyment_transfers_in_reverse_lineup_direction(self):
        rows = selected(ranked(self.cards, self.edges,
                               [discovery([command(self.artist.key, 'enjoyed')])]))
        self.assertEqual(rows[self.future.key]['points'], 500)
        self.assertEqual(rows[self.future.key]['memberships'][0]['path'][0]['traversal'], 'reverse')

    def test_enjoyment_without_explicit_attendance_does_not_transfer_event(self):
        for choice in ('enjoyed', 'interested', 'unknown', 'not_for_me'):
            result = ranked(self.cards, self.edges, [discovery([command(self.past.key, choice)])])
            self.assertEqual(result['lanes'], [])
        self.assertEqual(ranked(self.cards, self.edges, [])['status'], 'needs-enjoyment')

    def test_unknown_save_open_skip_and_interest_are_not_enjoyment(self):
        for response in ('unknown', 'saved', 'opened', 'skipped', 'interested', 'completed', 'aware'):
            snapshot = FeedbackSnapshot(**CONTEXT, source='synthetic-actions', revision=8,
                reactions=(Reaction(self.artist.key, response, 'synthetic-event', 3, 'direct', WHEN),))
            result = ranked(self.cards, self.edges, [snapshot])
            self.assertEqual(result['selection'], [])
            self.assertEqual(result['feedback'][0].reactions[0].response, response)

    def test_discovery_save_not_like_and_metadata_never_supplies_feedback(self):
        feedback = discovery([command(self.artist.key, operation='save')])
        cards = [replace(c, roots=(self.artist.key,), lineage=((self.artist.key, 1),)) for c in self.cards]
        self.assertEqual(feedback.reactions[0].response, 'saved')
        self.assertEqual(ranked(cards, self.edges, [feedback])['lanes'], [])
        self.assertEqual(ranked(cards, self.edges, [])['selection'], [])

    def test_negative_target_and_negative_intermediate_override_positive_paths(self):
        feedback = discovery([command(self.music.key, 'enjoyed'),
                              command(self.future.key, 'not_for_me')])
        rows = selected(ranked(self.cards, self.edges, [feedback]))
        self.assertNotIn(self.future.key, rows)
        self.assertIn(self.artist.key, rows)
        feedback = discovery([command(self.music.key, 'enjoyed'), command(self.artist.key, 'not_for_me')])
        self.assertEqual(ranked(self.cards, self.edges, [feedback])['selection'], [])

    def test_negative_in_another_current_system_wins_without_inventing_dislikes(self):
        positive = discovery([command(self.artist.key, 'enjoyed')])
        negative = discovery([command(self.artist.key, 'not_for_me')], state_id='another-system')
        result = ranked(self.cards, self.edges, [positive, negative])
        self.assertEqual(result['lanes'], [])
        self.assertEqual(len(result['feedback']), 2)

    def test_original_root_multiple_paths_and_duplicate_snapshots_do_not_sum(self):
        other = card('artist', 'other-performer')
        cards = self.cards + [other]
        edges = self.edges + [fact(self.past, other, 'lineup'), fact(self.music, other, 'recording-artist')]
        feedback = discovery([command(self.past.key, 'enjoyed')], attended=(self.past.key,))
        baseline = ranked(cards, edges, [feedback])
        duplicated = ranked(cards, edges * 8, [feedback, feedback])
        self.assertEqual(baseline, duplicated)
        self.assertEqual(selected(baseline)[self.music.key]['points'], 250)
        self.assertEqual(len(selected(baseline)[self.music.key]['memberships']), 1)

    def test_same_item_in_two_systems_is_one_root_with_original_references(self):
        first = discovery([command(self.artist.key, 'enjoyed')])
        second = discovery([command(self.artist.key, 'enjoyed')], state_id='second', revision=99)
        result = ranked(self.cards, self.edges, [first, second])
        self.assertEqual(len(result['lanes']), 1)
        self.assertEqual(len(result['lanes'][0]['feedback']), 2)
        self.assertEqual(selected(result)[self.future.key]['points'], 500)
        self.assertEqual([s.revision for s in result['feedback']], [99, 20])

    def test_maximum_two_fact_hops_cycles_and_no_inferred_self_reinforcement(self):
        other = card('artist', 'shared-bill')
        third = card('recording', 'too-far')
        cards = self.cards + [other, third]
        edges = self.edges + [fact(self.future, other, 'lineup'), fact(third, other, 'recording-artist')]
        feedback = discovery([command(self.music.key, 'enjoyed')])
        result = ranked(cards, edges, [feedback])
        self.assertNotIn(other.key, selected(result))
        self.assertNotIn(third.key, selected(result))
        self.assertEqual([lane['root'] for lane in result['lanes']], [self.music.key])
        self.assertEqual(result, ranked(cards, list(reversed(edges)), [feedback]))

    def test_unverified_event_dates_are_not_future_inventory(self):
        feedback = discovery([command(self.artist.key, 'enjoyed')])
        for date in (None, 'bad-date', '2026-03-01', NOW.isoformat(), WHEN):
            cards = [replace(c, starts_at=date) if c == self.future else c for c in self.cards]
            self.assertNotIn(self.future.key, selected(ranked(cards, self.edges, [feedback])))

    def test_explanations_keep_fact_type_source_confidence_and_human_revision(self):
        edges = [replace(edge, confidence=0.8) for edge in self.edges]
        feedback = discovery([command(self.artist.key, 'enjoyed')], revision=42)
        result = ranked(self.cards, edges, [feedback])
        row = selected(result)[self.future.key]
        self.assertEqual(row['points'], 400)
        step = row['memberships'][0]['path'][0]
        self.assertEqual(step['kind'], 'factual')
        self.assertEqual(step['source'], 'Synthetic credits')
        self.assertEqual(step['confidence'], 0.8)
        self.assertIn('not a real lineup', step['evidence'])
        self.assertEqual(result['feedback'][0].revision, 42)
        self.assertIsNone(result['lanes'][0]['feedback'][0][1].revision)

    def test_pure_reconstruction_input_order_and_mutable_output_isolation(self):
        feedback = discovery([command(self.music.key, 'enjoyed')])
        original = deepcopy((self.cards, self.edges, feedback))
        with patch('builtins.open', side_effect=AssertionError('no I/O')), \
             patch('socket.socket', side_effect=AssertionError('no network')):
            result = ranked(self.cards, self.edges, [feedback])
            rebuilt = ranked(list(reversed(self.cards)), list(reversed(self.edges)), [feedback])
        self.assertEqual(result, rebuilt)
        self.assertEqual(original, (self.cards, self.edges, feedback))
        result['selection'][0]['memberships'][0]['path'][0]['source'] = 'mutated output'
        self.assertEqual(ranked(self.cards, self.edges, [feedback]), rebuilt)

    def test_correction_restart_and_undo_reconstruct_ranking_not_accumulated_scores(self):
        history = [command(self.artist.key, 'enjoyed')]
        baseline = ranked(self.cards, self.edges, [discovery(history)])['selection']
        history.append(command(self.artist.key, 'not_for_me'))
        self.assertEqual(ranked(self.cards, self.edges, [discovery(history)])['selection'], [])
        history.append(command(operation='undo'))
        self.assertEqual(ranked(self.cards, self.edges, [discovery(history)])['selection'], baseline)
        history.append(command(operation='restart'))
        self.assertEqual(ranked(self.cards, self.edges, [discovery(history)])['selection'], [])
        history.append(command(operation='undo'))
        reconstructed = discovery(json.loads(json.dumps(history)))
        self.assertEqual(ranked(self.cards, self.edges, [reconstructed])['selection'], baseline)

    def test_only_new_human_enjoyment_of_a_suggestion_can_create_another_root(self):
        first = discovery([command(self.music.key, 'enjoyed')])
        self.assertEqual(len(ranked(self.cards, self.edges, [first])['lanes']), 1)
        second = discovery([command(self.music.key, 'enjoyed'), command(self.artist.key, 'enjoyed')])
        result = ranked(self.cards, self.edges, [second])
        self.assertEqual(len(result['lanes']), 2)
        self.assertEqual(selected(result)[self.future.key]['points'], 750)

    def test_path_budget_is_reported_for_many_shared_lineup_paths(self):
        artists = [card('artist', f'performer-{i}') for i in range(30)]
        music = [card('recording', f'music-{i}') for i in range(60)]
        edges = [fact(self.past, artist, 'lineup') for artist in artists]
        edges += [fact(recording, artist, 'recording-artist') for artist in artists for recording in music]
        feedback = discovery([command(self.past.key, 'enjoyed')], attended=(self.past.key,))
        result = ranked([self.past] + artists + music, edges, [feedback])
        self.assertTrue(result['lanes'][0]['path_budget_reached'])
        self.assertLessEqual(len(result['lanes'][0]['members']), 100)
        self.assertTrue(all(row['points'] <= 500 for row in result['selection']))
        self.assertEqual(result, ranked([self.past] + artists + music, list(reversed(edges)), [feedback]))


class ThemeAndFairnessTests(TestCase):
    def test_adaptation_and_shared_work_facts_are_distinct_from_inferred_theme(self):
        book, film, series = [card(kind, kind) for kind in ('book', 'movie', 'tv')]
        arts = card('event', 'gallery', starts_at=FUTURE, category='gallery')
        edges = [fact(film, book, 'adaptation'), fact(film, series, 'shared-work'),
                 FeatureEdge(series.key, arts.key, 'visual-art', 'Synthetic programme', 'Theme note', 1.0)]
        result = ranked([book, film, series, arts], edges, [discovery([command(book.key, 'enjoyed')])])
        rows = selected(result)
        self.assertEqual(rows[film.key]['points'], 500)
        self.assertEqual(rows[series.key]['points'], 250)
        self.assertEqual(rows[arts.key]['points'], 25)
        membership = rows[arts.key]['memberships'][0]
        self.assertEqual((membership['fact_hops'], membership['feature_hops']), (2, 1))
        self.assertEqual([s['kind'] for s in membership['path']], ['factual', 'factual', 'inferred-feature'])

    def test_theme_only_weak_global_cap_and_no_dietary_inference(self):
        roots = [card(kind, f'nature-{i}') for i, kind in enumerate(('book', 'movie', 'tv', 'book', 'tv'))]
        arts = card('event', 'nature-art', starts_at=FUTURE, category='art_fair')
        edges = [FeatureEdge(root.key, arts.key, 'nature', 'Synthetic programme', 'Supplied theme', 1.0)
                 for root in roots]
        feedback = discovery([command(root.key, 'enjoyed') for root in roots])
        row = selected(ranked(roots + [arts], edges, [feedback]))[arts.key]
        self.assertEqual(row['points'], 150)
        self.assertEqual(len(row['memberships']), 5)
        self.assertTrue(all(m['points'] == 100 and m['inferred'] for m in row['memberships']))
        self.assertFalse(any('diet' in key or 'constraint' in key for key in row))
        for feature in ('vegan', 'vegetarian', 'diet', 'allergy', 'food-preference'):
            with self.subTest(feature=feature), self.assertRaises(InvalidData):
                ranked(roots + [arts], [replace(edges[0], feature=feature)], [feedback])

    def test_theme_requires_source_evidence_and_arts_target_no_reverse_transfer(self):
        film = card('movie', 'film')
        arts = card('event', 'art', starts_at=FUTURE, category='gallery')
        edge = FeatureEdge(film.key, arts.key, 'history', 'Synthetic source', 'Synthetic theme', 0.8)
        for changed in (replace(edge, evidence=''), replace(edge, source=''),
                        replace(edge, confidence=float('nan')), replace(edge, confidence=1.1)):
            with self.assertRaises(InvalidData):
                ranked([film, arts], [changed], [])
        with self.assertRaises(InvalidData):
            ranked([film, replace(arts, category='music')], [edge], [])
        feedback = discovery([command(arts.key, 'enjoyed')], attended=(arts.key,))
        self.assertEqual(ranked([film, arts], [edge], [feedback])['selection'], [])

    def test_five_uneven_lanes_keep_empty_and_missing_roots_without_collapse(self):
        roots = [card('artist', f'root-{i}') for i in range(6)]
        cards = roots[:4] + [roots[5]]  # root-4 has missing inventory/metadata
        edges = []
        for i, count in enumerate((8, 1, 0, 3)):
            for j in range(count):
                member = card('recording', f'lane-{i}-{j}')
                cards.append(member)
                edges.append(fact(member, roots[i], 'recording-artist'))
        feedback = discovery([command(root.key, 'enjoyed') for root in reversed(roots)])
        result = ranked(cards, edges, [feedback], size=7)
        self.assertEqual([lane['root'] for lane in result['lanes']], [root.key for root in roots[:5]])
        self.assertEqual([len(lane['members']) for lane in result['lanes']], [8, 1, 0, 3, 0])
        self.assertEqual([row['card'].key for row in result['selection']], [
            'fixture:recording:lane-0-0', 'fixture:recording:lane-1-0', 'fixture:recording:lane-3-0',
            'fixture:recording:lane-0-1', 'fixture:recording:lane-3-1',
            'fixture:recording:lane-0-2', 'fixture:recording:lane-3-2'])

    def test_shared_member_does_not_consume_next_lane_turn_and_total_is_capped(self):
        roots = [card('artist', f'root-{i}') for i in range(5)]
        shared = card('recording', 'a-shared')
        unique = card('recording', 'b-unique')
        edges = [fact(shared, root, 'recording-artist') for root in roots]
        edges.append(fact(unique, roots[1], 'recording-artist'))
        feedback = discovery([command(root.key, 'enjoyed') for root in roots])
        result = ranked(roots + [shared, unique], edges, [feedback], size=2)
        self.assertEqual([row['card'].key for row in result['selection']], [shared.key, unique.key])
        self.assertEqual(result['selection'][0]['points'], 1000)
        self.assertEqual(len(result['selection'][0]['memberships']), 5)

    def test_catalogue_projection_reuses_validated_namespaces(self):
        data = {'schema': 'cross-media-cache-v1', 'nodes': [node('tmdb:movie:99001', 'Synthetic movie')],
                'edges': [], 'records': []}
        cards = catalogue_candidates(Cache(data))
        self.assertIsInstance(cards[0], Candidate)
        self.assertEqual((cards[0].key, cards[0].kind), ('tmdb:movie:99001', 'movie'))


class AdapterReplayTests(TestCase):
    def test_discovery_correction_undo_restart_and_restart_undo_keep_effective_reference(self):
        artist = card('artist', 'root')
        history = [command(artist.key, 'enjoyed'), command(artist.key, 'not_for_me')]
        corrected = discovery(history)
        self.assertEqual(corrected.reactions[0].response, 'not_for_me')
        history.append(command(operation='undo'))
        restored = discovery(history, revision=88)
        self.assertEqual(restored.reactions[0].response, 'enjoyed')
        self.assertTrue(restored.reactions[0].event_id.endswith(':0'))
        self.assertEqual(restored.revision, 88)
        history.append(command(operation='restart'))
        self.assertEqual(discovery(history).reactions, ())
        history.append(command(operation='undo'))
        self.assertEqual(discovery(history, revision=88), discovery(json.loads(json.dumps(history)), revision=88))
        self.assertEqual(discovery(history, revision=88), restored)

    def test_durable_undo_clears_instead_of_restoring_previous_choice(self):
        run = dict(actor_id=7101, profile_id=7102, id='synthetic-run', revision=27, archived_at=None,
                   history=[intake_event('tmdb:movie:99001')])
        kwargs = dict(CONTEXT, run_id='synthetic-run', expected_revision=27)
        first = from_intake(run, **kwargs)
        self.assertEqual(first.revision, 27)
        self.assertIsNone(first.reactions[0].revision)
        run['history'].append(intake_event('tmdb:movie:99001', 'not_for_me', 'correct'))
        self.assertEqual(from_intake(run, **kwargs).reactions[0].response, 'not_for_me')
        run['history'].append(intake_event('tmdb:movie:99001', None, 'undo'))
        self.assertEqual(from_intake(run, **kwargs).reactions, ())
        self.assertEqual(from_intake(json.loads(json.dumps(run)), **kwargs), from_intake(run, **kwargs))
        with self.assertRaises(ContextMismatch):
            from_intake(run, **dict(kwargs, expected_revision=26))

    def test_durable_import_retains_unknown_time_and_cannot_be_relabelled(self):
        from shelf.cross_media_ranking import IMPORT_PROVENANCE
        event = intake_event('tmdb:movie:99001', operation='import')
        event.update(provenance=IMPORT_PROVENANCE, responded_at=None)
        run = dict(actor_id=7101, profile_id=7102, id='synthetic-run', revision=0, history=[event])
        snapshot = from_intake(run, **CONTEXT, run_id='synthetic-run', expected_revision=0)
        self.assertIsNone(snapshot.reactions[0].responded_at)
        self.assertEqual(snapshot.reactions[0].provenance, IMPORT_PROVENANCE)

    def test_stale_mixed_context_conflicting_snapshot_and_missing_stream_rejected(self):
        feedback = discovery([])
        for changed in (replace(feedback, actor_id=9999), replace(feedback, profile_id=9999),
                        replace(feedback, mode='household'), replace(feedback, revision=19)):
            with self.assertRaises(ContextMismatch):
                rank([], [], [changed], **CONTEXT, now=NOW, expected_revisions={feedback.source:20})
        with self.assertRaises(ContextMismatch):
            ranked([], [], [feedback, replace(feedback, reactions=(Reaction('x', 'saved', 'id', None, 'direct', WHEN),))])
        with self.assertRaises(ContextMismatch):
            rank([], [], [], **CONTEXT, now=NOW, expected_revisions={feedback.source:20})

    def test_discovery_rejects_stale_or_automated_history_and_mode_mixing(self):
        snapshot = dict(CONTEXT, id='state', revision=5, history=[])
        kwargs = dict(CONTEXT, state_id='state', expected_revision=5)
        for changes in ({'revision':4}, {'actor_id':True}, {'profile_id':9999}, {'mode':'household'}, {'id':'elsewhere'}):
            with self.assertRaises(ContextMismatch):
                from_discovery(dict(snapshot, **changes), **kwargs)
        for changes in ({'origin':'live-automated'}, {'operation':'open'}, {'mode':'household'}, {'choice':'saved'}):
            bad = dict(command('fixture:artist:root', 'enjoyed'), **changes)
            with self.assertRaises(InvalidData):
                from_discovery(dict(snapshot, history=[bad]), **kwargs)

    def test_budgets_and_materialized_inputs(self):
        for size in (-1, 51, True):
            with self.assertRaises(InvalidData):
                ranked([], [], [], size=size)
        with self.assertRaises(InvalidData):
            ranked(iter(()), [], [])
        with self.assertRaises(InvalidData):
            ranked([card('artist', str(i)) for i in range(513)], [], [])
        self.assertEqual(ranked([], [], [], size=0)['selection'], [])


class NumericDiscoveryCompatibilityTests(TestCase):
    """Shared household history keeps numeric bases and exact source positions."""

    def item(self, key='official:event:synthetic', medium='event'):
        return dict(key=key, title='Synthetic rated item', medium=medium,
                    source='Synthetic source', url='https://example.org/item',
                    provenance='live-automated', lane='synthetic', roots=[], fetched_at=WHEN)

    def action(self, operation, key='', **fields):
        return dict(operation=operation, key=key, choice='', at=WHEN,
                    origin='authenticated-human-post', mode='household', **fields)

    def rating(self, value=4, basis='interest', item=None, experienced=False):
        item = item or self.item()
        return self.action('rate', item['key'], card=item, value=value,
                           basis=basis, experienced=experienced)

    def adapt(self, history, attended=()):
        context = dict(CONTEXT, mode='household')
        snapshot = dict(context, id='numeric-state', revision=91, history=history)
        return from_discovery(snapshot, **context, state_id='numeric-state',
                              expected_revision=91, attended_keys=attended)

    def test_all_numeric_values_preserve_basis_and_actual_value(self):
        mappings = {'interest': ('uninterested', 'uninterested', 'neutral', 'interested', 'interested'),
                    'enjoyment': ('not_for_me', 'not_for_me', 'neutral', 'enjoyed', 'enjoyed')}
        for basis, responses in mappings.items():
            for value, response in enumerate(responses, 1):
                with self.subTest(basis=basis, value=value):
                    reaction, = self.adapt([self.rating(value, basis, experienced=basis == 'enjoyment')]).reactions
                    self.assertEqual((reaction.response, reaction.original_value, reaction.basis),
                                     (response, value, basis))
                    self.assertEqual(reaction.event_id, 'numeric-state:history:0')
                    self.assertIsNone(reaction.revision)
                    self.assertEqual(reaction.experienced, basis == 'enjoyment')
                    self.assertEqual(reaction.attended, basis == 'enjoyment')

    def test_independent_bases_edits_clear_and_undo_retain_real_history_indices(self):
        history = [self.rating(5), self.rating(4, 'enjoyment', experienced=True),
                   self.action('funnel_next', shown=['other']), self.rating(2)]
        reactions = {r.basis: r for r in self.adapt(history).reactions}
        self.assertEqual(reactions['interest'].event_id, 'numeric-state:history:3')
        self.assertEqual(reactions['enjoyment'].event_id, 'numeric-state:history:1')
        self.assertEqual(reactions['interest'].response, 'uninterested')
        history.append(self.rating(None))
        reaction, = self.adapt(history).reactions
        self.assertEqual(reaction.event_id, 'numeric-state:history:1')
        history.append(self.action('undo'))
        self.assertEqual({r.basis: r for r in self.adapt(history).reactions}, reactions)
        history.append(self.action('undo'))
        restored = {r.basis: r for r in self.adapt(history).reactions}
        self.assertEqual(restored['interest'].event_id, 'numeric-state:history:0')
        self.assertEqual(restored['interest'].original_value, 5)

    def test_restart_undo_and_clearing_last_basis_remove_only_effective_reactions(self):
        history = [self.rating(5, 'enjoyment', experienced=True)]
        original = self.adapt(history)
        self.assertEqual(self.adapt(history + [self.action('restart')]).reactions, ())
        self.assertEqual(self.adapt(history + [self.action('restart'), self.action('undo')]), original)
        self.assertEqual(self.adapt(history + [self.rating(None, 'enjoyment', experienced=True)]).reactions, ())

    def test_skip_next_and_favourite_are_not_numeric_ratings_or_attendance(self):
        item = self.item()
        history = [self.action('funnel_skip', item['key'], card=item),
                   self.action('funnel_next', shown=[item['key']]),
                   self.action('save', item['key'], card=item)]
        reaction, = self.adapt(history).reactions
        self.assertEqual(reaction.response, 'saved')
        self.assertEqual(reaction.event_id, 'numeric-state:history:2')
        self.assertIsNone(reaction.basis)
        self.assertFalse(reaction.experienced)
        self.assertFalse(reaction.attended)
        self.assertEqual(self.adapt(history[:2]).reactions, ())

    def test_interest_never_claims_experience_or_attendance(self):
        item = self.item()
        reaction, = self.adapt([self.rating(5, experienced=True)], attended=[item['key']]).reactions
        self.assertEqual(reaction.response, 'interested')
        self.assertFalse(reaction.attended)
        self.assertFalse(reaction.experienced)
        film = self.item('tmdb:movie:123', 'movie')
        reaction, = self.adapt([self.rating(5, 'enjoyment', film, True)]).reactions
        self.assertTrue(reaction.experienced)
        self.assertFalse(reaction.attended)

    def test_legacy_qualitative_references_survive_interleaved_funnel_events(self):
        legacy = dict(command('official:artist:legacy', 'enjoyed'), mode='household')
        item = self.item()
        history = [legacy, self.action('funnel_skip', item['key'], card=item),
                   self.action('funnel_next', shown=[]), self.rating(3)]
        reactions = self.adapt(history).reactions
        self.assertEqual(reactions[0], self.adapt([legacy]).reactions[0])
        self.assertEqual(reactions[0].event_id, 'numeric-state:history:0')
        self.assertEqual(reactions[0].original_value, 'enjoyed')
        self.assertIsNone(reactions[0].basis)
        self.assertEqual(reactions[1].event_id, 'numeric-state:history:3')
        self.assertEqual(reactions[1].response, 'neutral')

    def test_numeric_enjoyment_attendance_can_seed_verified_event_path(self):
        item = self.item()
        event_card = Candidate(item['key'], item['title'], 'event', item['source'],
                               item['provenance'], 0, WHEN, starts_at=WHEN)
        artist = Candidate('official:artist:performer', 'Synthetic artist', 'artist',
                           'Synthetic source', 'live-automated', 0, WHEN)
        feedback = self.adapt([self.rating(5, 'enjoyment', experienced=True)])
        result = rank([event_card, artist], [fact(event_card, artist, 'lineup')], [feedback],
                      **dict(CONTEXT, mode='household'), now=NOW,
                      expected_revisions={feedback.source: feedback.revision})
        self.assertEqual(selected(result)[artist.key]['points'], 500)
        interest = self.adapt([self.rating(5)])
        result = rank([event_card, artist], [fact(event_card, artist, 'lineup')], [interest],
                      **dict(CONTEXT, mode='household'), now=NOW,
                      expected_revisions={interest.source: interest.revision})
        self.assertEqual(result['lanes'], [])

    def test_invalid_numeric_actions_and_sample_numeric_history_are_refused(self):
        valid = self.rating()
        for changes in ({'value': True}, {'value': 0}, {'value': 6}, {'basis': 'preference'},
                        {'basis': 'enjoyment', 'experienced': False}, {'card': {}},
                        {'origin': 'machine'}, {'mode': 'sample'}):
            with self.subTest(changes=changes), self.assertRaises(InvalidData):
                self.adapt([dict(valid, **changes)])
        with self.assertRaises(InvalidData):
            self.adapt([self.action('funnel_next', shown=['duplicate', 'duplicate'])])
        with self.assertRaises(InvalidData):
            discovery([dict(valid, mode='sample')])
