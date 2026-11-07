from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0013_merge_browser_workflows')]
    operations = [migrations.CreateModel(name='ObtainReviewedNZB', fields=[
        ('request', models.OneToOneField('shelf.obtainpreview', primary_key=True,
                                        serialize=False, on_delete=models.CASCADE)),
        ('exact', models.JSONField()),
        ('payload', models.BinaryField()),
        ('expires_at', models.DateTimeField()),
    ])]
