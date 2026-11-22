from importlib import import_module

from django.conf import settings
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
import django.db.models.deletion


# SQLite rebuilds Profile for these constraints. Restore the existing portable
# trigger verbatim; it must still permit only installation-owned owner profiles.
BINDING = next(sql for sql in import_module(
    'shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    if 'CREATE TRIGGER cabinet_binding_insert ' in sql)


def guard_personal_profile_downgrade(apps, schema_editor):
    """Permit rollback only while no account-owned profiles need preservation.

    Operational recovery is to restore a pre-0032 backup, or ship a forward
    corrective migration that preserves personal profiles. Never delete or
    relabel those rows merely to make this migration reversible.
    """
    Profile = apps.get_model('shelf', 'Profile')
    if Profile.objects.using(schema_editor.connection.alias).filter(kind='personal').exists():
        raise IrreversibleError(
            'Cannot reverse shelf.0032_account_personal_profile while account-owned personal profiles '
            'exist; they may contain user data. Restore a pre-0032 backup or deploy a forward corrective '
            'migration; do not delete or relabel personal profiles.')


class Migration(migrations.Migration):
    dependencies = [('shelf', '0031_beta_problem_reports'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.RunSQL('DROP TRIGGER cabinet_binding_insert', BINDING),
        migrations.RemoveConstraint('profile', 'profile_kind_allowed'),
        migrations.AlterField('profile', 'kind', models.CharField(max_length=8, choices=[
            ('owner', 'Owner'), ('partner', 'Partner'), ('family', 'Family'), ('personal', 'Personal')])),
        migrations.AddField('profile', 'owner', models.OneToOneField(null=True,
            on_delete=django.db.models.deletion.PROTECT, related_name='personal_profile',
            to=settings.AUTH_USER_MODEL)),
        migrations.AddConstraint('profile', models.CheckConstraint(
            condition=(models.Q(kind__in=['owner', 'partner', 'family'], owner__isnull=True)
                       | models.Q(kind='personal', owner__isnull=False)), name='profile_kind_allowed')),
        migrations.AddConstraint('profile', models.UniqueConstraint(fields=['kind'],
            condition=models.Q(owner__isnull=True), name='legacy_profile_kind_unique')),
        migrations.RunSQL(BINDING, 'DROP TRIGGER cabinet_binding_insert'),
        # Keep this last: reverse migration executes it first, before any schema
        # operation can remove the owner column or reject personal profile rows.
        migrations.RunPython(migrations.RunPython.noop, guard_personal_profile_downgrade),
    ]
