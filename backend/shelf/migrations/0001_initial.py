from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="Profile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("owner", "Owner"), ("partner", "Partner"), ("family", "Family")], max_length=7, unique=True)),
                ("label", models.CharField(max_length=80)),
                ("active", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ["id"],
                "constraints": [models.CheckConstraint(condition=models.Q(kind__in=["owner", "partner", "family"]), name="profile_kind_allowed")],
            },
        ),
        migrations.CreateModel(
            name="Grant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("can_view", models.BooleanField(default=False)),
                ("can_select", models.BooleanField(default=False)),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="shelf.profile")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("user", "profile"), name="one_grant_per_user_profile")]},
        ),
    ]
