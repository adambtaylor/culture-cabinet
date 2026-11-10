from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0026_saved_credential_binding')]
    operations = [migrations.AddField(model_name='savedcredentialbinding',
        name='delete_no_effect_proof', field=models.JSONField(null=True, default=None))]
