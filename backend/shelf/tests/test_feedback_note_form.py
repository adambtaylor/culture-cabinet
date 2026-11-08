"""The additive note field preserves text without changing legacy constraint clearing."""
from urllib.parse import urlencode
import uuid

from django.http import QueryDict
from django.test import SimpleTestCase

from shelf.feedback import mutate
from shelf.forms import FeedbackForm, forms_for
from shelf.models import FeedbackProjection
from .synthetic_feedback import FeedbackCase, TARGET_IDS


class NoteFormTests(SimpleTestCase):
    def form(self, signal, text):
        fields = dict(mutation_id=str(uuid.uuid4()), target_type='item', scope='item',
            signal=signal, operation='set', base_revision='0', text=text)
        return FeedbackForm(QueryDict(urlencode(fields)), target_id=str(uuid.uuid4()))

    def test_note_exact_text_empty_note_and_constraint_clear_are_distinct(self):
        for text in ('  e\u0301 / é\t\r\n雪  ', ''):
            note = self.form('note', text)
            self.assertTrue(note.is_valid(), note.errors)
            self.assertEqual(note.command.value, {'text': text})
        constraint = self.form('constraint', '')
        self.assertTrue(constraint.is_valid(), constraint.errors)
        self.assertIsNone(constraint.command.value)

    def test_note_initialization_has_exact_unstripped_text_field(self):
        text = '  Original\n雪  '
        note = FeedbackForm(target_id=str(uuid.uuid4()), initial={'signal': 'note', 'operation': 'set', 'text': text})
        self.assertFalse(note.fields['text'].strip)
        self.assertEqual(note['text'].value(), text)


class NoteFormIntegrationTests(FeedbackCase):
    def test_forms_for_initializes_saved_note_and_retains_authored_undo(self):
        from shelf.models import FeedbackTarget
        text = '  Saved exact note\n雪  '
        result = mutate(actor=self.actor, profile_id=self.profile.pk, command=self.command('note', {'text': text}))
        target = FeedbackTarget.objects.get(pk=TARGET_IDS[0])
        rows = {row.signal: row for row in FeedbackProjection.objects.filter(profile=self.profile, target=target)}
        forms, undo = forms_for(self.profile, target, rows)
        note = next(form for form in forms if form.initial['signal'] == 'note')
        self.assertEqual(note['text'].value(), text)
        self.assertTrue(any(str(form.initial['reverses_id']) == result.event_id for form in undo))
