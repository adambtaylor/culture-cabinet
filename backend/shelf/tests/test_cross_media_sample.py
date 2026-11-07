"""Real templates and test-only authentication; offline samples cannot change taste."""
from contextlib import ExitStack
from copy import deepcopy
from html.parser import HTMLParser
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.html import escape

from shelf import cross_media as graph
from shelf.cross_media_views import sample

URL = '/samples/cross-media/'


class Markup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class CrossMediaSampleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='fictional-sample-reader')

    def setUp(self):
        self.client.force_login(self.user)

    def test_default_and_fixed_selectors_need_no_profile_or_grant(self):
        response = self.client.get(URL)
        self.assertEqual(response.context['trail_id'], 'dune')
        for trail in graph.SAMPLE_IDS:
            with self.subTest(trail=trail):
                response = self.client.get(URL, {'trail': trail})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['trail_id'], trail)
                self.assertTemplateUsed(response, 'cabinet/cross_media_sample.html')
                self.assertContains(response, 'aria-current="page"', count=1)

    def test_invalid_and_duplicate_selectors_refused_before_fixture_io(self):
        with patch('shelf.cross_media_views.sample_context', side_effect=AssertionError('invalid selector reached fixture')):
            for query in ('trail=', 'trail=unknown', 'trail=../dune', 'trail=Dune',
                          'trail=dune&trail=dune-messiah', 'trail=dune&trail=dune'):
                self.assertEqual(self.client.get(URL + '?' + query).status_code, 404)

    def test_login_redirect_without_sample_access(self):
        with patch('shelf.cross_media_views.sample_context', side_effect=AssertionError('anonymous fixture access')):
            response = Client().get(URL + '?trail=dune-messiah')
        self.assertRedirects(response, '/login/?next=/samples/cross-media/%3Ftrail%3Ddune-messiah', fetch_redirect_response=False)

    def test_only_get_allowed(self):
        with patch('shelf.cross_media_views.sample_context', side_effect=AssertionError('method reached fixture')):
            for method in ('post', 'put', 'patch', 'delete', 'head', 'options', 'trace'):
                response = getattr(self.client, method)(URL)
                self.assertEqual(response.status_code, 405, method)
                self.assertEqual(response['Allow'], 'GET')

    def test_rendered_research_paths_and_honest_provenance(self):
        dune = self.client.get(URL)
        for label in ('RECONSTRUCTED research fixtures; not captured API responses.',
                      'Source revision, statement IDs and freshness unknown.',
                      'Hardcover mapping:', 'unresolved', 'No supplied Hardcover/ISBN binding',
                      'not the owner’s likes', 'no live catalogue or Hardcover book records',
                      'Why this?', 'Relationship: based-on', 'Recorded direction:',
                      'Traversal: reverse', 'Statement ID: unknown', 'Revision: unknown',
                      'Checked at: unknown', 'P144', 'P4969', 'tmdb:movie:438631'):
            self.assertContains(dune, label)
        messiah = self.client.get(URL, {'trail': 'dune-messiah'})
        for label in ('Q437383', 'Q1072774', 'Q437365', 'tmdb:tv:9156',
                      'Factual hops: 2', 'Traversal: forward', 'Relationship: identity'):
            self.assertContains(messiah, label)
        for response in (dune, messiah):
            for forbidden in ('faithful adaptation', 'population co-like', 'HDR'):
                self.assertNotContains(response, forbidden)

    def test_synthetic_lanes_memberships_and_distinct_outcome_states(self):
        response = self.client.get(URL, {'trail': 'synthetic-lanes'})
        self.assertEqual(len(response.context['lanes']), 5)
        for label in ('SIMULATED provider rankings and synthetic human history.',
                      'Simulated roots are not the owner’s likes', 'Signal: experienced',
                      'Signal: anticipated', 'Fictional response time:',
                      'SIMULATED original rank (zero-based): 0',
                      'SIMULATED original rank (zero-based): 1',
                      'Round-robin selection and lane memberships',
                      'Synthetic timestamps are fictional; provider freshness unknown.'):
            self.assertContains(response, label)
        for state in ('items', 'empty', 'ambiguous', 'failed', 'timeout', 'rate-limited'):
            self.assertContains(response, 'Status: ' + state)
        self.assertNotContains(response, 'href="https://www.themoviedb.org/movie/8')

    def test_canonical_links_no_remote_assets_or_write_controls(self):
        for trail in graph.SAMPLE_IDS:
            response = self.client.get(URL, {'trail': trail})
            markup = Markup(response.content.decode())
            expected = graph.sample_context(trail)
            safe_urls = {expected['root']['url']}
            for card in expected['cards']:
                safe_urls.add(card['url'])
                for step in card['path']:
                    safe_urls.update(e['source_url'] for e in step['evidence'])
            for tag, attrs in markup.tags:
                self.assertNotIn(tag, ('script', 'img', 'iframe', 'object', 'embed', 'form', 'button', 'input', 'style'))
                self.assertFalse(any(name.startswith('on') for name in attrs))
                self.assertNotIn('src', attrs)
                href = attrs.get('href', '')
                if href.startswith('https:'):
                    self.assertIn(href, safe_urls)
                    self.assertRegex(href, r'^https://(?:www\.wikidata\.org/wiki/Q[1-9][0-9]*|www\.themoviedb\.org/(?:movie|tv)/[1-9][0-9]*|openlibrary\.org/(?:works|books|authors)/OL[1-9][0-9]*[WMA])$')
                    self.assertNotIn('?', href)
                elif href:
                    self.assertTrue(href.startswith(('/', '#')), href)
                if attrs.get('target') == '_blank':
                    self.assertTrue({'noopener', 'noreferrer'} <= set(attrs.get('rel', '').split()))
                if tag == 'link':
                    self.assertTrue(href.startswith('/static/'))
            self.assertContains(response, 'View Dune' if trail == 'dune' else 'SAMPLE')

    def test_normal_autoescaping_in_titles_provenance_and_evidence(self):
        context = graph.sample_context('dune')
        hostile = '<script>alert("sample")</script> & <b>fiction</b>'
        context['sample_title'] = hostile
        context['root']['title'] = hostile
        context['cards'][0]['title'] = hostile
        context['cards'][0]['path'][0]['evidence'][0]['statement_id'] = hostile
        context['provenance_label'] = hostile
        with patch('shelf.cross_media_views.sample_context', return_value=context):
            response = self.client.get(URL)
        self.assertContains(response, escape(hostile))
        self.assertNotContains(response, hostile)
        self.assertFalse(any(tag == 'script' for tag, attrs in Markup(response.content.decode()).tags))

    def test_navigation_survives_child_block_overrides(self):
        for template in ('cabinet/provider_profiles.html', 'cabinet/provider_catalogue.html',
                         'cabinet/saved_items.html', 'registration/provider_login.html',
                         'cabinet/cross_media_sample.html'):
            with self.subTest(template=template):
                context = graph.sample_context('dune')
                context.update(cards=[], choice_counts=dict(enjoyed=0, not_for_me=0, interested=0, unknown=0))
                content = render_to_string(template, context)
                self.assertIn('aria-label="Research samples"', content)
                self.assertIn('href="' + URL + '"', content)

    def test_redirect_and_deprecated_movie_trap_uses_canonical_projection(self):
        fixture = json.loads(graph.FIXTURE.read_text())
        cache = graph.resolve_documents(fixture['entities'], redirects=fixture['redirects'],
                                        external_ids=fixture['external_ids'], provenance='reconstructed')
        nodes = {node['key']: node for node in cache.snapshot()['nodes']}
        context = graph.sample_context('dune')
        context['root'] = nodes['wikidata:entity:Q987305']
        context['cards'] = [dict(nodes[path['key']], **{k: v for k, v in path.items() if k != 'key'})
                            for path in graph.paths_from(cache, context['root']['key'])]
        with patch('shelf.cross_media_views.sample_context', return_value=context):
            response = self.client.get(URL)
        self.assertContains(response, 'https://www.wikidata.org/wiki/Q987305')
        self.assertContains(response, 'https://www.themoviedb.org/tv/19566')
        self.assertEqual(response.context['root']['key'], 'wikidata:entity:Q987305')
        self.assertEqual([card['key'] for card in response.context['cards']], ['tmdb:tv:19566'])
        # Redirect origin remains truthful source evidence, never a separate endpoint.
        self.assertContains(response, 'Q16515173 / external_ids.wikidata_id')
        for trap in ('wikidata:entity:Q16515173', '/movie/911852', '/movie/19566'):
            self.assertNotContains(response, trap)

    def test_no_user_state_source_network_or_model_calls_and_no_writes(self):
        session = self.client.session
        session['selected_profile_id'] = 999
        session['fictional_taste_canary'] = {'enjoyed': ['never-use-this']}
        session.save()
        before_session = deepcopy(dict(self.client.session))
        fixture_bytes = graph.FIXTURE.read_bytes()
        tables = connection.introspection.table_names()
        def snapshot():
            with connection.cursor() as cursor:
                return {table: cursor.execute('SELECT * FROM ' + connection.ops.quote_name(table)).fetchall()
                        for table in tables}
        before_db = snapshot()
        forbidden_calls = ('shelf.provider_views._state', 'shelf.provider_views._context',
                           'shelf.durable_intake.hydrate', 'shelf.provider_catalogue._request',
                           'shelf.provider_catalogue.initial_deck', 'shelf.provider_catalogue.search',
                           'shelf.provider_catalogue.neighbors', 'socket.socket',
                           'subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen')
        with ExitStack() as stack:
            for target in forbidden_calls:
                stack.enter_context(patch(target, side_effect=AssertionError(target + ' forbidden')))
            with CaptureQueriesContext(connection) as queries:
                for trail in graph.SAMPLE_IDS:
                    self.assertEqual(self.client.get(URL, {'trail': trail}).status_code, 200)
            for query in queries:
                self.assertTrue(query['sql'].lstrip().upper().startswith('SELECT'), query['sql'])
                self.assertNotIn('shelf_', query['sql'])
            # The view itself also renders with DB access forbidden and no real actor.
            request = RequestFactory().get(URL)
            request.user = SimpleNamespace(is_authenticated=True)
            with self.assertNumQueries(0):
                self.assertEqual(sample(request).status_code, 200)
        self.assertEqual(snapshot(), before_db)
        self.assertEqual(dict(self.client.session), before_session)
        self.assertEqual(graph.FIXTURE.read_bytes(), fixture_bytes)
        self.assertNotContains(self.client.get(URL), 'never-use-this')
