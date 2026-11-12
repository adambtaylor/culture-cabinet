"""Local-only selection settings; existing native writer guards stay installed."""
from importlib import import_module
from django.db import migrations,models

PREVIOUS=import_module('shelf.migrations.0028_guided_owner_journey')
GUARDS=['''CREATE TRIGGER journey_location_revision BEFORE UPDATE ON shelf_portablejourneystate
 WHEN NEW.location_revision < OLD.location_revision OR
 ((NEW.home_country IS NOT OLD.home_country OR NEW.home_postcode IS NOT OLD.home_postcode
 OR NEW.radius_km IS NOT OLD.radius_km) AND NEW.location_revision IS NOT OLD.location_revision+1)
 OR ((NEW.home_country IS OLD.home_country AND NEW.home_postcode IS OLD.home_postcode
 AND NEW.radius_km IS OLD.radius_km) AND NEW.location_revision IS NOT OLD.location_revision)
 BEGIN SELECT RAISE(ABORT,'checked local setting revision required'); END''']
TRIGGERS=list(PREVIOUS.TRIGGERS)+GUARDS


def state_guards(editor):
    # SQLite constraint reconstruction can replace the table. Restore exactly
    # the already admitted state-writer guards, never a looser replacement.
    for sql in PREVIOUS.GUARDS:
        if ' ON shelf_portablejourneystate' in sql:
            name=sql.split()[2]
            editor.execute('DROP TRIGGER IF EXISTS '+name)
            editor.execute(sql)


def install(apps,editor):
    state_guards(editor)
    for sql in GUARDS:editor.execute(sql)


def uninstall(apps,editor):
    if apps.get_model('shelf','PortableJourneyState').objects.filter(
            models.Q(location_revision__gt=0)|~models.Q(next_noise=25)).exists():
        raise RuntimeError('LOCAL_SELECTION_HISTORY_REVERSAL_REFUSED')
    for sql in GUARDS:editor.execute('DROP TRIGGER IF EXISTS '+sql.split()[2])


def restore_previous(apps,editor):state_guards(editor)


def effective_inventory(raw):
    previous=PREVIOUS.effective_inventory(raw)
    applied=raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0029_local_selection_preferences'").fetchone() is not None
    columns={row[1] for row in raw.execute('PRAGMA table_info(shelf_portablejourneystate)')}
    added={'home_country','home_postcode','radius_km','location_revision','next_noise'}
    if (not added<=columns if applied else bool(added&columns)):raise RuntimeError('LOCAL_SELECTION_SCHEMA_MISMATCH')
    return [*previous,*GUARDS] if applied else previous


class Migration(migrations.Migration):
    dependencies=[('shelf','0028_guided_owner_journey')]
    operations=[
        migrations.RunPython(migrations.RunPython.noop,restore_previous),
        migrations.AddField('portablejourneystate','home_country',models.CharField(max_length=2,null=True)),
        migrations.AddField('portablejourneystate','home_postcode',models.CharField(max_length=4,null=True)),
        migrations.AddField('portablejourneystate','radius_km',models.PositiveSmallIntegerField(null=True)),
        migrations.AddField('portablejourneystate','location_revision',models.PositiveBigIntegerField(default=0)),
        migrations.AddField('portablejourneystate','next_noise',models.PositiveSmallIntegerField(default=25)),
        migrations.AddConstraint('portablejourneystate',models.CheckConstraint(condition=(
            models.Q(home_country__isnull=True,home_postcode__isnull=True,radius_km__isnull=True)|
            models.Q(home_country='AU',home_postcode__isnull=False,radius_km__isnull=False,
                     radius_km__gte=1,radius_km__lte=500)),name='journey_local_setting')),
        migrations.AddConstraint('portablejourneystate',models.CheckConstraint(
            condition=models.Q(next_noise__lte=100),name='journey_noise_range')),
        migrations.RunPython(install,uninstall),
    ]
