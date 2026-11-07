"""Bound fictional import and human export. Packages never confer authority.

All renderings carry the same typed document. CSV free text stays inside JSON;
Markdown's escaped human summary is separate from its complete JSON appendix.
Imports prevalidate the entire package before the first database write.
"""
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone as dt_timezone
import hashlib
import html
import io
import re
import uuid

from django.db import connection
from django.utils import timezone
from shelf.feedback import (FeedbackError, Conflict, Gone, canonical, strict_json, exact, uid,
    text, integer, validate_body, envelope_hash, authorize, prior_receipt, immediate,
    is_busy, Busy, recompute, NATIVE_LABELS, SIGNALS, ORIGINS, bounded_preflight)
from shelf.models import (FeedbackTarget, FeedbackEvent, FeedbackProjection, FeedbackSubject,
                          MutationReceipt)
from shelf.recovery import access_barrier, installed_coordinator, RecoveryBlocked

SCHEMA = 'cc.feedback.v1'
REDACTED_SCHEMA = 'cc.feedback.v2'
REDACTED_FIELDS = ('value','source_record_id','source_time','source_metadata',
                   'attribution','original_recorded_at')
LEGACY_SCHEMA = 'cc.legacy-fiction.v1'
NAMESPACE = uuid.UUID('00000000-0000-4000-8000-000000000200')
SCALES = {'integer-1-5': {'values': [1,2,3,4,5], 'basis':'experienced'},
          'legacy-native-1-5': {'values':[1,2,3,4,5], 'basis':'legacy_unknown', 'labels':NATIVE_LABELS}}
TARGET_FIELDS = ('id','kind','parent_id','title','category','source_namespace','source_key')
RECORD_FIELDS = ('id','target_id','signal','scope','revision','operation','value','source_record_id',
    'source_time','source_metadata','original_origin','attribution','original_recorded_at',
    'reverses_id','corrects_id','erased')
PACKAGE_FIELDS = ('schema_version','package_id','fictional','exported_at','subject_id','targets',
                  'records','scales','counts','restrictions')


@dataclass(frozen=True)
class ValidatedImportBinding:
    """Trusted synthetic caller attestations; see backend/HANDOFF.md's binding contract.

    Independent record approvals default to empty. The caller must derive them
    from independently specified approved fiction or witnessed authorized local
    direct mutations BEFORE export or input parsing. Incoming origin labels,
    digests and imported rows cannot confer that trust. Revalidate the separate
    attestations with fresh authority and bindings at each destination and replay;
    new local continuations need new witnessed approvals.
    """

    destination_profile_id: int
    source_namespace: str
    source_person_id: str | None
    source_subject_id: str
    destination_subject_id: str
    target_map: tuple
    fictional_manifest_digest: str
    independent_record_digests: tuple = ()

    def canonical_value(self):
        value=asdict(self)
        value['target_map']=sorted([dict(x) for x in self.target_map],
            key=lambda x:(x['source_namespace'],x['media'],str(x['id']),x['target_uuid']))
        value['independent_record_digests']=sorted(
            [dict(x) for x in self.independent_record_digests], key=lambda x:x['id'])
        return value


@dataclass(frozen=True)
class ExportArtifact:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class ImportReport:
    status: str
    counts: dict | None


def timestamp(value, *, nullable=True):
    if value is None and nullable: return None
    if not isinstance(value,str) or len(value)>40: raise FeedbackError()
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.utcoffset() != dt_timezone.utc.utcoffset(result): raise ValueError()
        return result
    except (ValueError,TypeError): raise FeedbackError() from None


def iso(value):
    return value.isoformat() if value else None


def validate_snapshot(value):
    if value is None: return
    if not isinstance(value,dict) or set(value)-{'state','rating','saved'}: raise FeedbackError()
    if 'state' in value and value['state'] not in ('rated','unseen','skipped'): raise FeedbackError()
    if 'saved' in value and type(value['saved']) is not bool: raise FeedbackError()
    if 'rating' in value:
        rating=value['rating']
        if rating is not None and (type(rating) is not int or rating not in range(1,6)): raise FeedbackError()
        if value.get('state')=='rated' and rating is None: raise FeedbackError()
        if value.get('state') in ('unseen','skipped') and rating is not None: raise FeedbackError()


def validate_metadata(value):
    if value is None: return
    if not isinstance(value,dict) or set(value)-{'request_id','undone','before','after'} or len(canonical(value))>2048: raise FeedbackError()
    if 'request_id' in value:
        text(value['request_id'],100)
        if len(value['request_id'])<8: raise FeedbackError()
    if 'undone' in value and type(value['undone']) is not bool: raise FeedbackError()
    for key in ('before','after'):
        if key in value: validate_snapshot(value[key])


def validate_provenance(record, subject):
    if record['original_origin'] not in ORIGINS: raise FeedbackError()
    attribution=record['attribution']
    if attribution is not None:
        if not isinstance(attribution,dict) or set(attribution) not in ({'subject_id'},{'subject_id','label'}): raise FeedbackError()
        if uid(attribution['subject_id'])!=subject: raise FeedbackError()
        if 'label' in attribution: text(attribution['label'],160)
    timestamp(record['original_recorded_at']); timestamp(record['source_time'])
    if record['source_record_id'] is not None: text(record['source_record_id'],160)
    if len(canonical({k:record[k] for k in ('original_origin','attribution','original_recorded_at')}))>512: raise FeedbackError()
    validate_metadata(record['source_metadata'])
    if record['erased'] and any(record[k] is not None for k in ('value','attribution','original_recorded_at','source_time','source_metadata','source_record_id')): raise FeedbackError()


def document_counts(package):
    records=package['records']; groups={}
    for r in records:
        groups.setdefault((r['target_id'],r['signal'],r['scope']),[]).append(r)
    active=0; bases={}
    for r in records:
        if r['signal'] in ('legacy_rating','enjoyment') and r['operation']=='set' and r['value'] is not None:
            basis=r['value']['basis']; bases[basis]=bases.get(basis,0)+1
    for key,group in groups.items():
        group.sort(key=lambda r:r['revision'])
        if key[1] not in ('enjoyment','legacy_rating') or any(r['erased'] for r in group): continue
        reversed_ids={r['reverses_id'] for r in group if r['operation']=='reverse'}
        effective=next((r for r in reversed(group) if r['operation']=='set' and r['id'] not in reversed_ids),None)
        if effective and effective['value'] is not None: active+=1
    counts = {'targets':len(package['targets']), 'events':len(records),'projections':len(groups),
        'source_interaction_rows':len({r['source_record_id'] for r in records if r['original_origin']=='legacy_snapshot' and r['source_record_id'] is not None}),
        'rating_statements_by_basis':bases,'active_rating_projections':active,
        'action_statements':sum(r['signal'] in ('saved','skipped','opened','completion') and r['operation']=='set' for r in records),
        'seeds':sum(r['signal']=='seed_statement' for r in records),
        'reversals':sum(r['operation']=='reverse' for r in records),
        'corrections':sum(r['corrects_id'] is not None for r in records),
        'tombstones':len(package['restrictions'])}
    if package['schema_version']==REDACTED_SCHEMA:
        counts['source_redactions']=sum(r['source_redacted'] is True for r in records)
    return counts


def validate_package(package):
    exact(package,PACKAGE_FIELDS)
    if package['schema_version'] not in (SCHEMA,REDACTED_SCHEMA) or package['fictional'] is not True or package['scales']!=SCALES: raise FeedbackError()
    uid(package['package_id']); subject=uid(package['subject_id']); timestamp(package['exported_at'],nullable=False)
    if not isinstance(package['records'],list) or len(package['records'])>100000 or not isinstance(package['targets'],list): raise FeedbackError()
    targets={}; records={}; groups={}
    for t in package['targets']:
        exact(t,TARGET_FIELDS); uid(t['id'])
        if t['id'] in targets: raise FeedbackError()
        for f,n in (('title',200),('category',80),('source_namespace',80),('source_key',160)): text(t[f],n,empty=False)
        if t['parent_id'] is not None: uid(t['parent_id'])
        if t['kind'] not in ('item','session') or (t['kind']=='item') != (t['parent_id'] is None): raise FeedbackError()
        targets[t['id']]=t
    if len({(t['source_namespace'],t['source_key']) for t in targets.values()})!=len(targets): raise FeedbackError()
    for t in targets.values():
        if t['parent_id'] and (t['parent_id'] not in targets or targets[t['parent_id']]['kind']!='item'): raise FeedbackError()
    for r in package['records']:
        exact(r,RECORD_FIELDS+(('source_redacted',) if package['schema_version']==REDACTED_SCHEMA else ()))
        if package['schema_version']==REDACTED_SCHEMA:
            if type(r['source_redacted']) is not bool: raise FeedbackError()
            if r['source_redacted'] and (r['erased'] or r['original_origin']=='direct' or
                    any(r[k] is not None for k in REDACTED_FIELDS)): raise FeedbackError()
        uid(r['id']); integer(r['revision'],1)
        if type(r['erased']) is not bool or r['id'] in records or r['target_id'] not in targets or r['scope']!=targets[r['target_id']]['kind'] or r['operation'] not in ('set','reverse','erase'): raise FeedbackError()
        validate_body(r['signal'],r['value'],imported=True); validate_provenance(r,subject)
        if r['operation']!='set' and r['value'] is not None: raise FeedbackError()
        if (r['operation']=='reverse') != (r['reverses_id'] is not None): raise FeedbackError()
        if r['operation']=='erase' and not r['erased']: raise FeedbackError()
        for k in ('reverses_id','corrects_id'):
            if r[k] is not None: uid(r[k])
        records[r['id']]=r
        groups.setdefault((r['target_id'],r['signal'],r['scope']),[]).append(r)
    reversed_ids=set()
    for group in groups.values():
        group.sort(key=lambda r:r['revision'])
        if [r['revision'] for r in group]!=list(range(1,len(group)+1)): raise FeedbackError()
        erased=any(r['erased'] for r in group)
        if erased and (not all(r['erased'] for r in group) or group[-1]['operation']!='erase'): raise FeedbackError()
        for r in group:
            for key in ('reverses_id','corrects_id'):
                if r[key]:
                    ref=records.get(r[key])
                    if ref not in group or ref['operation']!='set' or ref['revision']>=r['revision']: raise FeedbackError()
            if r['reverses_id']:
                if r['reverses_id'] in reversed_ids: raise FeedbackError()
                reversed_ids.add(r['reverses_id'])
    seen=set()
    if not isinstance(package['restrictions'],list): raise FeedbackError()
    for r in package['restrictions']:
        exact(r,('kind','target_id','scope','signals','through_revision'))
        if r['kind']!='feedback_erased' or not isinstance(r['signals'],list) or not r['signals'] or len(r['signals'])!=len(set(r['signals'])): raise FeedbackError()
        integer(r['through_revision'],1)
        for signal in r['signals']:
            key=(r['target_id'],signal,r['scope'])
            if key in seen or key not in groups: raise FeedbackError()
            group=groups[key]
            if not all(e['erased'] for e in group) or group[-1]['operation']!='erase' or group[-1]['revision']!=r['through_revision']: raise FeedbackError()
            seen.add(key)
    if seen!={key for key,group in groups.items() if any(r['erased'] for r in group)}: raise FeedbackError()
    if package['schema_version']==REDACTED_SCHEMA and not any(r['source_redacted'] for r in package['records']): raise FeedbackError()
    expected_counts=document_counts(package)
    if package['schema_version']==REDACTED_SCHEMA:
        if not isinstance(package['counts'],dict) or set(package['counts'])!=set(expected_counts): raise FeedbackError()
        for k,v in expected_counts.items():
            actual=package['counts'][k]
            if isinstance(v,dict):
                if not isinstance(actual,dict) or any(type(n) is not int for n in actual.values()): raise FeedbackError()
            elif type(actual) is not int: raise FeedbackError()
    if package['counts']!=expected_counts: raise FeedbackError()
    return package


def encode_package(package,format):
    data=canonical(package)
    if format=='json': return data
    if format=='csv':
        out=io.StringIO(newline=''); w=csv.writer(out)
        w.writerow(('schema_version','package_id','record_type','record_id','data_json'))
        manifest={k:v for k,v in package.items() if k not in ('targets','records','scales','counts','restrictions')}
        entries=[('manifest','manifest',manifest)]
        for kind,key in (('target','targets'),('record','records'),('restriction','restrictions')):
            entries.extend((kind,str(i),v) for i,v in enumerate(package[key]))
        entries.extend([('scale','scales',package['scales']),('counts','counts',package['counts'])])
        for kind,key,value in entries: w.writerow((package['schema_version'],package['package_id'],kind,key,canonical(value).decode()))
        return out.getvalue().encode()
    if format=='md':
        fence='`'*max(3,1+max((len(m.group()) for m in re.finditer(r'`+',data.decode())),default=0))
        lines=['# Fictional feedback export','','Ratings from legacy sources have an unknown basis.','',
               '| Work | Kind |','| --- | --- |']
        for t in package['targets']:
            title=html.escape(t['title']).replace('|','&#124;').replace('\n','<br>').replace('[','&#91;').replace(']','&#93;').replace('`','&#96;')
            lines.append(f"| {title} | {t['kind']} |")
        lines.extend(['','| Signal | Statement | Basis |','| --- | --- | --- |'])
        from shelf.feedback import LABELS
        for record in package['records']:
            if record['operation'] != 'set' or record['erased']: continue
            body = record['value']
            if record.get('source_redacted',False): statement = 'Source content redacted'
            elif body is None: statement = 'Explicitly cleared'
            elif 'text' in body: statement = body['text']
            else: statement = str(body['value'])
            escape = lambda value: html.escape(str(value)).replace('|','&#124;').replace('\n','<br>').replace('[','&#91;').replace(']','&#93;').replace('`','&#96;')
            lines.append('| '+escape(LABELS[record['signal']])+' | '+escape(statement)+' | '+escape((body or {}).get('basis','Unstated'))+' |')
        lines.extend(['','Canonical JSON appendix (complete import document):',f'{fence}json',data.decode(),fence,''])
        return '\n'.join(lines).encode()
    raise FeedbackError()


def decode_package(data):
    if not isinstance(data,bytes) or len(data)>64*1024*1024: raise FeedbackError()
    stripped=data.lstrip()
    if stripped.startswith(b'{'): return strict_json(stripped)
    if stripped.startswith(b'# Fictional feedback export'):
        try:
            lines=data.decode().splitlines()
            index=lines.index('Canonical JSON appendix (complete import document):')
            fence=lines[index+1]
            if not re.fullmatch(r'`{3,}json',fence) or lines[-1]!=fence[:-4]: raise FeedbackError()
            return strict_json('\n'.join(lines[index+2:-1]).encode())
        except (UnicodeError,ValueError,IndexError): raise FeedbackError() from None
    try:
        csv.field_size_limit(64*1024*1024)
        reader=csv.reader(io.StringIO(data.decode(),newline=''),strict=True)
        if next(reader)!=['schema_version','package_id','record_type','record_id','data_json']: raise FeedbackError()
        parts={}; seen=set(); package_id=None; version=None
        for row in reader:
            if len(row)!=5 or row[0] not in (SCHEMA,REDACTED_SCHEMA) or (version is not None and row[0]!=version) or (package_id is not None and row[1]!=package_id): raise FeedbackError()
            version=row[0]; package_id=row[1]; key=(row[2],row[3])
            if key in seen or row[2] not in ('manifest','target','record','scale','restriction','counts'): raise FeedbackError()
            seen.add(key); parts.setdefault(row[2],[]).append(strict_json(row[4].encode()))
        if any(len(parts.get(k,[]))!=1 for k in ('manifest','scale','counts')): raise FeedbackError()
        p=parts['manifest'][0]
        if p['package_id']!=package_id or p['schema_version']!=version: raise FeedbackError()
        p.update(targets=parts.get('target',[]),records=parts.get('record',[]),restrictions=parts.get('restriction',[]),scales=parts['scale'][0],counts=parts['counts'][0])
        return p
    except (UnicodeError,csv.Error,StopIteration,KeyError): raise FeedbackError() from None


def package_for(profile):
    subject=FeedbackSubject.objects.filter(profile=profile).first()
    if not subject: raise FeedbackError('There are no feedback statements to export.')
    records=[]
    coordinator=installed_coordinator()
    restricted_sources={r['subject_id'] for _,r in coordinator.committed_restrictions() if r['restriction_code']=='deny_source_content'} if coordinator else set()
    for e in FeedbackEvent.objects.filter(profile=profile).select_related('target').order_by('target_id','signal','scope','revision'):
        records.append(dict(id=str(e.pk),target_id=str(e.target_id),signal=e.signal,scope=e.scope,
            revision=e.revision,operation=e.operation,value=e.body,source_record_id=e.source_record_id,
            source_time=iso(e.source_time),source_metadata=e.source_metadata,original_origin=e.original_origin,
            attribution=e.attribution,original_recorded_at=iso(e.original_recorded_at),
            reverses_id=str(e.reverses_id) if e.reverses_id else None,
            corrects_id=str(e.corrects_id) if e.corrects_id else None,erased=e.erased))
        redacted=not e.erased and e.original_origin!='direct' and e.target.source_namespace in restricted_sources
        records[-1]['source_redacted']=redacted
        if redacted:
            for field in REDACTED_FIELDS: records[-1][field]=None
    version=REDACTED_SCHEMA if any(r['source_redacted'] for r in records) else SCHEMA
    if version==SCHEMA:
        for r in records: del r['source_redacted']
    ids={r['target_id'] for r in records}
    # Sessions need their structural item parent even without item statements.
    targets=list(FeedbackTarget.objects.filter(pk__in=ids)); ids.update(str(t.parent_id) for t in targets if t.parent_id)
    targets=[dict(id=str(t.pk),kind=t.kind,parent_id=str(t.parent_id) if t.parent_id else None,
        title=t.title,category=t.category,source_namespace=t.source_namespace,source_key=t.source_key)
        for t in FeedbackTarget.objects.filter(pk__in=ids).order_by('id')]
    restrictions=[]
    for p in FeedbackProjection.objects.filter(profile=profile,erased=True).order_by('target_id','signal'):
        if str(p.target_id) not in ids: continue
        restrictions.append(dict(kind='feedback_erased',target_id=str(p.target_id),scope=p.scope,
                                 signals=[p.signal],through_revision=p.revision))
    package=dict(schema_version=version,package_id=str(uuid.uuid4()),fictional=True,
        exported_at=timezone.now().isoformat(),subject_id=str(subject.subject_id),targets=targets,
        records=records,scales=SCALES,counts={},restrictions=restrictions)
    package['counts']=document_counts(package)
    return validate_package(package)


def human_export(*, actor, profile_id, format):
    if format not in ('json','csv','md'): raise FeedbackError()
    with access_barrier() as barrier:
        if connection.in_atomic_block or connection.connection.in_transaction: raise FeedbackError()
        connection.connection.execute('BEGIN')
        try:
            profile,_=authorize(actor,profile_id,'export')
            package=package_for(profile)
            authorize(actor,profile_id,'export')
            content=encode_package(package,format)
            authorize(actor,profile_id,'export')
        finally: connection.connection.execute('ROLLBACK')
        barrier.verify_current() if hasattr(barrier,'verify_current') else None
        return ExportArtifact(content,{'json':'application/json','csv':'text/csv; charset=utf-8','md':'text/markdown; charset=utf-8'}[format],
                              'culture-cabinet-fiction-'+package['schema_version'].rsplit('.',1)[1]+'.'+format)


def check_binding(binding,profile_id,package):
    if not isinstance(binding,ValidatedImportBinding) or binding.destination_profile_id!=int(profile_id): raise FeedbackError()
    if uid(binding.source_subject_id)!=package['subject_id'] or uid(binding.destination_subject_id)!=package['subject_id']: raise FeedbackError()
    if not re.fullmatch('[0-9a-f]{64}',binding.fictional_manifest_digest): raise FeedbackError()
    if type(binding.independent_record_digests) is not tuple: raise FeedbackError()
    approved=set()
    for entry in binding.independent_record_digests:
        exact(entry,('id','sha256')); uid(entry['id'])
        if not isinstance(entry['id'],str): raise FeedbackError()
        if entry['id'] in approved or not isinstance(entry['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',entry['sha256']): raise FeedbackError()
        approved.add(entry['id'])
    expected={x['target_uuid']:x for x in binding.target_map}
    if len(expected)!=len(binding.target_map): raise FeedbackError()
    for t in package['targets']:
        mapped=expected.get(t['id'])
        if not mapped or set(mapped)!= {'source_namespace','media','id','target_uuid'} or mapped['source_namespace']!=t['source_namespace']: raise FeedbackError()
        key=f"{mapped['media']}:{mapped['id']}" if t['source_namespace']=='legacy-fixture-v1' else str(mapped['id'])
        if t['source_key']!=key: raise FeedbackError()
        if t['source_namespace']=='legacy-fixture-v1':
            if mapped['media'] not in ('film','book') or type(mapped['id']) is not int or mapped['id']<1 or t['kind']!='item' or t['parent_id'] is not None or t['title']!=f"Fictional {mapped['media']} {mapped['id']}" or t['category']!=f"Fictional {mapped['media']}": raise FeedbackError()
            expected_id=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',mapped['media'],mapped['id']]).decode()))
            if expected_id!=t['id']: raise FeedbackError()
    return binding.canonical_value()


@bounded_preflight
def _import(*,actor,profile_id,mutation_id,package,binding,family,semantic_payload):
    uid(mutation_id); validate_package(package)
    binding_value=check_binding(binding,profile_id,package)
    digest=envelope_hash(family,profile_id,semantic_payload,binding_value)
    with access_barrier() as barrier:
        if barrier is not installed_coordinator(): raise RecoveryBlocked()
        def preflight():
            profile,grant=authorize(actor,profile_id,'mutate')
            barrier.assert_import_source_policy(package=package,binding=binding)
            old=prior_receipt(actor,mutation_id,profile_id,family,digest)
            if old: return profile,grant,old
            subject=FeedbackSubject.objects.filter(profile=profile).first()
            if subject and str(subject.subject_id)!=package['subject_id']: raise FeedbackError()
            existing=list(FeedbackEvent.objects.filter(profile=profile))
            if existing:
                # Fresh destination only. Stable records may no-op only when the
                # complete portable identity/provenance/reference set matches.
                local=package_for(profile)
                def stable(p):
                    value={k:v for k,v in p.items() if k not in ('package_id','exported_at')}
                    value['targets']=sorted(value['targets'],key=lambda r:r['id'])
                    value['records']=sorted(value['records'],key=lambda r:r['id'])
                    value['restrictions']=sorted(value['restrictions'],key=lambda r:canonical(r))
                    return value
                if stable(local)!=stable(package): raise Conflict()
            current_targets=list(FeedbackTarget.objects.all())
            by_id={str(t.pk):t for t in current_targets}
            by_source={(t.source_namespace,t.source_key):t for t in current_targets}
            for t in package['targets']:
                same=by_id.get(t['id'])
                collision=by_source.get((t['source_namespace'],t['source_key']))
                if collision and str(collision.pk)!=t['id']: raise FeedbackError()
                if same and same.deleted: raise Gone()
                if same and any((str(getattr(same,k)) if k=='parent_id' and getattr(same,k) else getattr(same,k))!=v for k,v in t.items() if k!='id'): raise FeedbackError()
            foreign_ids=set(str(e) for e in FeedbackEvent.objects.exclude(profile=profile).values_list('pk',flat=True))
            for r in package['records']:
                barrier.assert_scope_open(profile.pk,r['target_id'],r['signal'],r['scope'])
                if r['id'] in foreign_ids: raise FeedbackError()
            return profile,grant,None
        with immediate():
            profile,grant,old=preflight()
            if old: return ImportReport('accepted',None)
        pending=None
        if package['restrictions']:
            restrictions=[dict(kind='erase',profile_id=profile.pk,target_id=r['target_id'],signal=s,
                scope=r['scope'],subject_id=None,restriction_code='erase_feedback')
                for r in package['restrictions'] for s in r['signals']]
            pending=barrier.prepare(mutation_id,restrictions,audit=(actor.pk,grant.pk))
        for attempt in range(3):
            try:
                with immediate():
                    # Pending belongs to this already-authorized import. Ordinary
                    # requests remain denied; trusted local phase checks DB actor
                    # and grants directly without bypassing any request gate.
                    if pending:
                        from django.contrib.auth import get_user_model
                        from shelf.models import Grant,Profile
                        if not get_user_model().objects.filter(pk=actor.pk,is_active=True).exists() or not Profile.objects.filter(pk=profile.pk,active=True).exists() or not Grant.objects.filter(pk=grant.pk,can_view=True,can_mutate=True).exists(): raise RecoveryBlocked()
                        barrier.assert_import_source_policy(package=package,binding=binding)
                    else:
                        profile,grant,old=preflight()
                        if old: return ImportReport('accepted',None)
                    FeedbackSubject.objects.get_or_create(profile=profile,defaults={'subject_id':package['subject_id']})
                    existing_targets=set(str(t) for t in FeedbackTarget.objects.values_list('pk',flat=True))
                    FeedbackTarget.objects.bulk_create([FeedbackTarget(**t) for t in sorted(package['targets'],key=lambda t:t['parent_id'] is not None) if t['id'] not in existing_targets],batch_size=300)
                    now=timezone.now(); additions=[]
                    existing_ids=set(str(e) for e in FeedbackEvent.objects.filter(profile=profile).values_list('pk',flat=True))
                    for r in package['records']:
                        if r['id'] in existing_ids: continue
                        additions.append(FeedbackEvent(id=r['id'],profile=profile,target_id=r['target_id'],signal=r['signal'],scope=r['scope'],revision=r['revision'],operation=r['operation'],actor_id=actor.pk,authority_grant_id=grant.pk,
                            recorded_at=now,origin='imported_statement',original_origin=r['original_origin'],
                            attribution=r['attribution'],original_recorded_at=timestamp(r['original_recorded_at']),
                            source_time=timestamp(r['source_time']),source_record_id=r['source_record_id'],source_metadata=r['source_metadata'],
                            reverses_id=r['reverses_id'],corrects_id=r['corrects_id'],body=r['value'],erased=r['erased']))
                    # Deferred FK checks allow in-batch forward storage order;
                    # all references have already been fully validated.
                    FeedbackEvent.objects.bulk_create(additions,batch_size=300)
                    if not existing_ids:
                        groups={}
                        for r in package['records']: groups.setdefault((r['target_id'],r['signal'],r['scope']),[]).append(r)
                        projections=[]
                        for (target,signal,scope),group in groups.items():
                            group.sort(key=lambda r:r['revision'])
                            reversed_ids={r['reverses_id'] for r in group if r['operation']=='reverse'}
                            erased=any(r['erased'] for r in group)
                            effective=next((r for r in reversed(group) if r['operation']=='set' and r['id'] not in reversed_ids),None) if not erased else None
                            projections.append(FeedbackProjection(profile=profile,target_id=target,signal=signal,scope=scope,
                                revision=group[-1]['revision'],effective_event_id=effective['id'] if effective else None,
                                value=effective['value'] if effective else None,erased=erased))
                        FeedbackProjection.objects.bulk_create(projections,batch_size=300)
                    MutationReceipt.objects.create(actor_id=actor.pk,mutation_id=mutation_id,profile_id=profile.pk,
                        operation_family=family,payload_sha256=None if pending else digest,state='retired' if pending else 'accepted',created_at=now)
                    if pending:
                        from shelf.recovery import apply_restriction
                        for r in pending['restrictions']: apply_restriction(connection.connection,pending,r,audit=(actor.pk,grant.pk))
                        barrier.validate_pending_authority(connection.connection)
                    else: authorize(actor,profile.pk,'mutate')
                if pending: barrier.finalize(pending,pending['local_commit_uuid'])
                return ImportReport('accepted',package['counts'])
            except Exception as error:
                if not is_busy(error): raise
                if attempt==2: raise Busy() from None


def import_portable(*,actor,profile_id,mutation_id,package_bytes,binding):
    package=decode_package(package_bytes)
    return _import(actor=actor,profile_id=profile_id,mutation_id=mutation_id,package=package,
        binding=binding,family='import.portable',semantic_payload=package)


def legacy_to_package(fixture,binding):
    exact(fixture,('schema_version','fictional','source_namespace','person','subject_id','labels','interactions','seeds'))
    if fixture['schema_version']!=LEGACY_SCHEMA or fixture['fictional'] is not True or fixture['source_namespace']!='legacy-fixture-v1' or fixture['labels']!=NATIVE_LABELS or fixture['person']!=binding.source_person_id or fixture['subject_id']!=binding.source_subject_id: raise FeedbackError()
    person=fixture['person']; subject=uid(fixture['subject_id'])
    if subject!=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',person]).decode())): raise FeedbackError()
    targets=[]; records=[]; keys=set(); bykey={}
    for row in fixture['interactions']:
        if not isinstance(row,dict) or set(row)-{'person','media','id','state','rating','saved','source_metadata','source_time'} or not {'person','media','id','state','saved'}<=set(row): raise FeedbackError()
        if row['person']!=person or row['media'] not in ('film','book') or type(row['id']) is not int or row['id']<=0: raise FeedbackError()
        key=(row['media'],row['id'])
        if key in keys: raise FeedbackError()
        keys.add(key); validate_snapshot({k:row[k] for k in ('state','rating','saved') if k in row})
        if row['state']=='rated' and 'rating' not in row: raise FeedbackError()
        target=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',*key]).decode()))
        bykey[key]=target
        targets.append(dict(id=target,kind='item',parent_id=None,title=f'Fictional {key[0]} {key[1]}',category=f'Fictional {key[0]}',source_namespace='legacy-fixture-v1',source_key=f'{key[0]}:{key[1]}'))
        bodies={'legacy_state':{'value':row['state']},'saved':{'value':row['saved']}}
        if 'rating' in row: bodies['legacy_rating']=None if row['rating'] is None else dict(value=row['rating'],basis='legacy_unknown',scale='legacy-native-1-5',labels=NATIVE_LABELS)
        for signal,body in bodies.items():
            records.append(dict(id=str(uuid.uuid5(NAMESPACE,canonical([person,*key,signal,'snapshot']).decode())),target_id=target,signal=signal,scope='item',revision=1,operation='set',value=body,
                source_record_id=canonical([person,*key]).decode(),source_time=row.get('source_time'),source_metadata=row.get('source_metadata'),original_origin='legacy_snapshot',attribution={'subject_id':subject},original_recorded_at=None,reverses_id=None,corrects_id=None,erased=False))
    for index,seed in enumerate(fixture['seeds']):
        exact(seed,('media','id','text','attribution')); text(seed['text'],500); text(seed['attribution'],160)
        target=bykey.get((seed['media'],seed['id']))
        if not target: raise FeedbackError()
        records.append(dict(id=str(uuid.uuid5(NAMESPACE,canonical([person,seed['media'],seed['id'],'seed_statement',index]).decode())),target_id=target,signal='seed_statement',scope='item',revision=1,operation='set',value=dict(text=seed['text'],attribution=seed['attribution'],basis='attributed_statement'),source_record_id=None,source_time=None,source_metadata=None,original_origin='seed_statement',attribution={'subject_id':subject,'label':seed['attribution']},original_recorded_at=None,reverses_id=None,corrects_id=None,erased=False))
    package=dict(schema_version=SCHEMA,package_id=str(uuid.uuid5(NAMESPACE,hashlib.sha256(canonical(fixture)).hexdigest())),fictional=True,exported_at='2000-01-01T00:00:00+00:00',subject_id=subject,targets=targets,records=records,scales=SCALES,counts={},restrictions=[])
    package['counts']=document_counts(package)
    return validate_package(package)


def import_legacy_fixture(*,actor,profile_id,mutation_id,fixture_bytes,binding):
    fixture=strict_json(fixture_bytes)
    if not isinstance(binding,ValidatedImportBinding): raise FeedbackError()
    package=legacy_to_package(fixture,binding)
    return _import(actor=actor,profile_id=profile_id,mutation_id=mutation_id,package=package,
        binding=binding,family='import.legacy',semantic_payload=fixture)
