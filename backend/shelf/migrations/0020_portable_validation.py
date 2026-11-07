from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('shelf', '0019_portable_installation'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name='portableconnection', name='validation_generation', field=models.PositiveBigIntegerField(default=0)),
        migrations.CreateModel(name='PortableProbeAttempt', fields=[
            ('id', models.UUIDField(primary_key=True, serialize=False)),
            ('preview_ref', models.UUIDField()), ('bindings', models.JSONField()),
            ('body_digest', models.CharField(max_length=64)),
            ('state', models.CharField(max_length=12)), ('outcome', models.CharField(max_length=32)),
            ('created_at', models.DateTimeField()),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
            ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ], options={'constraints': [models.CheckConstraint(condition=models.Q(state__in=['prepared', 'sending', 'completed', 'failed', 'unknown']), name='portable_probe_state')]}),
        migrations.CreateModel(name='PortableValidation', fields=[
            ('attempt', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, serialize=False, to='shelf.portableprobeattempt')),
            ('bindings', models.JSONField()), ('proof_kind', models.CharField(max_length=32)),
            ('observed_model', models.CharField(max_length=128, null=True)),
            ('tested_at', models.DateTimeField()), ('expires_at', models.DateTimeField()),
            ('observation_limits', models.JSONField(default=list)),
        ]),
    ]
