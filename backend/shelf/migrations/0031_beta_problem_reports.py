import django.conf
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('shelf', '0030_run_selection_retention'),
        migrations.swappable_dependency(django.conf.settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FeedbackTesterBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('active', models.BooleanField(default=True)),
                ('installation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to='shelf.portableinstallation')),
                ('tester', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to=django.conf.settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='BetaProblemReport',
            fields=[
                ('reference', models.UUIDField(primary_key=True, serialize=False)),
                ('body', models.TextField(null=True)),
                ('deleted', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('installation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to='shelf.portableinstallation')),
                ('tester', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to=django.conf.settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(model_name='betaproblemreport', constraint=models.CheckConstraint(
            condition=(models.Q(deleted=False, body__isnull=False)
                       | models.Q(deleted=True, body__isnull=True)),
            name='beta_report_deleted_body')),
    ]
