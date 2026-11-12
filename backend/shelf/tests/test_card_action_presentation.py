"""Real escaping/native forms over fictional v2 representation, no authority DB."""
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from shelf.card_action_presentation import card_action_context, card_action_refresh_context, local_event_display_context, render_card_action_refusal, ACTIONS
from .test_onboarding_experience_views import Document, Poison

PROFILE = '00000000-0000-4000-8000-000000000001'
PUBLICATION = '00000000-0000-4000-8000-000000000002'
TARGET = '00000000-0000-4000-8000-000000000003'
COMMAND = '00000000-0000-4000-8000-000000000010'
TOKEN = 's' * 64


def projection():
    fields = dict(profile_ref=PROFILE, publication_ref=PUBLICATION, publication_revision=1,
                  target_ref=TARGET, interest_revision=0, saved_revision=0, command_id=COMMAND)
    return dict(schema='native-card-actions/v1', **{k: v for k, v in fields.items() if k != 'command_id'},
        selected_state='none', ranking_state='fresh', last_transition_ref=None, undo=None, receipt_href=None,
        actions=[dict(action=key, label=label, direction=direction, enabled=True, disabled_reason='',
                      form_action='/cabinet/actions/', fields={**fields, 'action': key},
                      obtain_state='unavailable' if key == 'obtain' else 'not_requested')
                 for key, (direction, label, icon) in ACTIONS.items()])


def template_settings():
    return [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
             'DIRS': [Path(__file__).resolve().parents[3] / 'templates'],
             'OPTIONS': {'loaders': [('django.template.loaders.locmem.Loader', {
                 'test_card_base.html': '<!doctype html><html><body>{% block content %}{% endblock %}</body></html>'}),
                 'django.template.loaders.filesystem.Loader']}}]


LOCAL_KEY = 'fictional:gallery:source-entry'


def local_projection():
    """Actual immutable interfaces-a1-display-02 shape, with fictional facts."""
    return dict(schema='local-event-display/v1', candidate_key=LOCAL_KEY, profile_ref=PROFILE,
        publication_ref=PUBLICATION, publication_revision=3, dataset_revision='a'*64,
        location_revision=2, selection_context_digest='b'*64,
        distance_label='Approximately 12 km from postcode reference', distance_basis='postcode',
        radius_band='outside_radius', source_refs=['fact:opening', 'fact:artist'],
        opening_status='current_exhibition', opening_starts_at='2026-11-01T18:00:00+11:00',
        opening_ends_at=None, exhibition_starts_on='2026-11-02', exhibition_ends_on='2026-11-30',
        date_precision='date', timezone='Australia/Melbourne', artist_match_status='name_match_unconfirmed',
        artist_match_label='Artist name matches your stated favourite; identity unconfirmed.')


class LocalEventDisplayPresentationTests(SimpleTestCase):
    def normalize(self, value, **extra):
        arguments = dict(candidate_key=LOCAL_KEY, category='gallery', profile_ref=PROFILE,
                         publication_ref=PUBLICATION, publication_revision=3)
        arguments.update(extra)
        return local_event_display_context(value, **arguments)

    def render(self, value, *, normalize=True):
        local = self.normalize(value) if normalize else value
        card = dict(title='Fictional exhibition', candidate_key=LOCAL_KEY, medium='gallery', local_event_display=local)
        with override_settings(TEMPLATES=template_settings()):
            return render_to_string('cabinet/portable_cabinet.html', dict(enhancement_base='test_card_base.html',
                published=True, profile_label='Fictional cabinet', profile_ref=PROFILE, csrf_token=TOKEN,
                card_actions_available=True, categories=[dict(category='gallery', label='Exhibitions & openings',
                    visible=True, cards=[card])]))

    def test_supplied_approximate_distance_and_local_bindings_are_preserved_without_calculation(self):
        p = local_projection(); p.update(latitude=99, longitude=99, request=Poison())
        result = self.normalize(p)
        for key in ('candidate_key', 'profile_ref', 'publication_ref', 'publication_revision',
                    'dataset_revision', 'location_revision', 'selection_context_digest', 'source_refs', 'distance_label'):
            self.assertEqual(result[key], p[key])
        self.assertEqual(result['radius_band_text'], 'Beyond your preferred radius; still shown.')
        self.assertNotIn('latitude', result)
        self.assertNotIn('longitude', result)
        self.assertNotIn('request', result)

    def test_foreign_or_stale_card_bindings_nonlocal_categories_and_invalid_states_refuse(self):
        for key, value in (('candidate_key', 'foreign'), ('profile_ref', TARGET), ('publication_ref', TARGET),
                           ('publication_revision', 2), ('location_revision', True),
                           ('selection_context_digest', 'not-a-digest'), ('artist_match_status', 'verified_artist_relation')):
            p = local_projection(); p[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.normalize(p)
        with self.assertRaises(ValueError): self.normalize(local_projection(), category='movie')

    def test_past_opening_and_current_run_stay_separate_with_unconfirmed_artist_wording(self):
        html = self.render(local_projection())
        self.assertIn('Source dates indicate a current exhibition run', html)
        self.assertIn('Opening starts</dt><dd>2026-11-01T18:00:00+11:00', html)
        self.assertIn('Exhibition run starts</dt><dd>2026-11-02', html)
        self.assertIn('Opening ends</dt><dd>Not stated', html)
        self.assertIn('identity unconfirmed', html)
        self.assertIn('not verified identity or an exhibition appearance', html)
        self.assertIn('Beyond your preferred radius; still shown', html)

    def test_unknown_distance_and_missing_dates_never_become_zero_or_filled_opening(self):
        p = local_projection()
        p.update(distance_label='Distance unknown.', distance_basis='unknown', radius_band='unknown',
                 opening_status='unknown', artist_match_status='none', artist_match_label='DO-NOT-SHOW-NAME')
        for key in ('opening_starts_at', 'opening_ends_at', 'exhibition_starts_on', 'exhibition_ends_on', 'date_precision', 'timezone'):
            p[key] = None
        html = self.render(p)
        self.assertIn('Distance unknown.', html)
        self.assertNotIn('0 km', html)
        self.assertNotIn('DO-NOT-SHOW-NAME', html)
        self.assertIn('Opening starts</dt><dd>Not stated', html)
        self.assertIn('Exhibition run starts</dt><dd>Not stated', html)
        self.assertIn('Radius comparison unavailable', html)

    def test_raw_overlay_is_not_rendered_and_unsafe_text_cannot_bypass_presenter(self):
        p = local_projection()
        self.assertNotIn('Approximately 12 km', self.render(p, normalize=False))
        p['distance_label'] = mark_safe('<script>unsafe</script>')
        with self.assertRaises(ValueError): self.normalize(p)
        p = local_projection(); p['artist_match_label'] = '<Fictional & unconfirmed>'
        self.assertIn('&lt;Fictional &amp; unconfirmed&gt;', self.render(p))


class CardActionPresentationTests(SimpleTestCase):
    def test_refresh_keeps_owned_history_separate_from_current_publication_and_projects_only_primitives(self):
        receipt = dict(command_id=COMMAND, target_ref=TARGET, requested_action='like',
            preference_outcome='committed', selected_state='like', interest_revision=1,
            saved_revision=0, ranking_state='feedback_pending', secret=Poison(), title=Poison())
        current = dict(published=True, publication_ref=PUBLICATION, publication_revision=2)
        result = card_action_refresh_context(profile_ref=PROFILE, interaction=current,
            receipt=receipt, command_id=COMMAND)
        self.assertEqual(result['publication_revision'],2)
        self.assertEqual(result['read_state'],'current')
        self.assertEqual(set(result['receipt']),{'command_id','target_ref','requested_action',
            'preference_outcome','selected_state','interest_revision','saved_revision','ranking_state'})
        unavailable = card_action_refresh_context(profile_ref=PROFILE, interaction=current,
            receipt=receipt, command_id=COMMAND, read_available=False)
        self.assertEqual(unavailable['receipt'],result['receipt'])
        self.assertEqual(unavailable['read_state'],'unavailable')
        self.assertIsNone(unavailable['publication_ref'])
        self.assertIsNone(unavailable['publication_revision'])

    def test_refresh_rejects_forged_command_unsafe_revisions_and_unknown_outcomes(self):
        receipt = dict(command_id=COMMAND,target_ref=TARGET,requested_action='like',
            preference_outcome='committed',selected_state='like',interest_revision=1,
            saved_revision=0,ranking_state='feedback_pending')
        current = dict(published=True,publication_ref=PUBLICATION,publication_revision=1)
        for key,bad in (('command_id',TARGET),('target_ref','bad'),('requested_action','injected'),
                ('preference_outcome','pending'),('selected_state','obtain'),('ranking_state','stale'),
                ('interest_revision',True),('saved_revision',2**53)):
            with self.subTest(key=key),self.assertRaises(ValueError):
                card_action_refresh_context(profile_ref=PROFILE,interaction=current,
                    receipt={**receipt,key:bad},command_id=COMMAND)
        with self.assertRaises(ValueError):
            card_action_refresh_context(profile_ref=PROFILE,interaction=current,command_id=COMMAND)
        for changed in ({**current,'publication_ref':'ABCDEFAB-0000-4000-8000-000000000001'},
                        {**current,'publication_revision':True}):
            with self.assertRaises(ValueError):
                card_action_refresh_context(profile_ref=PROFILE,interaction=changed)

    def render(self, value=None, **extra):
        context = card_action_context(value or projection())
        card = dict(title='A fictional <book>', card_actions=context, reasons=[{'label': 'Previous association', 'text': 'OLD-PERSONAL-EXPLANATION', 'evidence_count': 1}], feedback={'signals': [
            {'signal': 'enjoyment', 'value': 4}, {'signal': 'note', 'value': 'Keep <this> exact.'}]})
        with override_settings(TEMPLATES=template_settings()):
            html = render_to_string('cabinet/portable_cabinet.html', dict(enhancement_base='test_card_base.html',
                published=True, profile_label='Fictional cabinet', profile_ref=PROFILE, csrf_token=TOKEN,
                card_actions_available=True, feedback_available=True,
                categories=[{'label': 'Books', 'visible': True, 'cards': [card]}], **extra))
        return html, Document(html), context

    def test_one_native_four_button_form_has_current_csrf_and_original_shared_command(self):
        html, doc, context = self.render()
        form = next(attrs for tag, attrs in doc.tags if tag == 'form' and 'data-card-action-form' in attrs)
        self.assertEqual(form['action'], '/cabinet/actions/')
        self.assertEqual(form['method'], 'post')
        values = {i['name']: i.get('value') for i in doc.inputs}
        self.assertEqual(values['command_id'], COMMAND)
        self.assertEqual(context['pending_receipt_href'], '/cabinet/actions/' + COMMAND + '/')
        self.assertIn('check this action’s recorded receipt', html)
        self.assertEqual(values['csrfmiddlewaretoken'], TOKEN)
        self.assertEqual(values['target_ref'], TARGET)
        buttons = [a for t, a in doc.tags if t == 'button' and a.get('name') == 'action']
        self.assertEqual([b['value'] for b in buttons], ['like', 'dislike', 'favourite', 'obtain'])
        self.assertEqual([b['data-direction'] for b in buttons], ['right', 'left', 'up', 'down'])
        self.assertNotIn('experienced_ack', values)
        self.assertFalse(any(t == 'select' for t, a in doc.tags))
        self.assertIn('Experienced enjoyment: 4 / 5', html)
        self.assertIn('Keep &lt;this&gt; exact.', html)
        self.assertIn('No purchase or download starts here', html)
        self.assertNotIn('data-card-feedback', html)

    def test_selected_favourite_is_not_an_extra_obtain_score_or_optimistic_like(self):
        value = projection(); value.update(selected_state='favourite', ranking_state='feedback_pending')
        html, doc, context = self.render(value)
        pressed = [a['value'] for t, a in doc.tags if t == 'button' and a.get('name') == 'action' and a.get('aria-pressed') == 'true']
        self.assertEqual(pressed, ['favourite'])
        self.assertIn('Feedback saved; Enhance to update recommendations.', html)
        self.assertNotIn('OLD-PERSONAL-EXPLANATION', html)
        self.assertNotIn('weight', context)
        self.assertNotIn('score', context)

    def test_native_cabinet_enhance_places_one_noise_control_in_current_post_form(self):
        from shelf.taste_history_presentation import noise_control_context
        for value in (0, 25, 100):
            html, doc, _ = self.render(portable_commands={'start': '/enhance/start'},
                connection_ref=TARGET, client_key=COMMAND, refresh_nonce=PUBLICATION,
                enhance_url='/enhance?profile_ref='+PROFILE, noise_control=noise_control_context(value))
            native = [a for t, a in doc.tags if t == 'form' and 'data-native-enhance-start' in a]
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0]['action'], '/enhance/start')
            self.assertEqual(native[0]['method'], 'post')
            start = html.index('<form method="post" action="/enhance/start"')
            end = html.index('</form>', start)
            form_html = html[start:end]
            form_doc = Document(form_html)
            self.assertEqual(len([i for i in form_doc.inputs if i.get('name') == 'noise']), 1)
            fields = {i.get('name'): i.get('value') for i in form_doc.inputs}
            self.assertEqual(fields, dict(csrfmiddlewaretoken=TOKEN, profile_ref=PROFILE,
                connection_ref=TARGET, client_key=COMMAND, refresh_nonce=PUBLICATION, noise=str(value)))
            self.assertNotIn('<form', form_html[len('<form'):])
            self.assertIn('<button type="submit">Enhance</button>\n  <fieldset', form_html)
            self.assertIn('Precise', form_html)
            self.assertIn('Experimental', form_html)
            self.assertNotIn('Enhance this cabinet</a>', html)
            self.assertEqual(html.count('src="/cabinet/actions/script.js"'), 1)

    def test_cabinet_start_never_overrides_resume_missing_context_or_fixed_action(self):
        from shelf.taste_history_presentation import noise_control_context
        ready = dict(portable_commands={'start': '/enhance/start'}, connection_ref=TARGET,
            client_key=COMMAND, refresh_nonce=PUBLICATION, enhance_url='/enhance?profile_ref='+PROFILE,
            noise_control=noise_control_context(25))
        for change in ('unresolved', 'missing', 'foreign_action', 'no_default'):
            context = deepcopy(ready)
            if change == 'unresolved': context.update(round_blocks_start=True, round_url='/enhance?run_ref='+COMMAND)
            elif change == 'missing': context.pop('client_key')
            elif change == 'foreign_action': context['portable_commands']['start'] = 'https://fictional.invalid/start'
            else: context.pop('noise_control')
            html, doc, _ = self.render(**context)
            self.assertFalse(any(t == 'form' and 'data-native-enhance-start' in a for t, a in doc.tags))
            self.assertNotIn('name="noise"', html)
            if change == 'unresolved': self.assertIn('Continue the recorded round', html)
            else: self.assertIn('Review Enhance setup and requests', html)

    def test_tampered_direction_route_identity_or_command_shape_is_refused(self):
        for change in ('direction', 'route', 'profile', 'command', 'revision', 'fields'):
            value = projection(); row = value['actions'][1]
            if change == 'direction': row['direction'] = 'down'
            elif change == 'route': row['form_action'] = 'https://fictional.invalid/collect'
            elif change == 'profile': row['fields']['profile_ref'] = TARGET
            elif change == 'command': row['fields']['command_id'] = TARGET
            elif change == 'revision': row['fields']['saved_revision'] = True
            else: row['fields']['secret'] = 'not-allowed'
            with self.subTest(change=change), self.assertRaises(ValueError):
                card_action_context(value)

    def test_opaque_context_and_pre_marked_html_cannot_enter_output(self):
        value = projection(); value.update(request=Poison(), authority=Poison(), secret='DO-NOT-RENDER')
        html, doc, context = self.render(value)
        self.assertNotIn('DO-NOT-RENDER', html)
        self.assertIn('A fictional &lt;book&gt;', html)
        bad = deepcopy(value); bad['actions'][0]['disabled_reason'] = mark_safe('<script>bad</script>')
        with self.assertRaises(ValueError): card_action_context(bad)

    def test_new_action_assets_and_history_do_not_enable_error_or_legacy_actions(self):
        html, _, _ = self.render()
        self.assertIn('/cabinet/actions/script.js', html)
        self.assertIn('/cabinet/actions/style.css', html)
        error, _, _ = self.render(read_code='AUTHORITY_DENIED')
        self.assertNotIn('/cabinet/actions/script.js', error)
        self.assertNotIn('/cabinet/actions/style.css', error)

    def test_disabled_target_and_receipt_are_safe_native_fallbacks(self):
        value = projection(); value['actions'][0].update(enabled=False, disabled_reason='This card is no longer available.')
        value['receipt_href'] = '/cabinet/actions/' + COMMAND + '/'
        html, doc, context = self.render(value)
        like = next(a for t, a in doc.tags if t == 'button' and a.get('value') == 'like')
        self.assertIn('disabled', like)
        self.assertIn('Check the recorded outcome', html)
        value['receipt_href'] = '//fictional.invalid/receipt'
        with self.assertRaises(ValueError): card_action_context(value)

    def test_undo_keeps_acquisition_separate_and_cannot_target_another_profile(self):
        value = projection(); value['undo'] = {'form_action': '/cabinet/actions/undo/', 'enabled': True, 'fields': {
            'profile_ref': PROFILE, 'original_command_id': COMMAND, 'command_id': '00000000-0000-4000-8000-000000000011',
            'interest_revision': 1, 'saved_revision': 0}}
        html, doc, context = self.render(value)
        self.assertIn('Preference Undo does not cancel an Obtain action.', html)
        self.assertTrue(any(t == 'form' and a.get('action') == '/cabinet/actions/undo/' for t, a in doc.tags))
        value['undo']['fields']['profile_ref'] = TARGET
        with self.assertRaises(ValueError): card_action_context(value)


class CardActionRefusalPresentationTests(SimpleTestCase):
    def render_refusal(self, **facts):
        with override_settings(TEMPLATES=template_settings()):
            return render_card_action_refusal(**facts)

    def test_response_and_undo_keep_uncertain_outcomes_action_specific(self):
        # The same status may cover pre-effect denial or an existing command;
        # neither response is entitled to promise that nothing was saved.
        for operation, phrase in [('response', 'Your card response is not confirmed.'),
                                  ('undo', 'Undo is not confirmed.')]:
            for code, status in [('UNAUTHENTICATED', 401), ('AUTHORITY_DENIED', 403),
                                 ('IDEMPOTENCY_CONFLICT', 409),
                                 ('UNKNOWN_RECONCILIATION_REQUIRED', 503)]:
                with self.subTest(operation=operation, code=code):
                    response = self.render_refusal(operation=operation, code=code, status=status)
                    html = response.content.decode()
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response['Cache-Control'], 'no-store')
                    self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                    self.assertIn(phrase, html)
                    self.assertIn('Nothing is retried automatically.', html)
                    self.assertIn('explicitly choose the action you intend', html)
                    for absent in ('not saved', 'Enhance progress', 'existing round',
                                   'This round is blocked', code):
                        self.assertNotIn(absent, html)

    def test_recovery_has_fixed_get_links_no_forms_or_request_context(self):
        # A context processor would expose request facts; no request is passed
        # into rendering and this deliberately invalid processor must not run.
        settings = template_settings()
        settings[0]['OPTIONS']['context_processors'] = ['not_a_real_module.must_not_run']
        with override_settings(TEMPLATES=settings):
            response = render_card_action_refusal(operation='response', code='UNAUTHENTICATED', status=401)
        html = response.content.decode(); doc = Document(html)
        self.assertEqual([a['href'] for t, a in doc.tags if t == 'a'],
                         ['#main', '/setup', '/setup/cabinet'])
        self.assertFalse(any(t in ('form', 'input', 'button', 'script') for t, a in doc.tags))
        self.assertIn('href="/enhance/style.css"', html)
        self.assertNotIn('csrf', html.lower())
        with self.assertRaises(TypeError):
            render_card_action_refusal(operation='response', code='UNAUTHENTICATED', status=401, request=Poison())

    def test_untrusted_or_nonprimitive_refusal_facts_fail_closed(self):
        for key, bad in [('operation', 'like'), ('operation', Poison()),
                         ('operation', mark_safe('response')),
                         ('code', '<script>private</script>'), ('code', Poison()),
                         ('code', mark_safe('UNAUTHENTICATED')), ('status', True),
                         ('status', '401'), ('status', 200), ('status', 600)]:
            facts = dict(operation='response', code='UNAUTHENTICATED', status=401)
            facts[key] = bad
            with self.subTest(key=key, value_type=type(bad).__name__), self.assertRaises(ValueError):
                self.render_refusal(**facts)
