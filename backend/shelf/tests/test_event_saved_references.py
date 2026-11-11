"""Synthetic supplied policies prove mechanics, never publisher permission."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase

from shelf import event_saved_references as refs
from shelf.event_source_registry import SOURCES
from shelf.pbs_events import parse_listing, event_url
from shelf.tests.test_pbs_events import listing
from shelf.tests.test_discovery_transport import NOW
from shelf.venue303_events import parse_listing as parse_303
from shelf.tests.test_venue303_events import listing as listing_303, card as card_303
from shelf.event_sources import SOURCES as OLD_SOURCES, official_url


def policies():
    return {**SOURCES, 'pbs': replace(SOURCES['pbs'], reference_policy_ref='synthetic-private-bookmark/v1',
        restriction_namespace='event-source:pbs', reference_url_validator=event_url),
        'venue303': replace(SOURCES['venue303'], reference_policy_ref='synthetic-private-bookmark/v1',
        restriction_namespace='event-source:venue303',
        reference_url_validator=lambda value: official_url(value, OLD_SOURCES['venue303']))}


def pbs_card():
    return parse_listing(listing(), retrieved_at=NOW.isoformat())[0]


def action(card, reference, **changes):
    return dict(operation='save', key=card.key, choice='', mode='household',
        origin='authenticated-human-post', source={'provenance':card.provenance},
        saved_reference=reference, **changes)


class SavedReferenceTests(TestCase):
    def make(self, card=None, **kwargs):
        return refs.make_reference(card or pbs_card(), mode='household',
                                   descriptors=policies(), **kwargs)

    def test_positive_policy_required_and_new_field_contains_only_six_fields(self):
        card=pbs_card(); reference=self.make(card)
        self.assertEqual(set(reference), refs.FIELDS)
        self.assertEqual(reference['official_url'], card.url)
        self.assertIsNone(reference['occurrence_ref'])
        self.assertIsNone(refs.make_reference(card, mode='household', descriptors=SOURCES))
        self.assertIsNone(self.make(card, denied_namespaces={'event-source:pbs'}))
        for forbidden in ('title','starts_at','source_notice','snapshot_sha256','lineup','image'):
            self.assertNotIn(forbidden, reference)

    def test_stale_identity_cannot_refill_and_sample_evidence_cannot_cross_modes(self):
        card=pbs_card()
        stale=refs.ReferenceCandidate(card.key,'Saved event reference — source details expired',
            'event',card.source,card.provenance,card.rank,card.retrieved_at,
            saved_reference=self.make(card))
        self.assertIsNone(self.make(stale))
        self.assertIsNone(stale.url)
        self.assertIn('Stale', stale.freshness(NOW))
        self.assertIsNone(refs.make_reference(card,mode='sample',descriptors=policies()))
        self.assertIsNone(self.make(replace(card,provenance='fixture')))
        self.assertIsNone(self.make(replace(card,evidence=(replace(card.evidence[0],provenance='fixture'),))))

    def test_occurrences_sharing_url_remain_distinct_and_wrong_binding_is_rejected(self):
        cards=parse_303(listing_303(card_303(start='2026-11-12',clock='7:30 PM'),
                                   card_303(start='2026-11-12',clock='9:30 PM')),
                        retrieved_at=NOW.isoformat(),provenance='live-automated')
        self.assertEqual(len(cards),2)
        first,second=map(self.make,cards)
        self.assertEqual(first['official_url'],second['official_url'])
        self.assertNotEqual(first['occurrence_ref'],second['occurrence_ref'])
        self.assertIsNone(refs.validate_reference(first,key=cards[1].key,mode='household',
            provenance='live-automated',descriptors=policies()))
        self.assertIsNone(self.make(replace(cards[0], evidence=(replace(cards[0].evidence[0],
            source_id='pbs'),))))

    def test_malformed_link_policy_shape_and_oversize_fail_closed(self):
        card=pbs_card(); good=self.make(card)
        changes=[{'official_url': 'https://evil.invalid/news/a'},
                 {'official_url':card.url+'?tracking=1'}, {'official_url':'https://www.pbsfm.org.au/login'},
                 {'official_url':'https://user@www.pbsfm.org.au/news/a'},
                 {'official_url':card.url+'#fragment'}, {'official_url':'x'*2049},
                 {'source_id':[]}, {'occurrence_basis':'explicit_date'}, {'title':'CANARY'},
                 {'retention_policy_ref':'forged'}, {'occurrence_ref':'invalid'}]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(refs.validate_reference(good|change,key=card.key,mode='household',
                    provenance=card.provenance,descriptors=policies()))

    def test_projection_never_backfills_and_returns_detached_values(self):
        card=pbs_card(); reference=self.make(card); history=[action(card,reference)]
        result=refs.project_references(history,mode='household',descriptors=policies())
        result[card.key]['official_url']='changed'
        self.assertEqual(history[0]['saved_reference'],reference)
        legacy=deepcopy(history[0]);legacy.pop('saved_reference');legacy['card']={'url':card.url}
        self.assertEqual(refs.project_references([legacy],mode='household',descriptors=policies()),{})
        self.assertEqual(refs.project_references(history,mode='household',descriptors=policies(),
            denied_namespaces={'event-source:pbs'}),{})
        self.assertEqual(refs.project_references(history,mode='sample',descriptors=policies()),{})

    def test_conflicting_or_malformed_history_hides_link_without_changing_actions(self):
        card=pbs_card(); good=action(card,self.make(card))
        bad=deepcopy(good);bad['saved_reference']['source_id']='venue303'
        history=[good,bad];before=deepcopy(history)
        self.assertEqual(refs.project_references(history,mode='household',descriptors=policies()),{})
        self.assertEqual(history,before)

    def test_removal_covers_undone_entries_preserving_choices_and_receipts(self):
        card=pbs_card(); history=[action(card,self.make(card)),
            {'operation':'unsave','key':card.key}, {'operation':'undo','key':''},
            {'operation':'restart','key':''}]
        before=deepcopy(history)
        redacted=refs.redact_history(history,source_namespaces={'event-source:pbs'},descriptors=policies())
        self.assertNotIn('saved_reference',redacted[0])
        self.assertEqual(history,before)
        self.assertEqual(redacted[1:],history[1:])
        self.assertEqual(refs.referenced_namespaces(history,descriptors=policies()),{'event-source:pbs'})
        self.assertEqual(refs.referenced_namespaces(redacted,descriptors=policies()),set())
        self.assertEqual(refs.redact_history(history,source_namespaces={'pbs'},descriptors=policies()),history)
