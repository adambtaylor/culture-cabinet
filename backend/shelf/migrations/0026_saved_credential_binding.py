import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0025_obtain_native_origin')]
    operations = [migrations.CreateModel(
        name='SavedCredentialBinding',
        fields=[
            ('connection', models.OneToOneField(primary_key=True, serialize=False,
                on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
            ('binding', models.JSONField()),
            ('config_revision', models.PositiveBigIntegerField()),
            ('state', models.CharField(max_length=16)),
            ('generation', models.UUIDField()),
            ('operation_id', models.UUIDField()),
            ('store_revision', models.CharField(max_length=128, null=True)),
            ('entered_at', models.DateTimeField()),
            ('updated_at', models.DateTimeField()),
            ('expires_at', models.DateTimeField(null=True)),
            ('scopes', models.JSONField(default=list)),
            ('removed', models.BooleanField(default=False)),
            ('remove_requested', models.BooleanField(default=False)),
        ],
        options={'constraints': [models.CheckConstraint(condition=models.Q(
            state__in=['PENDING_WRITE', 'ACTIVE', 'PENDING_DELETE', 'UNAVAILABLE']),
            name='saved_credential_binding_state')]},
    )]
