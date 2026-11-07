"""Entirely synthetic snapshots. No provider calls, accounts or live listings."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest import TestCase
from unittest.mock import patch

from shelf import event_sources as e, discovery_transport as transport
from shelf.discovery_sources import Candidate, MAX_BYTES, SourceError

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
FETCHED = NOW.isoformat()
LATER = (NOW + timedelta(hours=1)).isoformat()


def encoded(document):
    return json.dumps(document).encode()


def official(**changes):
    return dict(kind='event', id='synthetic-one', name='SYNTHETIC event',
                url='https://events.unimelb.edu.au/synthetic-one',
                startDate='2026-11-01T19:00:00+11:00', timezone='Australia/Melbourne',
                status='scheduled', eligibility='public', searchable=True) | changes


def snapshot(*rows):
    return encoded({'format': 'event-metadata-v1', 'events': list(rows)})


def parse(*rows, retrieved_at=FETCHED, provenance='fixture', source_id='unimelb', sources=e.SOURCES):
    return e.parse_official_snapshot(snapshot(*rows), source_id=source_id,
        retrieved_at=retrieved_at, provenance=provenance, sources=sources)


def jsonld(**changes):
    row = official()
    for key in ('kind', 'id', 'status'):
        row.pop(key)
    return row | {'@context': 'https://schema.org', '@type': 'Event',
                  '@id': 'https://events.unimelb.edu.au/synthetic-one#event',
                  'eventStatus': 'https://schema.org/EventScheduled'} | changes


def parse_ld(document, **kwargs):
    return e.parse_official_snapshot(encoded(document), source_id='unimelb',
                                    retrieved_at=FETCHED, provenance='fixture', **kwargs)


def moshtix_row(event_id=101, **changes):
    return {'id': event_id, 'name': 'SYNTHETIC show', 'startDate': '2026-11-01T19:00:00+11:00',
            'venue': {'id': 2362, 'name': 'SYNTHETIC venue',
                      'address': {'locality': 'SYNTHETIC locality', 'region': 'VIC'}}} | changes


def moshtix(*rows, total=None):
    return encoded({'data': {'viewer': {'getEvents': {
        'totalCount': len(rows) if total is None else total, 'items': list(rows)}}}})


def parse_mosh(raw, **kwargs):
    return e.parse_moshtix(raw, now=NOW, retrieved_at=FETCHED, provenance='fixture', **kwargs)


class MoshtixEventTests(TestCase):
    def test_nine_results_extend_candidate_with_ids_rank_and_provenance(self):
        raw = moshtix(*(moshtix_row(i) for i in range(101, 110)), total=200)
        cards = parse_mosh(raw)
        self.assertEqual(len(cards), 9)
        self.assertTrue(all(isinstance(c, Candidate) for c in cards))
        self.assertEqual([c.source_event_id for c in cards], [str(i) for i in range(101, 110)])
        self.assertEqual([c.rank for c in cards], list(range(1, 10)))
        for card in cards:
            self.assertEqual(card.source_id, 'moshtix')
            self.assertEqual(card.starts_at, '2026-11-01T19:00:00+11:00')
            self.assertEqual(card.date_precision, 'time')
            self.assertEqual(card.status, 'unknown')
            self.assertEqual(card.eligibility, 'public')
            self.assertTrue(card.searchable)
            self.assertIsNone(card.lineup)
            self.assertIsNone(card.entry_fee)
            self.assertIsNone(card.artwork_price)
            self.assertEqual(card.venue_key, 'official:venue:thornbury-theatre')
            self.assertEqual(card.provenance, 'fixture')
            self.assertTrue(card.key.startswith('fixture:event:'))
            self.assertIsNone(card.url)
            self.assertEqual(card.evidence[0].event_id, card.source_event_id)
            self.assertEqual(card.evidence[0].retrieved_at, FETCHED)
            self.assertEqual(len(card.evidence[0].snapshot_sha256), 64)

    def test_decimal_graphql_ids_reconcile_without_title_merges(self):
        cards = parse_mosh(moshtix(moshtix_row(101), moshtix_row('101', name='SYNTHETIC renamed'),
                                   moshtix_row(102)))
        self.assertEqual(len(cards), 2)
        one = next(c for c in cards if c.source_event_id == '101')
        self.assertEqual(len(one.evidence), 2)
        self.assertEqual({p.position for p in one.evidence}, {1, 2})

    def test_exact_venue_id_is_required_for_thornbury_join(self):
        row = moshtix_row()
        row['venue']['id'] = 700
        row['venue']['name'] = 'Thornbury Theatre'
        self.assertIsNone(parse_mosh(moshtix(row))[0].venue_key)

    def test_past_now_distant_date_only_and_missing_zone_suppressed(self):
        for start in ('2020-01-01T19:00:00+11:00', NOW.isoformat(),
                      (NOW + timedelta(weeks=8, seconds=1)).isoformat(),
                      '2026-11-01', '2026-11-01T19:00:00', None):
            with self.subTest(start=start):
                cards = parse_mosh(moshtix(moshtix_row(101, startDate=start), moshtix_row(102)))
                self.assertEqual([c.source_event_id for c in cards], ['102'])
        card, = parse_mosh(moshtix(moshtix_row(startDate=(NOW+timedelta(weeks=8)).isoformat())))
        self.assertEqual(card.source_event_id, '101')

    def test_row_and_total_count_limits(self):
        self.assertEqual(parse_mosh(moshtix()), ())
        for raw in (moshtix(*(moshtix_row(i) for i in range(1, 11))),
                    moshtix(moshtix_row(), total=0), moshtix(total=-1), moshtix(total=True)):
            with self.subTest(raw=raw[:100]), self.assertRaises(SourceError):
                parse_mosh(raw)

    def test_invalid_shapes_ids_and_regions_fail_entire_page(self):
        for event_id in (True, 0, -1, '01', '1.0', 2**63, {}, None):
            with self.subTest(event_id=event_id), self.assertRaises(SourceError):
                parse_mosh(moshtix(moshtix_row(), moshtix_row(event_id)))
        for field, value in (('id', None), ('id', True), ('address', {'region': 'NSW'}), ('address', None)):
            row = moshtix_row(); row['venue'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SourceError):
                parse_mosh(moshtix(row))
        for raw in (b'{}', b'null', b'[]', b'{"errors":[{"message":"SYNTHETIC CANARY"}]}'):
            with self.subTest(raw=raw), self.assertRaises(SourceError):
                parse_mosh(raw)

    def test_nine_result_request_preserves_verified_selection_and_guards(self):
        old = transport.events_command(NOW)
        command = transport.events_command(NOW, page_size=9)
        body = json.loads(command[command.index('--data-binary')+1])
        old_body = json.loads(old[old.index('--data-binary')+1])
        self.assertEqual(body['query'], old_body['query'].replace('pageSize:1,', 'pageSize:9,'))
        for part in ('includePastEvents:false', 'publicOnly:true', 'searchableOnly:true',
                     'region:[VIC]', 'pageIndex:0', '$from:Date,$to:Date',
                     'items{id name startDate venue{id name address{locality region}}}'):
            self.assertIn(part, body['query'])
        self.assertEqual(datetime.fromisoformat(body['variables']['from']), NOW)
        self.assertEqual(datetime.fromisoformat(body['variables']['to']), NOW+timedelta(weeks=8))
        for flag, value in (('--max-time', '5'), ('--max-redirs', '0'), ('--retry', '0'),
                            ('--max-filesize', str(MAX_BYTES))):
            self.assertEqual(command[command.index(flag)+1], value)
        self.assertEqual(command[-1], 'https://api.moshtix.com/v1/graphql')
        for size in (0, -1, 10, True, '9', None):
            with self.subTest(size=size), self.assertRaises(SourceError):
                transport.events_command(NOW, page_size=size)

    def test_explicit_fetch_uses_one_bounded_call_and_sanitized_outcomes(self):
        for status, body, expected in ((200, moshtix(moshtix_row()), 'ready'),
            (200, moshtix(), 'empty'), (429, b'SYNTHETIC CANARY', 'rate-limited'),
            (302, b'SYNTHETIC CANARY', 'error'), (503, b'SYNTHETIC CANARY', 'error'),
            (200, b'SYNTHETIC CANARY', 'invalid')):
            with self.subTest(status=status, expected=expected), patch.object(
                    transport, 'events', return_value=(status, body)) as send:
                outcome = e.fetch_moshtix(NOW)
                self.assertEqual(outcome.status, expected)
                self.assertNotIn('CANARY', repr(outcome))
                send.assert_called_once_with(NOW, page_size=9)
        for error, status in ((TimeoutError('CANARY'), 'timeout'), (OSError('CANARY'), 'invalid')):
            with patch.object(transport, 'events', side_effect=error):
                outcome = e.fetch_moshtix(NOW)
                self.assertEqual(outcome.status, status)
                self.assertNotIn('CANARY', repr(outcome))


class OfficialSnapshotTests(TestCase):
    def test_metadata_and_jsonld_project_same_identity_with_evidence(self):
        first, = parse(official())
        second, = parse_ld(jsonld(identifier='synthetic-one'))
        merged, = e.reconcile_events([first, second])
        self.assertEqual(merged.key, first.key)
        self.assertEqual(len(merged.evidence), 2)
        self.assertEqual(merged.status, 'scheduled')
        self.assertEqual(merged.eligibility, 'public')
        self.assertEqual(merged.roots, ())
        self.assertEqual(merged.lineage, ())
        self.assertIsNone(merged.artist_key)
        self.assertIsNone(merged.lineup)

    def test_jsonld_graph_array_and_id_url_fallback_without_context_fetch(self):
        row = jsonld(); del row['url']
        for document in (row, [row], {'@graph': [{'@type': 'Organization', 'name': 'SYNTHETIC'}, row]}):
            with self.subTest(document=document), patch.object(transport, 'events') as send:
                card, = parse_ld(document)
                self.assertEqual(card.source_event_id, row['@id'])
                self.assertEqual(card.official_url, 'https://events.unimelb.edu.au/synthetic-one')
                send.assert_not_called()

    def test_jsonld_cancellation_and_postponement_schema_spellings(self):
        for prefix in ('', 'http://schema.org/', 'https://schema.org/'):
            for term, expected in (('EventCancelled', 'cancelled'), ('EventPostponed', 'postponed')):
                with self.subTest(prefix=prefix, term=term):
                    event, = parse_ld(jsonld(**{'@type': [prefix+'Event', prefix+'MusicEvent'],
                                               'eventStatus': prefix+term}))
                    self.assertEqual(event.status, expected)
                    self.assertFalse(e.upcoming_events([event], NOW))

    def test_registry_sources_are_distinct_and_do_not_claim_connections(self):
        expected = {
            'science-gallery': 'https://melbourne.sciencegallery.com/whats-on',
            'rmit': 'https://www.rmit.edu.au/events', 'unimelb': 'https://events.unimelb.edu.au/',
            'forum': 'https://forummelbourne.com.au/shows', 'rrr': 'https://www.rrr.org.au/events',
            'pbs': 'https://www.pbsfm.org.au/news/pbs-events',
            'darebin': 'https://arts.darebin.vic.gov.au/Whats-on/Event-calendar',
        }
        for source_id, url in expected.items():
            self.assertEqual(e.SOURCES[source_id].listing_url, url)
            self.assertEqual(e.SOURCES[source_id].connection, 'supplied-snapshot-only')
            origin = url.split('/', 3)[:3]
            detail = '/'.join(origin) + '/synthetic-event'
            card, = parse(official(url=detail), source_id=source_id)
            self.assertEqual(card.source_id, source_id)
        self.assertEqual(e.UNCONFIRMED_MAPPINGS['deribin theater'], 'darebin')
        self.assertIn('2362', e.SOURCES['thornbury'].listing_url)
        self.assertIn('restaurant_events', e.SOURCE_CATEGORIES)
        with self.assertRaises(SourceError):
            parse(official(), source_id='deribin theater')

    def test_unknown_api_and_wrong_origin_sources_are_rejected(self):
        for source_id in ('unknown', 'moshtix', 'thornbury', 'rmit'):
            with self.subTest(source_id=source_id), self.assertRaises(SourceError):
                parse(official(), source_id=source_id)
        with self.assertRaises(SourceError):
            parse(official(), provenance='live-automated')

    def test_unsafe_links_and_listing_page_are_rejected(self):
        for url in ('http://events.unimelb.edu.au/synthetic', '//events.unimelb.edu.au/synthetic',
            'javascript:alert(1)', 'https://events.unimelb.edu.au.evil.invalid/synthetic',
            'https://evil.invalid@events.unimelb.edu.au/synthetic',
            'https://events.unimelb.edu.au:443/synthetic', 'https://events.unimelb.edu.au./synthetic',
            'https://events.unimelb.edu.au/login', 'https://events.unimelb.edu.au/%6cogin',
            'https://events.unimelb.edu.au/%256cogin', 'https://events.unimelb.edu.au/a/../login',
            'https://events.unimelb.edu.au/a%0a', 'https://events.unimelb.edu.au/a\\b',
            'https://events.unimelb.edu.au/event?redirect=https://evil.invalid',
            'https://events.unimelb.edu.au/', 'https://events.unimelb.edu.au/sso/synthetic', None):
            with self.subTest(url=url), self.assertRaises(SourceError):
                parse(official(url=url))

    def test_supplied_links_are_safe_and_fixture_links_not_displayed(self):
        fixture, = parse(official(url='https://events.unimelb.edu.au/synthetic-one/#details'))
        supplied, = parse(official(), provenance='supplied-snapshot')
        self.assertIsNone(fixture.url)
        self.assertEqual(fixture.official_url, supplied.url)
        self.assertTrue(supplied.key.startswith('official:event:'))
        self.assertEqual(len(e.reconcile_events([fixture, supplied])), 2)

    def test_restaurant_is_not_an_event_and_dining_origin_requires_registry(self):
        restaurant = e.EventSource('SYNTHETIC restaurant', 'https://dining.example.invalid/events',
                                   'restaurant_events', 'dining_event')
        row = official(url='https://dining.example.invalid/synthetic-dinner')
        with self.assertRaises(SourceError):
            parse(row, source_id='synthetic-restaurant')
        registry = dict(e.SOURCES, **{'synthetic-restaurant': restaurant})
        dinner, = parse(row, source_id='synthetic-restaurant', sources=registry)
        self.assertEqual(dinner.kind, 'event')
        self.assertEqual(dinner.category, 'dining_event')
        self.assertEqual(e.upcoming_events([dinner], NOW), (dinner,))
        undated, = parse(row | {'startDate': None}, source_id='synthetic-restaurant', sources=registry)
        self.assertEqual(e.upcoming_events([undated], NOW), ())
        self.assertEqual(parse(row | {'kind': 'restaurant'}, source_id='synthetic-restaurant', sources=registry), ())
        self.assertEqual(parse_ld(jsonld(**{'@type': 'Restaurant', 'review': 'SYNTHETIC private taste'})), ())

    def test_public_student_invite_and_unknown_never_infer_user_eligibility(self):
        for eligibility in ('public', 'student', 'invite', 'unknown', None):
            card, = parse(official(eligibility=eligibility))
            self.assertEqual(card.eligibility, eligibility or 'unknown')
            self.assertEqual(bool(e.upcoming_events([card], NOW)), eligibility == 'public')
        hidden, = parse(official(searchable=False))
        self.assertEqual(e.upcoming_events([hidden], NOW), ())
        # Being hosted by a university is insufficient to infer public access.
        row = jsonld(); row.pop('eligibility')
        self.assertEqual(parse_ld(row)[0].eligibility, 'unknown')

    def test_admission_is_separate_from_known_per_artwork_budget(self):
        card, = parse(official(admission={'price': '12', 'priceCurrency': 'AUD'}))
        self.assertEqual((card.entry_fee, card.entry_currency), ('12', 'AUD'))
        self.assertEqual(card.artwork_label(), 'Artwork price unknown; check official source')
        for amount, currency, per, expected in (
            ('2499.99', 'AUD', 'individual artwork', 'within budget'),
            ('2500', 'AUD', 'individual artwork', 'outside budget'),
            ('100', 'USD', 'individual artwork', 'unknown'),
            ('12', 'AUD', 'admission', 'unknown'), (None, 'AUD', 'individual artwork', 'unknown')):
            art, = parse(official(artwork={'price': amount, 'priceCurrency': currency, 'per': per}))
            self.assertIn(expected, art.artwork_label())
        missing, = parse(official())
        self.assertIsNone(missing.entry_fee)
        self.assertIsNone(missing.lineup)

    def test_jsonld_offers_performers_and_ignored_attendee_data(self):
        card, = parse_ld(jsonld(offers=[{'price': '0', 'priceCurrency': 'AUD'}],
            performer=[{'@type': 'MusicGroup', 'name': 'SYNTHETIC band'}],
            attendee={'name': 'CANARY'}, review='CANARY', aggregateRating={'ratingValue': 5}))
        self.assertEqual(card.lineup, ('SYNTHETIC band',))
        self.assertEqual(card.entry_fee, '0')
        self.assertIsNone(card.artwork_price)
        self.assertNotIn('CANARY', repr(card))
        multiple, = parse_ld(jsonld(offers=[{'price': '10', 'priceCurrency': 'AUD'},
                                           {'price': '20', 'priceCurrency': 'AUD'}]))
        self.assertIsNone(multiple.entry_fee)

    def test_malformed_rows_prices_lineups_and_metadata_fail_closed(self):
        mutations = ({'status': 'sold-out'}, {'eligibility': True}, {'searchable': 'true'},
                     {'timezone': 'AEST'}, {'id': {}}, {'name': ''}, {'location': []},
                     {'location': {'address': []}}, {'lineup': ['x'] * 26},
                     {'lineup': 'SYNTHETIC band'}, {'admission': {'price': True, 'priceCurrency': 'AUD'}},
                     {'admission': {'price': '-1', 'priceCurrency': 'AUD'}},
                     {'admission': {'price': 'NaN', 'priceCurrency': 'AUD'}},
                     {'admission': {'price': '3', 'priceCurrency': 'aud'}})
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(SourceError):
                parse(official(**mutation))
        with self.assertRaises(SourceError):
            parse_ld(jsonld(eventStatus={}))


class EventDateTests(TestCase):
    def test_timed_end_and_started_events_are_suppressed(self):
        for row in (official(startDate='2026-10-28T19:00:00+11:00', endDate='2026-10-28T20:00:00+11:00'),
                    official(startDate='2026-10-28T19:00:00+11:00', endDate='2026-10-29T20:00:00+11:00')):
            event, = parse(row)
            self.assertEqual(e.upcoming_events([event], NOW), ())
        ended, = parse(official(startDate='2026-10-28T19:00:00+11:00', endDate='2026-10-28T23:00:00+11:00'))
        self.assertEqual(ended.date_status(NOW), 'ended')

    def test_date_only_preserves_precision_and_uses_melbourne_day(self):
        for day, expected in (('2026-10-27', 'past-start'), ('2026-10-28', 'today-date'),
                              ('2026-10-29', 'future-date')):
            event, = parse(official(startDate=day))
            self.assertEqual(event.starts_at, day)
            self.assertEqual(event.date_precision, 'date')
            self.assertEqual(event.date_status(NOW), expected)
        event, = parse(official(startDate='2026-10-29'))
        self.assertEqual(event.date_status(NOW+timedelta(hours=2)), 'today-date')
        self.assertTrue(e.upcoming_events([event], NOW))

    def test_missing_timezone_never_uses_machine_local_timezone(self):
        for value in ('2026-11-01T19:00:00', '2026-11-01', None):
            event, = parse(official(startDate=value, timezone=None))
            self.assertEqual(event.source_start, value)
            self.assertEqual(event.date_status(NOW), 'unknown-date')
            self.assertFalse(e.upcoming_events([event], NOW))

    def test_explicit_offset_is_sufficient_without_named_zone(self):
        event, = parse(official(startDate='2026-11-01T08:00:00Z', timezone=None))
        self.assertEqual(event.starts_at, '2026-11-01T08:00:00+00:00')
        self.assertEqual(event.date_status(NOW), 'future')

    def test_melbourne_summer_and_winter_offsets(self):
        for value, expected in (('2027-01-01T19:00:00', '+11:00'), ('2027-07-01T19:00:00', '+10:00')):
            event, = parse(official(startDate=value))
            self.assertTrue(event.starts_at.endswith(expected))

    def test_dst_gap_and_fold_without_offset_remain_unknown(self):
        for value in ('2026-10-04T02:30:00', '2027-04-04T02:30:00'):
            event, = parse(official(startDate=value))
            self.assertIsNone(event.starts_at)
            self.assertEqual(event.date_precision, 'unknown')
            self.assertEqual(event.date_status(NOW), 'unknown-date')

    def test_explicit_offsets_disambiguate_fold_and_reject_gap_mismatch(self):
        for offset in ('+10:00', '+11:00'):
            event, = parse(official(startDate='2027-04-04T02:30:00'+offset))
            self.assertEqual(event.date_precision, 'time')
            self.assertTrue(event.starts_at.endswith(offset))
        for value in ('2026-10-04T02:30:00+10:00', '2026-10-04T02:30:00+11:00',
                      '2027-01-01T19:00:00+10:00', '2027-07-01T19:00:00+11:00'):
            with self.subTest(value=value), self.assertRaises(SourceError):
                parse(official(startDate=value))

    def test_date_only_end_is_inclusive_not_invented_midnight(self):
        event, = parse(official(startDate='2026-10-27', endDate='2026-10-28'))
        self.assertEqual(event.ends_at, '2026-10-28')
        self.assertEqual(event.date_status(NOW), 'past-start')
        self.assertEqual(event.date_status(NOW+timedelta(days=1)), 'ended')

    def test_invalid_dates_mixed_precision_and_reversed_range_fail(self):
        for changes in ({'startDate': '2026-02-30'}, {'startDate': '20261101'},
                        {'startDate': 'tomorrow'}, {'startDate': True},
                        {'startDate': '9999-12-31T23:59:59-12:00'},
                        {'startDate': '0001-01-01T00:00:00+14:00'},
                        {'startDate': '0001-01-01T00:00:00'},
                        {'endDate': '2026-10-31T19:00:00+11:00'}, {'endDate': '2026-11-02'},
                        {'startDate': '2026-11-02', 'endDate': '2026-11-01'}):
            with self.subTest(changes=changes), self.assertRaises(SourceError):
                parse(official(**changes))

    def test_naive_clock_rejected_before_transport_or_filter(self):
        with patch.object(transport, 'events') as send:
            self.assertEqual(e.fetch_moshtix(NOW.replace(tzinfo=None)).status, 'invalid')
            send.assert_not_called()
        with self.assertRaises(SourceError):
            e.upcoming_events(parse(official()), NOW.replace(tzinfo=None))
        with self.assertRaises(SourceError):
            e.upcoming_events([], NOW.replace(tzinfo=None))


class EventReconciliationTests(TestCase):
    def test_same_id_renamed_url_and_url_only_alias_keep_all_provenance(self):
        first, = parse(official())
        second, = parse(official(name='SYNTHETIC renamed', url='https://events.unimelb.edu.au/synthetic-new'), retrieved_at=LATER)
        third, = parse(official(id=None, url='https://events.unimelb.edu.au/synthetic-new'), retrieved_at=LATER)
        result, = e.reconcile_events([first, second, third])
        self.assertEqual(len(result.evidence), 3)
        self.assertEqual({p.official_url for p in result.evidence}, {first.official_url, second.official_url})

    def test_transitive_id_url_bridge_in_any_order(self):
        a, = parse(official(id='synthetic-a'))
        b, = parse(official(id='synthetic-b', url='https://events.unimelb.edu.au/synthetic-b'))
        bridge, = parse(official(id='synthetic-a', url='https://events.unimelb.edu.au/synthetic-b'))
        for rows in ([a,b,bridge], [bridge,a,b], [a,bridge,b], [b,a,bridge]):
            with self.subTest(rows=[r.source_event_id for r in rows]):
                merged, = e.reconcile_events(rows)
                self.assertEqual(len(merged.evidence), 3)

    def test_same_title_different_identity_and_cross_source_id_do_not_merge(self):
        a, = parse(official())
        b, = parse(official(id='synthetic-two', url='https://events.unimelb.edu.au/synthetic-two'))
        c, = parse(official(url='https://www.rmit.edu.au/synthetic-one'), source_id='rmit')
        self.assertEqual(len(e.reconcile_events([a,b,c])), 3)

    def test_url_shaped_source_id_does_not_collide_with_url_only_key(self):
        a, = parse(official(id=None))
        b, = parse(official(id=a.official_url, url='https://events.unimelb.edu.au/synthetic-other'))
        self.assertNotEqual(a.key, b.key)
        self.assertEqual(len(e.reconcile_events([a,b])), 2)

    def test_newer_cancellation_postponement_end_and_unknown_dates_suppress(self):
        original, = parse(official())
        for changes in ({'status': 'cancelled'}, {'status': 'postponed'}, {'status': 'ended'}, {'startDate': None}):
            update, = parse(official(**changes), retrieved_at=LATER)
            for rows in ([original, update], [update, original]):
                with self.subTest(changes=changes):
                    merged, = e.reconcile_events(rows)
                    self.assertEqual(len(merged.evidence), 2)
                    self.assertEqual(e.upcoming_events(rows, NOW), ())

    def test_equal_timestamp_cancellation_wins_regardless_of_order(self):
        original, = parse(official())
        cancelled, = parse(official(status='cancelled'))
        for rows in ([original,cancelled], [cancelled,original]):
            merged, = e.reconcile_events(rows)
            self.assertEqual(merged.status, 'cancelled')
            self.assertFalse(e.upcoming_events(rows, NOW))

    def test_unknown_status_cannot_reinstate_cancelled_event(self):
        old, = parse(official(status='cancelled'))
        new, = parse(official(status=None), retrieved_at=LATER)
        for rows in ([new,old], [old,new]):
            merged, = e.reconcile_events(rows)
            self.assertEqual(merged.status, 'cancelled')
            self.assertEqual(len(merged.evidence), 2)
            self.assertFalse(e.upcoming_events(rows, NOW))

    def test_equal_time_unknown_date_and_nonsearchable_updates_fail_safe(self):
        old, = parse(official(status=None))
        for changes in ({'startDate':None}, {'searchable':False}, {'eligibility':'invite'},
                        {'startDate':'2026-11-01', 'timezone':None}):
            new, = parse(official(**changes))
            for rows in ([old,new], [new,old]):
                self.assertFalse(e.upcoming_events(rows, NOW))

    def test_newer_explicit_reschedule_can_replace_postponement(self):
        old, = parse(official(status='postponed'))
        new, = parse(official(status='rescheduled', startDate='2026-11-02T19:00:00+11:00'), retrieved_at=LATER)
        result, = e.upcoming_events([new,old], NOW)
        self.assertEqual(result.starts_at, new.starts_at)
        self.assertEqual(len(result.evidence), 2)

    def test_newer_missing_lineup_and_price_do_not_inherit_stale_values(self):
        old, = parse(official(lineup=['SYNTHETIC act'], admission={'price': '20', 'priceCurrency': 'AUD'}))
        new, = parse(official(), retrieved_at=LATER)
        merged, = e.reconcile_events([old,new])
        self.assertIsNone(merged.lineup)
        self.assertIsNone(merged.entry_fee)
        self.assertIsNone(merged.artwork_price)

    def test_reconciliation_and_evidence_budgets(self):
        card, = parse(official())
        self.assertEqual(len(e.reconcile_events([card]*100)), 1)
        for rows in ([card]*101, [replace(card, evidence=card.evidence*101)], iter([card]), [Candidate(
            'fixture:event:one', 'SYNTHETIC', 'event', 'fixture', 'fixture', 1, FETCHED)]):
            with self.assertRaises(SourceError):
                e.reconcile_events(rows)


class EventSnapshotBoundsTests(TestCase):
    def test_response_byte_boundary_and_strict_json_for_both_parsers(self):
        for raw, parser in ((snapshot(official()), lambda raw: e.parse_official_snapshot(
                raw, source_id='unimelb', retrieved_at=FETCHED, provenance='fixture')),
                (moshtix(moshtix_row()), parse_mosh)):
            bounded = raw + b' '*(MAX_BYTES-len(raw))
            self.assertEqual(len(parser(bounded)), 1)
            for bad in (bounded+b' ', b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}',
                        b'\xff', b'[', b'['*2000+b']'*2000, 'not bytes'):
                with self.subTest(bad=repr(bad[:40])), self.assertRaises(SourceError):
                    parser(bad)

    def test_official_rows_graph_nodes_and_nested_lineup_offer_bounds(self):
        self.assertEqual(len(parse(*(official(id=str(i), url=f'https://events.unimelb.edu.au/synthetic-{i}') for i in range(9)))), 9)
        self.assertEqual(parse(), ())
        for document in ({'format':'event-metadata-v1', 'events':[official()]*10},
                         {'@graph':[{'@type':'Organization'}]*10},
                         {'format':'event-metadata-v1', 'events':{}}, [None],
                         {'format':'event-metadata-v2', 'events':[]},
                         jsonld(offers=[{'price': '1', 'priceCurrency':'AUD'}]*10)):
            with self.subTest(document=document), self.assertRaises(SourceError):
                parse_ld(document)

    def test_bad_retrieval_or_provenance_rejected(self):
        for value in ('2026-10-28T12:00:00', None, 'yesterday'):
            with self.subTest(value=value), self.assertRaises(SourceError):
                parse(official(), retrieved_at=value)
        with self.assertRaises(SourceError):
            parse(official(), provenance='verified-live')
