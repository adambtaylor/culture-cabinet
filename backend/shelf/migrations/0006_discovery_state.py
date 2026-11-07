"""Add only isolated music/events human state; no old table or data changes."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('shelf', '0005_obtain_preview'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(
        name='DiscoveryState',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('mode', models.CharField(choices=[('household', 'Household'), ('sample', 'Sample')], max_length=9)),
            ('revision', models.PositiveIntegerField(default=0)),
            ('nonce', models.CharField(max_length=200)),
            ('history', models.JSONField(default=list)),
            ('receipts', models.JSONField(default=dict)),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('profile', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profile')),
        ],
        options={'constraints': [
            models.UniqueConstraint(fields=('actor', 'profile', 'mode'), name='discovery_actor_profile_mode'),
            models.CheckConstraint(condition=models.Q(mode__in=['household', 'sample']), name='discovery_mode_allowed'),
        ]},
    )]
