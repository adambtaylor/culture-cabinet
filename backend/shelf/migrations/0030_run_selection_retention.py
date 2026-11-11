"""Retain original policy only; immutable attempts and content retirement stay strict."""
from importlib import import_module
from django.db import migrations

PREVIOUS=import_module('shelf.migrations.0029_local_selection_preferences')
CONTENT=import_module('shelf.migrations.0023_enhance_current_state')
INVENTORY=import_module('shelf.migrations.0022_portable_enhance_state')
name=CONTENT.name

# One predicate controls every first-redaction exception. Merely setting the
# timestamp cannot excuse changed attempt state, a wrong digest or extra data.
FIRST_REDACTION="""(OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NOT NULL
 AND cabinet_disclosure_write() IS 1 AND json(NEW.output) IS '{}'
 AND cabinet_redacted_selection_valid(OLD.evidence,NEW.evidence) IS 1
 AND NEW.content_digest IS cabinet_content_digest(OLD.output,OLD.evidence)
 AND NEW.state IS OLD.state AND NEW.portable_stage IS OLD.portable_stage
 AND NEW.attempts IS OLD.attempts AND NEW.transport_request_id IS OLD.transport_request_id
 AND NEW.cancel_requested IS OLD.cancel_requested)"""
REDACTED_UNCHANGED="""(OLD.content_redacted_at IS NOT NULL
 AND NEW.content_redacted_at IS OLD.content_redacted_at AND NEW.content_digest IS OLD.content_digest
 AND json(NEW.output) IS '{}' AND cabinet_redacted_selection_valid(OLD.evidence,NEW.evidence) IS 1)"""
run=next(sql for sql in PREVIOUS.TRIGGERS if name(sql)=='enhance_run_update')
assert CONTENT.FIRST_RUN_REDACTION in run
run=run.replace(CONTENT.FIRST_RUN_REDACTION,FIRST_REDACTION)
old="(NEW.content_redacted_at IS NULL AND json_extract(NEW.output,'$.schema') IS NOT 'cabinet-inference-result/v1')"
assert old in run
run=run.replace(old,"(NOT "+FIRST_REDACTION+" AND NOT "+REDACTED_UNCHANGED+
    " AND json_extract(NEW.output,'$.schema') IS NOT 'cabinet-inference-result/v1')")
content='''CREATE TRIGGER enhance_run_content_update BEFORE UPDATE ON shelf_synthesisrun
 WHEN OLD.contract_version='enhance/v1'
 BEGIN
 SELECT CASE WHEN OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NOT NULL
  AND NOT '''+FIRST_REDACTION+''' THEN RAISE(ABORT,'invalid first selection redaction') END;
 SELECT CASE WHEN OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NULL AND NEW.content_digest!=''
  THEN RAISE(ABORT,'unbound content digest') END;
 SELECT CASE WHEN OLD.content_redacted_at IS NOT NULL AND (NOT '''+REDACTED_UNCHANGED+'''
  OR NEW.attempts IS NOT OLD.attempts OR NEW.transport_request_id IS NOT OLD.transport_request_id
  OR (NEW.attempts=0 AND NEW.state NOT IN ('queued','failed')))
  THEN RAISE(ABORT,'redacted selection content cannot return') END;
 END'''
VALID="""cabinet_selection_valid(NEW.evidence,NEW.portable_bindings,NEW.refresh_nonce,
 NEW.input_digest,NEW.id,NEW.collection_digest,NEW.content_redacted_at IS NOT NULL) IS NOT 1"""
insert='''CREATE TRIGGER enhance_selection_insert BEFORE INSERT ON shelf_synthesisrun
 WHEN NEW.contract_version='enhance/v1'
 BEGIN SELECT CASE WHEN '''+VALID+'''
 OR json_type(NEW.evidence,'$.selection_seed') IS NOT NULL
 OR json_type(NEW.evidence,'$.selection_policy_digest') IS NOT NULL
 OR json_type(NEW.evidence,'$.selection_report') IS NOT NULL
 THEN RAISE(ABORT,'checked initial selection policy required') END; END'''
update='''CREATE TRIGGER enhance_selection_update BEFORE UPDATE ON shelf_synthesisrun
 WHEN OLD.contract_version='enhance/v1'
 BEGIN
 SELECT CASE WHEN '''+VALID+'''
 OR cabinet_redacted_selection(NEW.evidence) IS NULL OR cabinet_redacted_selection(OLD.evidence) IS NULL
 OR cabinet_redacted_selection(NEW.evidence) IS NOT cabinet_redacted_selection(OLD.evidence)
 THEN RAISE(ABORT,'immutable original selection policy') END;
 SELECT CASE WHEN NOT '''+FIRST_REDACTION+''' AND (
 (json_type(OLD.evidence,'$.selection_seed') IS NOT NULL AND
  cabinet_selection_field_equal(OLD.evidence,NEW.evidence,'selection_seed') IS NOT 1)
 OR (json_type(OLD.evidence,'$.selection_policy_digest') IS NOT NULL AND
  cabinet_selection_field_equal(OLD.evidence,NEW.evidence,'selection_policy_digest') IS NOT 1)
 OR (json_type(OLD.evidence,'$.selection_report') IS NOT NULL AND
  cabinet_selection_field_equal(OLD.evidence,NEW.evidence,'selection_report') IS NOT 1))
 THEN RAISE(ABORT,'frozen selection evidence changed') END;
 SELECT CASE WHEN json_type(OLD.evidence,'$.selection_seed') IS NULL
 AND json_type(NEW.evidence,'$.selection_seed') IS NOT NULL AND
 (OLD.content_redacted_at IS NOT NULL OR OLD.state!='completed' OR NEW.state!='completed'
  OR OLD.portable_stage!='validated' OR NEW.portable_stage!='validated' OR OLD.attempts!=1
  OR OLD.collection_digest='' OR json_type(NEW.evidence,'$.selection_report') IS NOT NULL)
 THEN RAISE(ABORT,'selection freeze requires validated run') END;
 SELECT CASE WHEN json_type(OLD.evidence,'$.selection_report') IS NULL
 AND json_type(NEW.evidence,'$.selection_report') IS NOT NULL AND
 (OLD.content_redacted_at IS NOT NULL OR OLD.state!='completed' OR NEW.state!='completed'
  OR OLD.attempts!=1 OR NEW.portable_stage!='published'
  OR json_type(OLD.evidence,'$.selection_seed') IS NULL
  OR NOT EXISTS (SELECT 1 FROM shelf_portablecabinetmanifest m WHERE m.run_id=NEW.id
   AND m.id=replace(json_extract(NEW.evidence,'$.publication_ref'),'-','') AND m.content_redacted_at IS NULL)
  OR EXISTS (SELECT 1 FROM json_each(NEW.evidence,'$.selection_report.categories') g,
   json_each(g.value,'$.exploratory_keys') k WHERE NOT EXISTS
   (SELECT 1 FROM shelf_portablecabinetmanifest m,json_each(m.manifest,'$.categories') mg,
    json_each(mg.value,'$.cards') c WHERE m.run_id=NEW.id AND m.content_redacted_at IS NULL
    AND json_extract(mg.value,'$.category')=json_extract(g.value,'$.category')
    AND json_extract(c.value,'$.key')=k.value)))
 THEN RAISE(ABORT,'selection report requires actual publication') END;
 END'''
REPLACEMENTS={'enhance_run_update':run,'enhance_run_content_update':content}
ADDED=[insert,update]
TRIGGERS=[REPLACEMENTS.get(name(sql),sql) for sql in PREVIOUS.TRIGGERS]+ADDED


def install(apps,editor):
    from shelf.models import register_closed_sql_guards
    register_closed_sql_guards(editor.connection.connection)
    INVENTORY.guard_inventory(editor,PREVIOUS.TRIGGERS)
    for key in REPLACEMENTS:editor.execute('DROP TRIGGER '+key)
    for sql in [*REPLACEMENTS.values(),*ADDED]:editor.execute(sql)


def uninstall(apps,editor):
    # A downgrade must not silently discard policy enforcement for old attempts.
    from shelf.models import register_closed_sql_guards
    register_closed_sql_guards(editor.connection.connection)
    with editor.connection.cursor() as cursor:
        present=cursor.execute("SELECT 1 FROM shelf_synthesisrun WHERE contract_version='enhance/v1' AND (json_type(evidence,'$.selection_policy') IS NOT NULL OR cabinet_selection_valid(evidence,portable_bindings,refresh_nonce,input_digest,id,collection_digest,content_redacted_at IS NOT NULL) IS NOT 1) LIMIT 1").fetchone()
    if present:raise RuntimeError('SELECTION_HISTORY_REVERSAL_REFUSED')
    INVENTORY.guard_inventory(editor,TRIGGERS)
    for sql in [*REPLACEMENTS.values(),*ADDED]:editor.execute('DROP TRIGGER '+name(sql))
    for sql in PREVIOUS.TRIGGERS:
        if name(sql) in REPLACEMENTS:editor.execute(sql)


def effective_inventory(raw):
    previous=PREVIOUS.effective_inventory(raw)
    applied=raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0030_run_selection_retention'").fetchone() is not None
    return [REPLACEMENTS.get(name(sql),sql) for sql in previous]+ADDED if applied else previous


class Migration(migrations.Migration):
    dependencies=[('shelf','0029_local_selection_preferences')]
    operations=[migrations.RunPython(install,uninstall)]
