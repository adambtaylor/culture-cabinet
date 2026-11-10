"""Real escaping/native forms over fictional v2 representation, no authority DB."""
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from shelf.card_action_presentation import card_action_context, ACTIONS
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


class CardActionPresentationTests(SimpleTestCase):
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
