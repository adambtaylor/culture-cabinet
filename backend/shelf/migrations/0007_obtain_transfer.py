from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0006_discovery_state')]
    operations = [
        migrations.CreateModel(name='ObtainTransfer', fields=[
            ('job', models.OneToOneField('shelf.acquisitionjob', primary_key=True, serialize=False, on_delete=models.PROTECT)),
            ('binding', models.JSONField()),
            ('result_guid', models.CharField(max_length=20, unique=True)),
            ('nzb_sha256', models.CharField(max_length=64, unique=True)),
            ('revision', models.PositiveIntegerField(default=0)),
            ('state', models.CharField(max_length=24, default='blocked')),
            ('reason', models.CharField(max_length=64, default='verification_required')),
            ('provider_job', models.CharField(max_length=120, blank=True, default='')),
            ('local_cancelled_at', models.DateTimeField(null=True)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
        ], options={'constraints': [models.CheckConstraint(condition=models.Q(state__in=[
            'blocked', 'outcome_unknown', 'queued', 'failed', 'cancel_pending',
            'cancelled', 'local_verified']), name='obtain_transfer_state')]}),
        migrations.CreateModel(name='ObtainLocalFile', fields=[
            ('transfer', models.OneToOneField('shelf.obtaintransfer', primary_key=True, serialize=False, on_delete=models.PROTECT)),
            ('path', models.CharField(max_length=4096, unique=True)),
            ('sha256', models.CharField(max_length=64, unique=True)),
            ('size', models.PositiveBigIntegerField()),
            ('verified_at', models.DateTimeField(auto_now_add=True)),
        ]),
    ]
