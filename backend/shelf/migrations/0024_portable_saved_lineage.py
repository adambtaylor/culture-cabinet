"""Nullable attribution with immutable surviving lineage and exact owned guards.

Legacy rows remain null. Reversal explicitly loses only lineage; neither saved
intent nor acquisition receipts are erased. DDL and guard replacement are atomic.
"""
from importlib import import_module
from django.db import migrations, models

PREVIOUS = import_module('shelf.migrations.0023_enhance_current_state')
INVENTORY = import_module('shelf.migrations.0022_portable_enhance_state')
LINEAGE_GUARDS = [
    '''CREATE TRIGGER saved_lineage_insert BEFORE INSERT ON shelf_saveditem
     WHEN NEW.publication_lineage IS NOT NULL AND cabinet_saved_lineage_valid(NEW.publication_lineage) IS NOT 1
     BEGIN SELECT RAISE(ABORT,'bounded saved lineage required'); END''',
    '''CREATE TRIGGER saved_lineage_immutable BEFORE UPDATE ON shelf_saveditem
     WHEN NEW.publication_lineage IS NOT OLD.publication_lineage
     BEGIN SELECT RAISE(ABORT,'immutable saved lineage'); END''',
]
TRIGGERS = list(PREVIOUS.TRIGGERS) + LINEAGE_GUARDS


def check(editor, definitions):
    if not editor.connection.in_atomic_block:
        raise RuntimeError('PORTABLE_ATOMIC_MIGRATION_REQUIRED')
    INVENTORY.guard_inventory(editor, definitions)
    with editor.connection.cursor() as cursor:
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
            raise RuntimeError('PORTABLE_UNKNOWN_VIEW_REFUSED')


def drop(editor, definitions):
    check(editor, definitions)
    INVENTORY.drop_inventory(editor, definitions)


def install(editor, definitions):
    from shelf.models import register_closed_sql_guards
    register_closed_sql_guards(editor.connection.connection)
    INVENTORY.restore_inventory(editor, definitions)
    check(editor, definitions)


def forward_drop(apps, editor):
    drop(editor, PREVIOUS.TRIGGERS)


def forward_install(apps, editor):
    install(editor, TRIGGERS)


def reverse_drop(apps, editor):
    drop(editor, TRIGGERS)


def reverse_install(apps, editor):
    install(editor, PREVIOUS.TRIGGERS)


def effective_inventory(raw):
    """Schema and migration record must agree; never discover arbitrary guards."""
    columns = {r[1] for r in raw.execute('PRAGMA table_info(shelf_saveditem)')}
    applied = raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0024_portable_saved_lineage'").fetchone() is not None
    if ('publication_lineage' in columns) != applied:
        raise RuntimeError('PORTABLE_LINEAGE_SCHEMA_MISMATCH')
    return TRIGGERS if applied else PREVIOUS.TRIGGERS


class Migration(migrations.Migration):
    atomic = True
    dependencies = [('shelf', '0023_enhance_current_state')]
    operations = [
        migrations.RunPython(forward_drop, reverse_install),
        migrations.AddField(model_name='saveditem', name='publication_lineage',
                            field=models.JSONField(null=True, default=None)),
        migrations.RunPython(forward_install, reverse_drop),
    ]
