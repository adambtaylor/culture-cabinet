"""Rendered offline preview flow using fictional listings and an injected backend."""
from datetime import timedelta
from html.parser import HTMLParser
import json
from unittest.mock import Mock, patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shelf import obtain_preview as preview
from shelf.acquisition_backend import PrivateSearch
from shelf.models import AcquisitionJob, ObtainSelection
from shelf.tests.test_saved import SavedFixture


class Page(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.forms = []
        self.links = []
        self.current = None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {'action': attrs.get('action'), 'method': attrs.get('method'), 'fields': {}}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None:
            self.current['fields'][attrs.get('name')] = attrs
        elif tag == 'a':
            self.links.append(attrs.get('href'))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def form(self, action):
        return next(f for f in self.forms if f['action'] == action)


def listing(title='Fiction Film 2024 1080p', guid='781234567890123456', category='2000', kind='application/x-nzb'):
    return {'title': title, 'guid': guid, 'id': guid,
            'link': 'http://fiction.invalid/nzb?apikey=FICTION_SECRET',
            'description': 'FICTION_SECRET',
            'enclosure': {'@attributes': {'length': '12345', 'type': kind,
                          'url': 'http://fiction.invalid/nzb?apikey=FICTION_SECRET'}},
            'attr': [{'@attributes': {'name': 'category', 'value': category}},
                     {'@attributes': {'name': 'hydraIndexerName', 'value': 'Fiction Index'}}]}


def fake_backend(*rows):
    backend = Mock()
    backend.search.side_effect = lambda *a, **kw: PrivateSearch(json.dumps({'channel': {'item': list(rows)}}).encode())
    return backend


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ObtainUITests(SavedFixture, TestCase):
    def test_exact_title_whitespace_survives_confirmation_without_rewriting(self):
        title = '  Fiction Film 2024 1080p  '
        self.backend.search.side_effect = fake_backend(listing(title=title)).search.side_effect
        page = self.results()
        row = page.context['obtain_preview']['results'][0]
        self.assertEqual(row['title'], title)
        review = self.client.get(row['review_url'])
        data = dict(review.context['obtain_result_form'].initial,
                    authorized_content='on', storage_check='on')
        url = review.context['obtain_result_confirm_url']
        self.assertEqual(self.client.post(url, {**data, 'edition': title.strip()}).status_code, 409)
        self.assertFalse(AcquisitionJob.objects.exists())
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(AcquisitionJob.objects.get().edition, title)

    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.start = f'{self.base}{self.saved.pk}/preview/'
        self.search = self.start + 'search/'
        self.backend = fake_backend(listing())
        self.factory = patch.object(preview, 'search_backend', return_value=self.backend)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def results(self):
        response = self.client.post(self.search)
        self.assertEqual(response.status_code, 302)
        return self.client.get(response.url)

    def review_page(self):
        results = self.results()
        url = results.context['obtain_preview']['results'][0]['review_url']
        return self.client.get(url)

    def test_saved_link_prefers_preview_and_get_search_form_is_csrf_only(self):
        response = self.client.get(self.base)
        self.assertIn(self.start, Page(response).links)
        self.assertNotIn(f'{self.base}{self.saved.pk}/obtain/', Page(response).links)
        start = self.client.get(self.start)
        form = Page(start).form(self.search)
        self.assertEqual(form['method'], 'post')
        self.assertEqual(set(form['fields']), {'csrfmiddlewaretoken'})
        self.assertTrue(form['fields']['csrfmiddlewaretoken']['value'])
        self.assertContains(start, 'Full film')
        self.assertContains(start, 'Search listings')
        self.backend.search.assert_not_called()
        self.backend.enqueue.assert_not_called()
        self.assertEqual(AcquisitionJob.objects.count(), 0)
        self.assertEqual(self.client.get(f'{self.base}{self.saved.pk}/obtain/').status_code, 200)

    def test_rendered_csrf_flow_exact_hidden_choice_and_disabled_job(self):
        # Submit actual rendered forms through CSRF middleware, including masked tokens.
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        from shelf.authority import SELECTION_KEY
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        start = self.client.get(self.start)
        fields = Page(start).form(self.search)['fields']
        token = fields['csrfmiddlewaretoken']['value']
        self.assertEqual(self.client.post(self.search).status_code, 403)
        response = self.client.post(self.search, {'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        results = self.client.get(response.url)
        row = results.context['obtain_preview']['results'][0]
        self.assertIn(row['review_url'], Page(results).links)
        review = self.client.get(row['review_url'])
        action = review.context['obtain_result_confirm_url']
        form = Page(review).form(action)
        self.assertEqual(set(form['fields']), {'csrfmiddlewaretoken', 'review_token', 'scope', 'edition',
                                              'authorized_content', 'storage_check'})
        for name, value in (('scope', 'film'), ('edition', row['title'])):
            self.assertEqual(form['fields'][name]['type'], 'hidden')
            self.assertEqual(form['fields'][name]['value'], value)
        self.assertEqual(form['fields']['review_token']['type'], 'hidden')
        self.assertContains(review, 'for="id_authorized_content"')
        self.assertContains(review, 'for="id_storage_check"')
        self.assertContains(review, '<select name="storage"', html=False)
        self.assertContains(review, 'Unknown; listing title only')
        self.assertContains(review, 'Unknown; listing claim only')
        self.assertContains(review, 'cannot download')
        data = {k: v['value'] for k, v in form['fields'].items() if v.get('type') == 'hidden'}
        data.update(authorized_content='on', storage_check='on', storage='local_library')
        self.assertEqual(self.client.post(action, {}).status_code, 403)
        response = self.client.post(action, data)
        self.assertEqual(response.status_code, 302)
        status = self.client.get(response.url)
        self.assertContains(status, 'Selection saved locally')
        self.assertContains(status, 'blocked_configuration')
        job = AcquisitionJob.objects.get()
        self.assertEqual(job.edition, row['title'])
        self.assertEqual(job.status, 'blocked_configuration')
        self.assertEqual(str(ObtainSelection.objects.get(job=job).result_id), row['id'])
        self.backend.search.assert_called_once()
        self.backend.enqueue.assert_not_called()
        self.assertEqual(self.client.post(action, data).url, response.url)
        self.assertEqual(AcquisitionJob.objects.count(), 1)

    def test_listing_metadata_has_attribution_size_and_no_private_exposure(self):
        results = self.results()
        review = self.client.get(results.context['obtain_preview']['results'][0]['review_url'])
        for response in (results, review):
            for text in ('Fiction Index', '12345 bytes', 'Unknown; listing title only', 'Unknown; listing claim only'):
                self.assertContains(response, text)
            for secret in ('FICTION_SECRET', 'fiction.invalid', 'apikey', '781234567890123456', 'digest'):
                self.assertNotContains(response, secret)
        self.backend.enqueue.assert_not_called()

    def test_partial_search_survives_reload_and_keeps_exact_selection_checks(self):
        safe = [listing(title=f'Fiction Film 2024 edition {i}', guid=str(i)) for i in range(19)]
        unsafe = listing(title='Fiction Film <UNSAFE_DISPLAY_SECRET>', guid='20')
        self.backend.search.side_effect = fake_backend(unsafe, *safe).search.side_effect
        page = self.results()
        receipt_id = page.context['obtain_preview']['id']
        # GET reloads the persisted receipt, without any provider work or session hint.
        page = self.client.get(f'{self.base}previews/{receipt_id}/')
        self.assertEqual(page.context['obtain_preview']['state'], 'partial')
        rows = page.context['obtain_preview']['results']
        self.assertEqual(len(rows), 19)
        self.assertContains(page, 'Some listings were omitted')
        self.assertContains(page, 'does not show all returned listings')
        self.assertNotContains(page, 'UNSAFE_DISPLAY_SECRET')
        self.assertNotContains(page, 'Search failed')
        self.assertFalse(preview.ObtainResult.objects.filter(preview_id=receipt_id, guid='20').exists())
        review = self.client.get(rows[0]['review_url'])
        self.assertContains(review, 'Some listings were omitted')
        action = review.context['obtain_result_confirm_url']
        data = dict(review.context['obtain_result_form'].initial,
                    authorized_content='on', storage_check='on')
        self.assertEqual(self.client.post(action, data | {'edition': 'Rewritten title'}).status_code, 409)
        self.assertEqual(self.client.post(action, data).status_code, 302)
        job = AcquisitionJob.objects.get()
        self.assertEqual(job.edition, rows[0]['title'])
        self.assertEqual(job.status, 'blocked_configuration')
        from shelf.obtain_acquisition import exact_result
        exact, receipt = exact_result(job, fresh=True)
        self.assertEqual(exact.guid, rows[0]['guid'])
        receipt.expires_at = timezone.now() - timedelta(seconds=1)
        receipt.save()
        from shelf.acquisition_contract import Blocked
        with self.assertRaises(Blocked):
            exact_result(job, fresh=True)
        self.assertEqual(self.client.get(rows[0]['review_url']).status_code, 409)
        self.backend.search.assert_called_once()
        self.backend.enqueue.assert_not_called()

    def test_all_omitted_search_is_distinct_from_empty_or_failed(self):
        self.backend.search.side_effect = fake_backend(listing(title='<UNSAFE_DISPLAY_SECRET>')).search.side_effect
        page = self.results()
        self.assertEqual(page.context['obtain_preview']['state'], 'partial')
        self.assertEqual(page.context['obtain_preview']['results'], [])
        self.assertContains(page, 'Some listings were omitted')
        self.assertContains(page, 'No listings in this stored preview')
        for text in ('No listings found', 'Search failed', 'UNSAFE_DISPLAY_SECRET', 'Review exact listing:'):
            self.assertNotContains(page, text)
        self.assertFalse(AcquisitionJob.objects.exists())
        self.backend.enqueue.assert_not_called()

    def test_display_omission_does_not_mask_conflict_in_ui(self):
        self.backend.search.side_effect = fake_backend(listing(), listing(title='<unsafe>')).search.side_effect
        page = self.results()
        self.assertEqual(page.context['obtain_preview']['state'], 'failed')
        self.assertEqual(page.context['obtain_preview']['results'], [])
        self.assertContains(page, 'Search failed')
        self.assertNotContains(page, 'Some listings were omitted')
        self.backend.enqueue.assert_not_called()

    def test_all_stored_states_and_gets_are_inert(self):
        response = self.results()
        receipt_id = response.context['obtain_preview']['id']
        receipt = preview.ObtainPreview.objects.get(pk=receipt_id)
        url = f'{self.base}previews/{receipt_id}/'
        for state, label in (('empty', 'No listings found'), ('failed', 'Search failed'),
                             ('unconfigured', 'Listing search is not configured'),
                             ('expired', 'Preview expired'), ('searching', 'Searching listings')):
            receipt.state = state
            receipt.save(update_fields=['state'])
            page = self.client.get(url)
            self.assertContains(page, label)
            self.assertNotContains(page, 'Review exact listing:')
            self.assertNotContains(page, 'Confirm local selection')
        receipt.state = 'results'
        receipt.expires_at = timezone.now() - timedelta(seconds=1)
        receipt.save()
        self.assertContains(self.client.get(url), 'Preview expired')
        self.backend.search.assert_called_once()
        self.backend.enqueue.assert_not_called()

    def test_ineligible_pack_multiepisode_ebook_and_torrent_have_no_selection_link(self):
        for medium, row, reason in (
            ('tv', listing('Fiction Series S01 complete', category='5000'), 'pack, multiple episodes'),
            ('tv', listing('Fiction Series S01E01E02', category='5000'), 'pack, multiple episodes'),
            ('book', listing('Fiction Book ebook', category='3030'), 'an ebook listing'),
            ('movie', listing(kind='application/x-bittorrent'), 'unsupported listing type')):
            with self.subTest(medium=medium, reason=reason):
                saved = self.saved if medium == 'movie' else self.item(medium)
                backend = fake_backend(row)
                with patch.object(preview, 'search_backend', return_value=backend):
                    response = self.client.post(f'{self.base}{saved.pk}/preview/search/')
                page = self.client.get(response.url)
                result = page.context['obtain_preview']['results'][0]
                self.assertFalse(result['selectable'])
                self.assertContains(page, reason)
                self.assertNotIn(result['review_url'], Page(page).links)
                self.assertEqual(self.client.get(result['review_url']).status_code, 409)
                backend.enqueue.assert_not_called()

    def test_normal_escaping_of_stored_listing_and_indexer(self):
        page = self.results()
        receipt = preview.ObtainPreview.objects.get(pk=page.context['obtain_preview']['id'])
        row = receipt.results.get()
        row.metadata.update(title='<script>alert("fiction")</script>', indexer='<img src=x onerror=fiction>')
        row.save()
        page = self.client.get(f'{self.base}previews/{receipt.pk}/')
        self.assertContains(page, '&lt;script&gt;')
        self.assertContains(page, '&lt;img src=x onerror=fiction&gt;')
        self.assertNotContains(page, '<script>')
        self.assertNotContains(page, '<img src=x')

    def test_ownership_revocation_and_tampered_confirmation(self):
        review = self.review_page()
        action = review.context['obtain_result_confirm_url']
        form = review.context['obtain_result_form']
        data = dict(form.initial, authorized_content='on', storage_check='on')
        self.assertEqual(self.client.post(action, data | {'edition': 'Different fiction'}).status_code, 409)
        self.assertEqual(self.client.post(self.search, {'query': 'override'}).status_code, 400)
        self.assertEqual(self.client.post(action, data | {'url': 'http://fiction.invalid'}).status_code, 400)
        self.assertEqual(AcquisitionJob.objects.count(), 0)
        self.client.force_login(self.other)
        for url in (self.start, review.request['PATH_INFO']):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(action, data).status_code, 404)
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': 1})
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.client.get(self.start).status_code, 404)
        self.assertEqual(self.client.post(action, data).status_code, 404)
        self.backend.search.assert_called_once()
        self.backend.enqueue.assert_not_called()

    def test_missing_preview_storage_has_no_search_control(self):
        with patch.object(preview, 'available', return_value=False):
            page = self.client.get(self.start)
        self.assertContains(page, 'Preview storage is unavailable')
        self.assertNotIn(self.search, [f['action'] for f in Page(page).forms])
        self.backend.search.assert_not_called()
