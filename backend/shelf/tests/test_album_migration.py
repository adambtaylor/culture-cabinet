"""The additive schema preserves pre-album saved and submission contracts."""
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from shelf.models import AcquisitionJob, SavedItem
from shelf.tests.test_saved import SavedFixture


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class AlbumMigrationTests(SavedFixture, TransactionTestCase):
    def test_old_rows_survive_and_old_scope_constraints_remain(self):
        item = self.item()
        response = self.confirm(item, self.confirmation(item))
        self.assertEqual(response.status_code, 302)
        before_item = SavedItem.objects.values().get()
        before_job = AcquisitionJob.objects.values().get()
        executor = MigrationExecutor(connection)
        executor.migrate([('shelf', '0008_obtain_local_metadata')])
        try:
            self.assertEqual(SavedItem.objects.values().get(), before_item)
            self.assertEqual(AcquisitionJob.objects.values().get(), before_job)
        finally:
            restored = MigrationExecutor(connection)
            restored.migrate(restored.loader.graph.leaf_nodes())
        self.assertEqual(SavedItem.objects.values().get(), before_item)
        self.assertEqual(AcquisitionJob.objects.values().get(), before_job)
        for changes in ({'scope': 'album_flac'}, {'medium': 'tv', 'scope': 'series_full'},
                        {'provider': 'hardcover', 'medium': 'book', 'scope': 'ebook'}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                AcquisitionJob.objects.update(**changes)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedItem.objects.update(provider='album_request', medium='music')
