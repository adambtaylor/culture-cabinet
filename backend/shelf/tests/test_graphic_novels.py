"""Actual private catalogue templates, owned synthetic profiles and source fixtures."""
from copy import deepcopy
from html.parser import HTMLParser
from itertools import permutations
import time
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings

from shelf import durable_intake as durable, intake, provider_catalogue as p
from shelf.models import FeedbackEvent, FeedbackProjection, Grant, IntakeRun, SavedItem
from shelf.tests.graphic_novel_fixtures import rows, STYLES
from shelf.tests.test_provider_views import ProviderFixture


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.links = []
        self.feed(content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


class DuplicateMetadataRenderTests(SimpleTestCase):
    def render(self, row):
        card = p.parse_candidates('hardcover', 'book', [row],
                                  lane='synthetic-books', lane_label='SYNTHETIC source')[0]
        return render_to_string('cabinet/book_details.html', {'card': card.data()})

    def test_conflicting_series_descriptions_render_uncertainty_in_every_order(self):
        row = rows()[1]
        first = row['book_series'][0]
        second = dict(first, compilation=True, details='SYNTHETIC other details',
                      series=dict(first['series'], name='SYNTHETIC other name', slug='synthetic-other'))
        expected = None
        for members in ([first, second], [first, second, first], [first, second, second]):
            for ordered in permutations(members):
                html = self.render(dict(row, book_series=list(ordered)))
                for text in ('Series name ambiguous', 'Series slug ambiguous', 'Series details ambiguous',
                             'Position 1', 'Compilation status ambiguous', 'Hardcover series 73001'):
                    self.assertIn(text, html)
                for text in ('Compilation / omnibus', 'Not marked as a compilation',
                             first['details'], second['details'], first['series']['name'], second['series']['name']):
                    self.assertNotIn(text, html)
                expected = expected or html
                self.assertEqual(html, expected)

    def test_position_conflict_does_not_hide_agreed_compilation_classification(self):
        row = rows()[1]
        for compilation, label in ((True, 'Compilation / omnibus (separate work)'),
                                   (False, 'Not marked as a compilation')):
            first = dict(row['book_series'][0], compilation=compilation)
            variants = [first, dict(first, position=2.5), dict(first, position=None)]
            expected = None
            for members in (variants[:2], variants, [variants[0], variants[1], variants[0]]):
                for ordered in permutations(members):
                    html = self.render(dict(row, book_series=list(ordered)))
                    self.assertIn('Position ambiguous', html)
                    self.assertIn(label, html)
                    self.assertNotIn('Position 1', html)
                    self.assertNotIn('Position 2.5', html)
                    self.assertNotIn('Compilation status ambiguous', html)
                    expected = expected or html
                    self.assertEqual(html, expected)

    def test_edition_uncertainty_renders_independently_of_canonical_reference(self):
        row = rows()[0]
        first = dict(row['editions'][0], canonical_id=85000)
        for field, alternative, label, retained in (
                ('edition_format', 'SYNTHETIC hardcover', 'Format ambiguous', first['edition_information']),
                ('edition_information', 'SYNTHETIC revised', 'Edition information ambiguous', first['edition_format']),
                ('canonical_id', 85001, 'Duplicate-edition reference ambiguous', first['edition_format'])):
            second = dict(first, **{field: alternative})
            expected = None
            for editions in ([first, second], [first, second, first], [first, second, second]):
                for ordered in permutations(editions):
                    html = self.render(dict(row, editions=list(ordered)))
                    self.assertIn(label, html)
                    self.assertIn(retained, html)
                    self.assertEqual(html.count('Edition 84001'), 1)
                    if field == 'canonical_id':
                        self.assertNotIn('Source duplicate-edition reference', html)
                    else:
                        self.assertIn('Source duplicate-edition reference 85000 (unresolved)', html)
                        self.assertNotIn('Duplicate-edition reference ambiguous', html)
                        self.assertNotIn(first[field], html)
                        self.assertNotIn(alternative, html)
                    expected = expected or html
                    self.assertEqual(html, expected)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class GraphicNovelViewsTests(ProviderFixture, TestCase):
    def fetch(self, source=None, **fields):
        with patch.object(p, '_request', return_value={'books': rows() if source is None else source}) as request:
            response = self.post('browse', book_category='graphic_novels', **fields)
        self.assertEqual(response.status_code, 302)
        request.assert_called_once()
        return self.client.get('/profiles/1/')

    def test_visible_post_browse_actual_cards_and_series_groups_no_get_io(self):
        with patch.object(p, '_request', side_effect=AssertionError('GET must not fetch')):
            initial = self.client.get('/profiles/1/?book_category=graphic_novels')
            self.assertContains(initial, 'Fetch graphic novels')
            self.assertContains(initial, 'name="book_category" value="graphic_novels"')
        page = self.fetch()
        self.assertContains(page, 'Graphic novels · page 1')
        for style in STYLES:
            self.assertContains(page, 'SYNTHETIC ' + style)
        self.assertContains(page, 'Graphic novel · Book', count=6)
        self.assertContains(page, 'Position 2.5')
        self.assertContains(page, 'Compilation / omnibus (separate work)')
        self.assertEqual([s['id'] for s in page.context['result_series']], [73001, 73002])
        self.assertEqual(len(page.context['result_series'][0]['works']), 3)
        self.assertEqual(page.context['choice_counts']['enjoyed'], 0)
        self.assertEqual(IntakeRun.objects.get().history, [])
        self.assertContains(page, 'not a verified population co-like graph')
        with patch.object(p, '_request', side_effect=AssertionError('cached GET must not fetch')):
            self.assertEqual(self.client.get('/profiles/1/intake/').status_code, 200)

    def test_template_escaping_safe_links_no_inline_cover_or_invented_preview(self):
        source = rows()[:1]
        source[0].update(title='<script>SYNTHETIC</script>', description='<b>plain synopsis</b>',
                         links={'publisher': 'https://invented.example.org'})
        source[0]['image']['url'] += '?w=1&h=2'
        page = self.fetch(source)
        self.assertContains(page, '&lt;script&gt;SYNTHETIC&lt;/script&gt;')
        self.assertContains(page, '&lt;b&gt;plain synopsis&lt;/b&gt;')
        self.assertNotContains(page, '<script>SYNTHETIC</script>')
        self.assertNotContains(page, 'invented.example.org')
        self.assertNotContains(page, '<img')
        self.assertContains(page, 'Publisher / official preview unavailable')
        self.assertIn("img-src 'self'", page.headers['Content-Security-Policy'])
        links = Links(page.content).links
        cover = next(link for link in links if link.get('href', '').startswith('https://covers.'))
        self.assertEqual(cover['target'], '_blank')
        self.assertEqual(set(cover['rel'].split()), {'noopener', 'noreferrer'})
        self.assertEqual(cover['referrerpolicy'], 'no-referrer')
        canonical = next(link for link in links if link.get('href') == 'https://hardcover.app/books/synthetic-graphic-94001')
        self.assertIn('opens a new tab', canonical['aria-label'])
        self.assertEqual(canonical['rel'], 'noopener noreferrer')

    def test_unsafe_and_missing_optional_values_are_rendered_honestly(self):
        source = rows()[:3]
        source[0].update(image={'url': 'javascript:alert(1)'}, description=42, book_series='bad', editions={})
        source[1].update(image=None, description=None, book_series=[], editions=[])
        for field in ('image', 'description', 'book_series', 'editions'):
            source[2].pop(field)
        page = self.fetch(source)
        for text in ('Cover link unavailable: invalid source value.', 'No cover link supplied.',
                     'Cover link unknown in this source snapshot.', 'Synopsis unavailable: invalid source value.',
                     'No synopsis supplied.', 'Synopsis unknown:', 'No series membership supplied',
                     'Series membership unknown', 'No editions supplied.', 'Editions unknown'):
            self.assertContains(page, text)
        self.assertNotContains(page, 'javascript:')

    def test_source_filtered_pagination_does_not_claim_books_exhausted(self):
        source = rows()
        source += [dict(source[0], id=95000+i, slug=f'synthetic-page-{i}', editions=[]) for i in range(3)]
        page = self.fetch(source)
        self.assertContains(page, 'Fetch next graphic novels page')
        self.assertEqual(page.context['browse']['next_page'], 2)
        page = self.fetch([], page=2, query='memoir')
        self.assertContains(page, 'does not establish that the full catalogue is exhausted')
        self.assertNotContains(page, 'Fetch next graphic novels page')
        page = self.fetch(source, page=10)
        self.assertIsNone(page.context['browse']['next_page'])
        self.assertContains(page, 'bounded window')

    def test_empty_local_title_filter_keeps_source_page_navigation(self):
        source = rows()
        source += [dict(source[0], id=95000+i, slug=f'synthetic-page-{i}', editions=[]) for i in range(3)]
        page = self.fetch(source, query='no matching title in this page')
        self.assertEqual(page.context['browse']['next_page'], 2)
        self.assertContains(page, 'Fetch next graphic novels page')
        self.assertContains(page, 'value="no matching title in this page"')
        self.assertContains(page, 'does not establish that the full catalogue is exhausted')
        self.assertNotContains(page, source[0]['title'])

    def test_add_save_duplicate_editions_and_logout_preserve_book_without_taste_mutation(self):
        self.fetch(rows()[:1])
        key = 'hardcover:book:94001'
        self.assertEqual(self.post('add', candidate_id=key).status_code, 302)
        before = deepcopy(self.state())
        for _ in range(2):
            response = self.client.post('/profiles/1/saved/save/', {'item_id': key, 'list_kind': 'read'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[intake.SESSION_KEY], before)
        item = SavedItem.objects.get()
        self.assertEqual((item.provider, item.medium, item.source_id, item.list_kind), ('hardcover', 'book', 94001, 'read'))
        self.assertEqual(IntakeRun.objects.get().history, [])
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(FeedbackProjection.objects.count(), 0)
        self.assertEqual(self.post('choose', candidate_id=key, choice='unknown').status_code, 302)
        run = IntakeRun.objects.get()
        self.assertEqual(len(run.history), 1)
        self.assertEqual(run.history[0]['card']['key'], key)
        self.assertNotIn('book_metadata', run.history[0]['card'])
        self.assertEqual(self.state()['choices'], {key: 'unknown'})
        self.client.logout()
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': 1})
        with patch.object(p, '_request', side_effect=AssertionError('no source fetch')):
            page = self.client.get('/profiles/1/saved/')
        self.assertContains(page, 'SYNTHETIC memoir')
        self.assertEqual(SavedItem.objects.get().pk, item.pk)
        state = self.state()
        self.assertEqual(state['choices'], {key: 'unknown'})
        self.assertNotIn('book_metadata', state['candidates'][key])

    def test_old_cached_cards_enrich_without_identity_or_expiry_change_and_history_survives(self):
        state = self.state()
        old = p.parse_candidates('hardcover', 'book', [rows()[0]], lane='old-lane', lane_label='Old lane')[0].data()
        old.pop('book_metadata')
        state['candidates'][old['key']] = old
        state['queued'] = [old['key']]
        session = self.client.session
        session[intake.SESSION_KEY] = state
        session.save()
        page = self.client.get('/profiles/1/')
        # Prototype selector has not yet selected it; explicitly fetched metadata enriches it.
        before = self.state()['expires_at']
        self.fetch(rows()[:1])
        state = self.state()
        self.assertEqual(state['expires_at'], before)
        self.assertEqual(state['candidates'][old['key']]['lane'], 'old-lane')
        self.assertEqual(state['candidates'][old['key']]['book_metadata']['category']['id'], 4)
        self.assertEqual(self.post('add', candidate_id=old['key']).status_code, 302)
        self.assertEqual(self.post('choose', candidate_id=old['key'], choice='enjoyed').status_code, 302)
        session = self.client.session
        session[intake.SESSION_KEY]['expires_at'] = time.time() - 1
        session.save()
        with patch.object(p, '_request', side_effect=AssertionError('expiry must not fetch')):
            expired = self.client.get('/profiles/1/')
        self.assertEqual(expired.context['search_results'], [])
        self.assertEqual(expired.context['choice_counts']['enjoyed'], 1)
        self.assertIsNone(expired.context['browse'])
        self.assertEqual(IntakeRun.objects.get().history[0]['card']['key'], old['key'])

    def test_browse_csrf_stale_receipts_and_own_profile_authority(self):
        state = self.state()
        payload = {'action': 'browse', 'book_category': 'graphic_novels', 'page': 1,
                   'revision': state['revision'], 'nonce': state['nonce']}
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        session = secure.session
        session['selected_profile_id'] = 1
        session.save()
        page = secure.get('/profiles/1/')
        with patch.object(p, '_request') as source:
            self.assertEqual(secure.post(self.url, payload).status_code, 403)
            self.assertEqual(self.client.post(self.url, dict(payload, revision=999)).status_code, 409)
            self.assertEqual(self.post('browse', book_category='prose').status_code, 400)
            self.assertEqual(self.post('browse', book_category='graphic_novels', page=11).status_code, 400)
            self.assertEqual(self.client.post('/profiles/2/catalogue/action/', payload).status_code, 404)
            source.assert_not_called()
        with patch.object(p, '_request', return_value={'books': rows()}) as source:
            self.assertEqual(self.client.post(self.url, payload).status_code, 302)
            self.assertEqual(self.client.post(self.url, payload).status_code, 302)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(self.client.post(self.url, dict(payload, page=2)).status_code, 409)
        with patch.object(p, '_request') as source:
            self.grant.can_select = False
            self.grant.save()
            self.assertEqual(self.post('browse', book_category='graphic_novels').status_code, 404)
            self.client.force_login(self.other)
            self.assertEqual(self.client.post(self.url, payload).status_code, 404)
            source.assert_not_called()

    def test_revoked_authority_after_browse_and_error_do_not_store_results(self):
        self.state()
        for failure in (False, True):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
            def fetch(*args, **kwargs):
                Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
                if failure:
                    raise p.CatalogueError('graphql')
                return {'books': rows()}
            with patch.object(p, '_request', side_effect=fetch):
                self.assertEqual(self.post('browse', book_category='graphic_novels').status_code, 404)
            self.assertNotIn(intake.SESSION_KEY, self.client.session)

    def test_legacy_session_browse_and_provider_failure_keep_existing_cache(self):
        with patch.object(durable, 'available', return_value=False):
            self.fetch()
            previous = self.state()
            with patch.object(p, '_request', side_effect=p.CatalogueError('graphql')):
                response = self.post('browse', book_category='graphic_novels')
            self.assertContains(response, 'Provider request unavailable: graphql', status_code=502)
            self.assertEqual(self.client.session[intake.SESSION_KEY]['search_results'], previous['search_results'])
            self.assertNotEqual(self.client.session[intake.SESSION_KEY]['nonce'], previous['nonce'])
            self.assertEqual(self.post('add', candidate_id='hardcover:book:94001').status_code, 302)
            page = self.client.get('/profiles/1/')
            self.assertContains(page, 'Graphic novel · Book')
