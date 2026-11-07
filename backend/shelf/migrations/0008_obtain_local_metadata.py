from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0007_obtain_transfer')]
    operations = [
        migrations.AddField('obtainlocalfile', 'acquired_at', models.DateTimeField(null=True)),
        migrations.AddField('obtainlocalfile', 'playback_verified', models.BooleanField(default=False)),
        migrations.AddField('obtainlocalfile', 'metadata', models.JSONField(default=dict)),
    ]
