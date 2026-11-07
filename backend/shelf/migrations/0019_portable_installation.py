from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('shelf', '0018_authored_discovery_state'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='PortableInstallation', fields=[
            ('id', models.UUIDField(primary_key=True, serialize=False)),
            ('data_schema_version', models.PositiveIntegerField(default=1)),
            ('root_identity', models.CharField(max_length=64, unique=True)),
            ('owner_revision', models.PositiveBigIntegerField(default=0)),
            ('process_generation', models.UUIDField()),
            ('failed_auth', models.PositiveSmallIntegerField(default=0)),
            ('cooldown_until', models.DateTimeField(null=True)),
            ('owner', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ], options={'constraints': [
            models.CheckConstraint(condition=(models.Q(owner__isnull=True, owner_revision=0) | models.Q(owner__isnull=False, owner_revision__gte=1)), name='portable_owner_revision'),
            models.CheckConstraint(condition=models.Q(failed_auth__lte=5), name='portable_auth_failure_cap'),
        ]}),
        migrations.CreateModel(name='PortableConnection', fields=[
            ('id', models.UUIDField(primary_key=True, serialize=False)),
            ('adapter', models.CharField(max_length=32)),
            ('model', models.CharField(max_length=128)),
            ('reasoning', models.CharField(max_length=16)),
            ('config_revision', models.PositiveBigIntegerField(default=1)),
            ('credential_generation', models.PositiveBigIntegerField(default=0)),
            ('removed', models.BooleanField(default=False)),
            ('installation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableinstallation')),
        ]),
    ]
