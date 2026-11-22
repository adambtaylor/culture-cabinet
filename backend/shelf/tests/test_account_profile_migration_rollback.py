"""Downgrade policy for account-owned profiles."""
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from shelf.models import Grant, Profile


class AccountProfileMigrationRollbackTests(TransactionTestCase):
    def test_downgrade_succeeds_when_no_personal_profiles_exist(self):
        old = [('shelf', '0031_beta_problem_reports')]
        current = [('shelf', '0032_account_personal_profile')]
        MigrationExecutor(connection).migrate(current)

        def restore_current_migration_state():
            MigrationExecutor(connection).migrate(current)

        self.addCleanup(restore_current_migration_state)
        apps = MigrationExecutor(connection).loader.project_state(current).apps
        user = apps.get_model('auth', 'User').objects.create(username='rollback-legacy-profile')
        profile = apps.get_model('shelf', 'Profile').objects.create(kind='owner',
            label='Legacy household', active=True)
        grant = apps.get_model('shelf', 'Grant').objects.create(user_id=user.pk,
            profile_id=profile.pk, can_view=True, can_select=True, can_mutate=True)

        MigrationExecutor(connection).migrate(old)

        old_apps = MigrationExecutor(connection).loader.project_state(old).apps
        retained = old_apps.get_model('shelf', 'Profile').objects.get(pk=profile.pk)
        stored_grant = old_apps.get_model('shelf', 'Grant').objects.get(pk=grant.pk)
        self.assertEqual((retained.kind, retained.label, retained.active),
                         ('owner', 'Legacy household', True))
        self.assertEqual((stored_grant.user_id, stored_grant.profile_id,
                          stored_grant.can_view, stored_grant.can_select, stored_grant.can_mutate),
                         (user.pk, profile.pk, True, True, True))
        with connection.cursor() as cursor:
            columns = {column.name for column in
                       connection.introspection.get_table_description(cursor, 'shelf_profile')}
        self.assertNotIn('owner_id', columns)
        self.assertNotIn(('shelf', '0032_account_personal_profile'),
                         MigrationRecorder(connection).applied_migrations())

    def test_downgrade_refuses_before_schema_or_personal_data_changes(self):
        old = [('shelf', '0031_beta_problem_reports')]
        current = [('shelf', '0032_account_personal_profile')]
        MigrationExecutor(connection).migrate(current)

        def restore_current_migration_state():
            MigrationExecutor(connection).migrate(current)

        self.addCleanup(restore_current_migration_state)
        apps = MigrationExecutor(connection).loader.project_state(current).apps
        user = apps.get_model('auth', 'User').objects.create(username='rollback-personal-profile')
        profile = apps.get_model('shelf', 'Profile').objects.create(owner_id=user.pk,
            kind='personal', label='Private profile', active=True)
        grant = apps.get_model('shelf', 'Grant').objects.create(user_id=user.pk,
            profile_id=profile.pk, can_view=True, can_select=True, can_mutate=True)

        with self.assertRaisesRegex(IrreversibleError, 'personal profiles exist; they may contain user data'):
            MigrationExecutor(connection).migrate(old)

        retained = Profile.objects.get(pk=profile.pk)
        self.assertEqual((retained.owner_id, retained.kind, retained.label, retained.active),
                         (user.pk, Profile.Kind.PERSONAL, 'Private profile', True))
        stored_grant = Grant.objects.get(pk=grant.pk)
        self.assertEqual((stored_grant.user_id, stored_grant.profile_id,
                          stored_grant.can_view, stored_grant.can_select, stored_grant.can_mutate),
                         (user.pk, profile.pk, True, True, True))
        with connection.cursor() as cursor:
            columns = {column.name for column in
                       connection.introspection.get_table_description(cursor, Profile._meta.db_table)}
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='cabinet_binding_insert'")
            trigger = cursor.fetchone()
        self.assertIn('owner_id', columns)
        self.assertIsNotNone(trigger)
        self.assertIn(('shelf', '0032_account_personal_profile'),
                      MigrationRecorder(connection).applied_migrations())
