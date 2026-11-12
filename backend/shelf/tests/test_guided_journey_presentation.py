"""Guided representation/real template checks, with no DB, request or services."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from django.test import SimpleTestCase, override_settings
from django.utils.safestring import mark_safe

from shelf.guided_journey_presentation import render_guided_journey, guided_journey_context, CATEGORIES, STEPS
from .test_onboarding_experience_views import Document, Poison


def ref(number):
    return str(UUID(int=number, version=4))


def form(path, fields, enabled=True):
    return dict(form_action=path, fields=fields, enabled=enabled, disabled_reason='')


def projection(step='favourites'):
    bound = dict(profile_ref=ref(1), journey_ref=ref(2), expected_revision=1)
    add_fields = dict(**bound, category='book', slot=1, entry_ref=ref(4), base_revision=0,
                      command_id=ref(5), mutation_id=ref(6), title='')
    slot = dict(slot=1, entry_ref=ref(4), title='', current_saved_revision=0, mutation_id=ref(6), removable=False,
        actions={'add': form('/cabinet/journey/favourites/add', add_fields),
                 'remove': form('/cabinet/journey/favourites/remove',
                     {**{k: v for k, v in add_fields.items() if k != 'title'}, 'command_id': ref(7), 'mutation_id': ref(8)}, False)})
    actions = dict(name=form('/cabinet/journey/name', dict(expected_owner_revision=1, label='', command_id=ref(3)), False),
        step={a: form('/cabinet/journey/step', {**bound, 'command_id': ref(20+i), 'step': step, 'action': a})
              for i, a in enumerate(('continue', 'back', 'skip'))},
        prepare=form('/cabinet/journey/prepare', {**bound, 'command_id': ref(9), 'connection_ref': None,
                     'client_key': ref(10), 'refresh_nonce': ref(11)}, False))
    return dict(schema='guided-owner-journey/v2', profile_ref=ref(1), profile_label='Fictional cabinet',
        journey_ref=ref(2), revision=1, step=step,
        steps=[dict(step=s, label=s.title(), state='current' if s == step else 'pending') for s in STEPS],
        providers=[], favourite_categories=[dict(category='book', label='Books', slots=[slot])],
        categories=[dict(category=c, label=c.replace('_', ' ').title(), state='unavailable', code='CAPABILITY_UNAVAILABLE',
            count=0, publication_ref=None, publication_revision=None, ranking_state='none', cards=[], next_action=None, href=None)
            for c in CATEGORIES],
        preparation=dict(state='not_started', run_ref=None, stage=None, code='OK', href=None,
                         publication_ref=None, publication_revision=None, partial=False),
        completion_receipts=[], actions=actions)


def provider(p):
    bound = dict(profile_ref=p['profile_ref'], journey_ref=p['journey_ref'], expected_revision=p['revision'])
    return dict(provider_id='tmdb', label='TMDB', optional=True, required_for_inference=False,
        connection_refs=[ref(40)], config_revisions=[1], storage_state='saved_available', credential_saved=True,
        credential_available=True, test_state='failed', test_scope='authentication only', test_current=False,
        test_observed_at=None, test_expires_at=None, test_code='UNAUTHORIZED', next_action='test', actions={
            'save': form('/cabinet/journey/providers/tmdb/save', {**bound, 'command_id': ref(41), 'secret': '', 'storage': 'persistent'}),
            'test': form('/cabinet/journey/providers/tmdb/test', {**bound, 'command_id': ref(42), 'confirmed': 'yes'})})


def forbidden_processor(request):
    raise AssertionError('Request processor invoked')


class GuidedJourneyPresentationTests(SimpleTestCase):
    def render(self, value, **options):
        response = render_guided_journey(value, csrf_token='s'*64, **options)
        html = response.content.decode()
        return response, html, Document(html)

    def test_optional_favourites_preserve_fill_skip_back_and_native_mutation_fields(self):
        p = projection(); _, html, doc = self.render(p)
        self.assertIn('Any number from zero to five is fine', html)
        self.assertIn('their order is not a ranking', html)
        self.assertIn('Skip this step', html)
        self.assertIn('Skipping or going back keeps entries you have already saved', html)
        actions = [a for t, a in doc.tags if t == 'form']
        self.assertTrue(any(a['action'] == '/cabinet/journey/favourites/add' for a in actions))
        self.assertTrue(any(a['action'] == '/cabinet/journey/step' for a in actions))
        self.assertTrue(all(a['method'] == 'post' for a in actions))
        values = {i.get('name'): i.get('value') for i in doc.inputs}
        self.assertEqual(values['csrfmiddlewaretoken'], 's'*64)
        self.assertNotIn('experienced_ack', values)
        self.assertNotIn('signal', values)
        self.assertFalse(any(t == 'select' for t, a in doc.tags))

    def test_failed_provider_test_keeps_key_and_never_replays_or_echoes_secret(self):
        p = projection('providers'); p['providers'] = [provider(p)]; p.update(request=Poison(), secret='NEVER-ECHO')
        _, html, doc = self.render(p)
        self.assertIn('Connection check failed', html)
        self.assertIn('The saved key has not been removed', html)
        self.assertIn('moving between steps does not run another test', html)
        self.assertNotIn('NEVER-ECHO', html)
        secrets = [i for i in doc.inputs if i.get('name') == 'secret']
        self.assertEqual(len(secrets), 1); self.assertNotIn('value', secrets[0])
        confirms = [i for i in doc.inputs if i.get('name') == 'confirmed']
        self.assertTrue(confirms); self.assertTrue(all('checked' not in i for i in confirms))
        self.assertFalse(any(t == 'script' for t, a in doc.tags))
        self.assertTrue(any(t == 'form' and a['action'] == '/cabinet/journey/providers/tmdb/test' for t, a in doc.tags))

    def test_current_success_is_reused_but_expired_success_is_not_called_connected(self):
        p = projection('providers'); row = provider(p); row.update(test_state='passed', test_current=True); p['providers'] = [row]
        _, current, _ = self.render(p); self.assertIn('Connection check passed', current)
        row['test_current'] = False
        _, stale, _ = self.render(p); self.assertIn('Previous check passed; it is no longer current', stale)
        self.assertNotIn('Connection check passed', stale)

    def test_exact_prepare_label_does_not_claim_publication_and_all_categories_remain_visible(self):
        p = projection('prepare'); response, html, doc = self.render(p)
        self.assertIn('Prepare my taste funnels', html)
        self.assertIn('Preparation has not published new selections yet', html)
        self.assertEqual({a['data-category'] for t, a in doc.tags if 'data-category' in a}, set(CATEGORIES))
        self.assertEqual(response['Cache-Control'], 'no-store')
        p['categories'].pop()
        with self.assertRaises(ValueError): guided_journey_context(p)

    def test_successful_publication_requires_actual_reference_and_revision(self):
        p = projection('finish'); p['preparation'].update(state='published')
        with self.assertRaises(ValueError): self.render(p)
        p['preparation'].update(publication_ref=ref(50), publication_revision=1, partial=True)
        _, html, _ = self.render(p)
        self.assertIn('Your first selections are ready', html)
        self.assertIn('Source unavailable', html)

    def test_projection_routes_bindings_booleans_and_empty_secrets_fail_closed(self):
        for change in ('route', 'binding', 'boolean', 'secret', 'markup'):
            p = projection('providers'); p['providers'] = [provider(p)]
            if change == 'route': p['providers'][0]['actions']['save']['form_action'] = 'https://fictional.invalid/key'
            elif change == 'binding': p['providers'][0]['actions']['save']['fields']['journey_ref'] = ref(60)
            elif change == 'boolean': p['providers'][0]['credential_saved'] = 'true'
            elif change == 'secret': p['providers'][0]['actions']['save']['fields']['secret'] = 'DO-NOT-RETAIN'
            else: p['profile_label'] = mark_safe('<script>unsafe</script>')
            with self.subTest(change=change), self.assertRaises(ValueError): self.render(p)

    def test_owner_authored_favourite_is_escaped_and_not_converted_to_a_rating(self):
        p = projection(); slot = p['favourite_categories'][0]['slots'][0]
        slot.update(title='<Fictional & favourite>', removable=True, current_saved_revision=1)
        for form_ in slot['actions'].values(): form_['fields']['base_revision'] = 1
        slot['actions']['add']['fields']['title'] = slot['title']; slot['actions']['remove']['enabled'] = True
        _, html, doc = self.render(p)
        self.assertIn('&lt;Fictional &amp; favourite&gt;', html)
        self.assertFalse(any(t == 'Fictional' for t, a in doc.tags))
        self.assertTrue(any(t == 'form' and a['action'].endswith('/favourites/remove') for t, a in doc.tags))
        self.assertFalse(any(i.get('name') in ('enjoyment', 'experienced_ack', 'value') for i in doc.inputs))

    def test_renderer_has_no_request_context_or_backend_dependency(self):
        templates = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [Path(__file__).resolve().parents[3] / 'templates'], 'OPTIONS': {'context_processors': [__name__+'.forbidden_processor']}}]
        with override_settings(TEMPLATES=templates):
            response, _, _ = self.render(projection())
        self.assertEqual(response.status_code, 200)


    def test_existing_connection_test_and_preparation_pages_offer_a_normal_return_to_guide(self):
        from django.template.loader import render_to_string
        from shelf.onboarding_experience_views import render_setup
        html = render_setup({'state': 'model', 'csrf_token': 's'*64}).content.decode()
        self.assertIn('href="/setup/journey"', html)
        self.assertIn('opening the guide sends no request', html)
        native = render_to_string('cabinet/enhancement_status.html', {'portable_ui': True,
            'enhancement': {'schema': 'enhance/v1', 'stage': 'published', 'state': 'published'}, 'csrf_token': 's'*64})
        self.assertIn('href="/setup/journey"', native)
        legacy = render_to_string('cabinet/enhancement_status.html', {'enhancement': {}, 'csrf_token': 's'*64})
        self.assertNotIn('href="/setup/journey"', legacy)


    def test_category_and_preparation_links_are_fixed_native_routes_without_secret_queries(self):
        p = projection('finish')
        p['categories'][0]['href'] = '/cabinet?profile_ref=' + ref(1) + '&category=book'
        p['preparation']['href'] = '/enhance?run_ref=' + ref(70) + '&review=1'
        data = guided_journey_context(p)
        self.assertEqual(data['categories'][0]['href'], p['categories'][0]['href'])
        for href in ('//fictional.invalid/cabinet', 'https://fictional.invalid/cabinet',
                     '/cabinet?secret=never-in-a-link', '/setup\n',
                     '/enhance/runs/------------------------------------/status'):
            with self.subTest(href=href), self.assertRaises(ValueError):
                bad = deepcopy(p); bad['categories'][0]['href'] = href
                guided_journey_context(bad)
