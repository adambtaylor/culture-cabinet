"""Real-template checks of permitted primitive facts, not history authority proof."""
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from shelf.taste_history_presentation import (render_taste_history, render_taste_history_compare,
    noise_control_context, noise_run_context)
from .test_guided_journey_presentation import projection, ref
from .test_onboarding_experience_views import Document


def point():
    return dict(checkpoint_ref=ref(100), profile_ref=ref(1), captured_at='2026-11-11T01:02:03+00:00',
                origin_label='Recorded favourite', current=True, complete=True,
                coverage_text='Supported item choices only.')


def action(kind, **extra):
    fields = dict(profile_ref=ref(1), command_id=ref(110))
    if kind == 'preview': fields.update(checkpoint_ref=ref(100), expected_state_digest='a'*64)
    elif kind == 'restore': fields.update(preview_ref=ref(101), preview_digest='b'*64, confirmed='restore_taste_choices')
    else: fields.update(restore_command_ref=ref(102), expected_state_digest='c'*64, confirmed='undo_taste_restore')
    fields.update(extra)
    return dict(form_action='/cabinet/taste/history/'+kind, fields=fields, enabled=True)


class TasteHistoryPresentationTests(SimpleTestCase):
    def compare(self, **extra):
        facts = dict(profile_ref=ref(1), checkpoint=point(), changes=[], csrf_token='s'*64)
        facts.update(extra)
        response = render_taste_history_compare(**facts)
        html = response.content.decode()
        return response, html, Document(html)

    def test_actual_dates_current_marker_and_empty_history_do_not_invent_checkpoints(self):
        response = render_taste_history(profile_ref=ref(1), checkpoints=[], csrf_token='s'*64)
        html = response.content.decode()
        self.assertIn('No earlier checkpoints are available yet', html)
        self.assertNotIn('<time', html)
        self.assertNotIn('<form', html)
        response = render_taste_history(profile_ref=ref(1), checkpoints=[point()],
            timezone_name='Australia/Melbourne', next_cursor='opaque+value', csrf_token='s'*64)
        html = response.content.decode()
        self.assertIn('12:02:03 AEDT (+1100)', html)
        self.assertIn('Current</strong>', html)
        self.assertIn('cursor=opaque%2Bvalue', html)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_compare_preserves_exact_note_and_absence_false_zero_original_meaning(self):
        changes = [dict(target_label='Fictional <title>', signal='note', historical_value='  Exact\n<&note>  ', current_value=None),
                   dict(target_label='Favourite', signal='saved', historical_value=False, current_value=True),
                   dict(target_label='Rating', signal='enjoyment', historical_value=0, current_value=4)]
        _, html, doc = self.compare(changes=changes, preview_form=action('preview'))
        self.assertIn('  Exact\n&lt;&amp;note&gt;  ', html)
        self.assertIn('Not set / cleared', html)
        self.assertIn('Not a favourite', html)
        self.assertIn('0 (original scale)', html)
        self.assertIn('Kept as they are now', html)
        self.assertIn('Location and Noise', html)
        posted = {i.get('name') for i in doc.inputs}
        self.assertEqual(posted, {'csrfmiddlewaretoken', 'profile_ref', 'checkpoint_ref', 'expected_state_digest', 'command_id'})
        self.assertNotIn('historical_value', posted)
        self.assertNotIn('actor_ref', posted)

    def test_complete_and_partial_restore_have_distinct_unchecked_confirmations(self):
        for complete, token in ((True, 'restore_taste_choices'), (False, 'restore_available_choices')):
            p = point(); p['complete'] = complete
            _, html, doc = self.compare(checkpoint=p, restore_form=action('restore', confirmed=token),
                result='preview', expires_at='2026-11-11T01:07:03Z')
            checks = [i for i in doc.inputs if i.get('name') == 'confirmed']
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]['type'], 'checkbox')
            self.assertEqual(checks[0]['value'], token)
            self.assertNotIn('checked', checks[0])
            self.assertIn('required', checks[0])
            self.assertIn('This review expires', html)
            self.assertIn('Use Enhance separately afterwards', html)
            if not complete: self.assertIn('Restore available choices only', html)
        p = point(); p['complete'] = False
        with self.assertRaises(ValueError):
            self.compare(checkpoint=p, restore_form=action('restore'), expires_at='2026-11-11T01:07:03Z')

    def test_conflict_expiry_unavailable_remove_stale_controls_and_undo_is_exact(self):
        for result in ('conflict', 'expired', 'unavailable'):
            _, html, doc = self.compare(result=result, preview_form=action('preview'))
            self.assertFalse(any(t == 'form' for t, a in doc.tags))
            if result == 'conflict': self.assertIn('Your taste changed; compare again', html)
        _, html, doc = self.compare(result='restored', undo_form=action('undo'))
        self.assertIn('Taste restored. Enhance to update recommendations.', html)
        self.assertIn('cannot undo later choices', html)
        self.assertTrue(any(i.get('value') == 'undo_taste_restore' for i in doc.inputs))
        self.assertFalse(any(t == 'script' for t, a in doc.tags))
        _, html, _ = self.compare(result='unavailable', changes=[dict(target_label='RESTRICTED-TITLE',
            signal='note', historical_value='RESTRICTED-NOTE', current_value=None)])
        self.assertNotIn('RESTRICTED', html)

    def test_profile_route_unsafe_text_and_missing_coverage_fail_closed(self):
        for change in ('profile', 'route', 'markup', 'timestamp', 'missing_value'):
            p = point(); form = action('preview'); changes = []
            if change == 'profile': form['fields']['profile_ref'] = ref(999)
            elif change == 'route': form['form_action'] = 'https://fictional.invalid/restore'
            elif change == 'markup': p['origin_label'] = mark_safe('<script>unsafe</script>')
            elif change == 'timestamp': p['captured_at'] = '2026-11-11T01:02:03'
            else: changes = [dict(target_label='Missing', signal='saved', current_value=False)]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.compare(checkpoint=p, preview_form=form, changes=changes)

    def test_history_renderer_needs_no_model_key_or_request_processors(self):
        def forbidden(request): raise AssertionError('Request processor invoked')
        templates = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [Path(__file__).resolve().parents[3] / 'templates'], 'OPTIONS': {'context_processors': [forbidden]}}]
        with override_settings(TEMPLATES=templates):
            response, html, _ = self.compare()
        self.assertEqual(response.status_code, 200)
        self.assertIn('view-only', html)

    def test_noise_is_a_native_bounded_start_field_below_button_not_a_network_control(self):
        from shelf.guided_journey_presentation import render_guided_journey
        control = noise_control_context(25)
        for value in (True, -1, 101, 25.0, '25', None):
            with self.subTest(value=value), self.assertRaises(ValueError): noise_control_context(value)
        p = projection('prepare'); p['next_noise'] = 25; p['actions']['prepare']['fields']['noise'] = 25
        html = render_guided_journey(p, csrf_token='s'*64).content.decode()
        self.assertLess(html.index('Prepare my taste funnels</button>'), html.index('id="prepare-noise"'))
        doc = Document(html); ranges = [i for i in doc.inputs if i.get('name') == 'noise']
        self.assertEqual(len(ranges), 1)
        self.assertEqual({k: ranges[0][k] for k in ('type','min','max','step','value')},
                         dict(type='range', min='0', max='100', step='1', value='25'))
        self.assertNotIn('oninput=', html)
        self.assertIn('Moving this control sends nothing', html)
        native = render_to_string('cabinet/enhancement_status.html', {'portable_ui': True,
            'portable_commands': {'start': '/enhance/start'}, 'noise_control': control,
            'profile_ref': ref(1), 'connection_ref': ref(2), 'client_key': ref(3), 'refresh_nonce': ref(4),
            'enhancement': {'schema': 'enhance/v1'}})
        self.assertLess(native.index('Start a round and review API requests</button>'), native.index('id="enhance-noise"'))
        self.assertIn('Experimental', native)
        p['actions']['prepare']['fields']['noise'] = 26
        with self.assertRaises(ValueError): render_guided_journey(p, csrf_token='s'*64)

    def test_frozen_noise_report_and_legacy_run_are_distinct_from_next_default(self):
        summary = noise_run_context(policy_version='selection-policy/v1', noise=100,
            categories=[dict(label='Books', requested=2, used=0, reason='No additional variety was available in the permitted pool.')])
        html = render_to_string('cabinet/enhancement_status.html', {'noise_run': summary,
            'enhancement': {'schema': 'enhance/v1'}})
        self.assertIn('Noise 100 · frozen for this round', html)
        self.assertIn('0 of 2 requested exploratory slots used', html)
        self.assertIn('No additional variety was available', html)
        self.assertIn('not newly learned preferences', html)
        legacy = noise_run_context(policy_version='legacy-ranked/v1', noise=None, categories=[])
        html = render_to_string('cabinet/enhancement_status.html', {'noise_run': legacy, 'enhancement': {}})
        self.assertIn('no recorded Noise setting', html)
        self.assertNotIn('Noise 25', html)
        with self.assertRaises(ValueError):
            noise_run_context(policy_version='legacy-ranked/v1', noise=25, categories=[])
        with self.assertRaises(ValueError):
            noise_run_context(policy_version='selection-policy/v1', noise=25,
                categories=[dict(label='Books', requested=2, used=0, reason='')])


class CabinetNoiseResultPresentationTests(SimpleTestCase):
    def render(self, *, run=None, exploratory=None):
        from .test_card_action_presentation import template_settings
        card = dict(title='Fictional source-grounded book', medium='book', candidate_key='fictional:book:1',
                    selection_exploratory=exploratory)
        with override_settings(TEMPLATES=template_settings()):
            return render_to_string('cabinet/portable_cabinet.html', dict(enhancement_base='test_card_base.html',
                published=True, profile_label='Fictional cabinet', profile_ref=ref(1), csrf_token='s'*64,
                connection_ref=ref(2), client_key=ref(3), refresh_nonce=ref(4),
                portable_commands={'start': '/enhance/start'}, noise_control=noise_control_context(25),
                noise_run=run, categories=[dict(category='book', label='Books', visible=True, cards=[card])]))

    def test_actual_run_report_is_frozen_separately_from_next_round_slider_default(self):
        run = noise_run_context(policy_version='selection-policy/v1', noise=100,
            categories=[dict(label='Books', requested=2, used=1, reason='No additional variety was available.')])
        before = deepcopy(run)
        html = self.render(run=run)
        self.assertIn('Noise 100 · frozen for this round', html)
        self.assertIn('Books: 1 of 2 requested exploratory slots used.', html)
        self.assertIn('No additional variety was available.', html)
        self.assertIn('Editing the next-round control does not change this round', html)
        self.assertIn('Starting default: 25.', html)
        self.assertIn('not newly learned preferences', html)
        doc = Document(html)
        self.assertEqual([i['value'] for i in doc.inputs if i.get('name') == 'noise'], ['25'])
        self.assertEqual(run, before)

    def test_legacy_and_missing_report_do_not_invent_recorded_noise_from_default_25(self):
        legacy = noise_run_context(policy_version='legacy-ranked/v1', noise=None, categories=[])
        html = self.render(run=legacy)
        self.assertIn('This older round has no recorded Noise setting', html)
        self.assertNotIn('Noise 25 · frozen', html)
        self.assertNotIn('Noise None', html)
        self.assertIn('Starting default: 25.', html)
        self.assertNotIn('data-noise-run', self.render())

    def test_exploratory_label_requires_actual_true_boolean_and_adds_no_form_or_taste_signal(self):
        baseline = self.render(exploratory=False)
        baseline_forms = sum(tag == 'form' for tag, attrs in Document(baseline).tags)
        html = self.render(exploratory=True)
        self.assertIn('data-selection-exploratory', html)
        self.assertIn('An exploratory pick from available source evidence.', html)
        self.assertEqual(sum(tag == 'form' for tag, attrs in Document(html).tags), baseline_forms)
        self.assertNotIn('name="score"', html)
        self.assertNotIn('name="selection_exploratory"', html)
        for value in (False, None, 1, 'true', {}, []):
            with self.subTest(value=value):
                self.assertNotIn('data-selection-exploratory', self.render(exploratory=value))

    def test_report_source_text_is_escaped_and_zero_used_does_not_mark_a_card(self):
        run = noise_run_context(policy_version='selection-policy/v1', noise=100,
            categories=[dict(label='Books <fixture>', requested=2, used=0, reason='No <additional> variety & no automatic retry.')])
        html = self.render(run=run, exploratory=False)
        self.assertIn('Books &lt;fixture&gt;: 0 of 2', html)
        self.assertIn('No &lt;additional&gt; variety &amp; no automatic retry.', html)
        self.assertNotIn('<additional>', html)
        self.assertNotIn('data-selection-exploratory', html)
