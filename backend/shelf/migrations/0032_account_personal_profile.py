from importlib import import_module

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


# SQLite rebuilds Profile for these constraints. Restore the existing portable
# trigger verbatim; it must still permit only installation-owned owner profiles.
BINDING = next(sql for sql in import_module(
    'shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    if 'CREATE TRIGGER cabinet_binding_insert ' in sql)


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
    ]
