from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0010_event_plan'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='SocialDiscoveryState', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
        ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
        ('mode', models.CharField(max_length=12)),
        ('revision', models.PositiveIntegerField(default=0)),
        ('nonce', models.CharField(max_length=200)),
        ('data', models.JSONField(default=dict)),
        ('receipts', models.JSONField(default=dict)),
    ], options={'constraints': [
        models.UniqueConstraint(fields=('actor', 'profile', 'mode'), name='social_actor_profile_mode'),
        models.CheckConstraint(condition=models.Q(mode__in=['household', 'sample']), name='social_mode_allowed'),
    ]})]
