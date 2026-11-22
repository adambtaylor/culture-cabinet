"""Rendered provider controls follow current household acquisition authority."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from shelf import durable_intake
from shelf.models import Grant, Profile


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ProviderCatalogueAccessUITests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='synthetic-access-owner')
        self.owner = Profile.objects.create(pk=1, kind='owner', active=True, label='Owner')
        Grant.objects.create(user=self.actor, profile=self.owner, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        self.client.post('/profiles/select/', {'profile_id': '1'})

    def personal(self):
        profile = Profile.objects.create(kind='personal', owner=self.actor,
            active=True, label='Private')
        Grant.objects.create(user=self.actor, profile=profile, can_view=True,
            can_select=True, can_mutate=True)
        self.client.post('/profiles/select/', {'profile_id': str(profile.pk)})
        return profile

    def test_fresh_personal_catalogue_hides_acquisition_and_owner_keeps_controls(self):
        owner_page = self.client.get('/profiles/1/')
        self.assertContains(owner_page, 'Start discovery')
        self.assertContains(owner_page, 'Fetch graphic novels')
        self.assertContains(owner_page, 'Search catalogue')
        profile = self.personal()
        page = self.client.get(f'/profiles/{profile.pk}/')
        for label in ('Start discovery', 'Fetch graphic novels', 'Search catalogue',
                      'Explore TMDB neighbors'):
            self.assertNotContains(page, label)
        self.assertContains(page, 'Provider search, browse, discovery and neighbor acquisition require')
        self.assertContains(page, 'Continue with durable intake')

    def test_active_personal_run_keeps_history_controls_and_hides_refresh(self):
        profile = self.personal()
        run = durable_intake.new_run(self.actor, profile.pk)
        page = self.client.get(f'/profiles/{profile.pk}/')
        self.assertContains(page, 'Continue current run')
        self.assertContains(page, 'Export human history (JSON)')
        for label in ('Refresh source metadata', 'Start discovery', 'Fetch graphic novels',
                      'Search catalogue', 'Explore TMDB neighbors'):
            self.assertNotContains(page, label)
        self.assertEqual(page.context['run_id'], str(run.pk))
