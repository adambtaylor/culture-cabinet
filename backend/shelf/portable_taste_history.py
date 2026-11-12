"""Reference-backed native taste history; never a backup or provider capability."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction

from . import grant_authority as ga
from .card_actions import history_digest
from .feedback import reduce_events
from .models import FeedbackEvent, FeedbackSubject, FeedbackProjection, Grant, PortableJourneyCommand
from .portable_taste_inputs import NATIVE_NAMESPACES

SIGNALS=('interest','saved','enjoyment','note')
def _coverage():
    return {'schema':'native-taste-coverage/v1','signals':list(SIGNALS),'scope':'item',
            'attribution':'current-owner-subject-direct','maximum_streams':500}
COVERAGE_TEXT='Native owner interest, favourites, experienced enjoyment and notes. Other history and settings stay current.'
NAMESPACES=(*NATIVE_NAMESPACES,'enhance-current')


def _uuid(value):return str(ga._uuid(value))


def _digest(value):return history_digest(value)


def _sha(value):
    ga._require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None,'CONTRACT_INVALID',422)
    return value


def _supported(cabinet):
    """Reduce authentic complete prefixes, including currently cleared streams."""
    subject=FeedbackSubject.objects.filter(profile_id=cabinet.profile_id).first()
    groups={}
    events=FeedbackEvent.objects.filter(profile_id=cabinet.profile_id,scope='item',signal__in=SIGNALS,
        target__source_namespace__in=NAMESPACES).select_related('target').order_by('target_id','signal','revision')
    for event in events:groups.setdefault((str(event.target_id),event.signal),[]).append(event)
    ga._require(len(groups)<=500,'LIMIT_EXCEEDED',422)
    result={};complete=True
    for key,history in groups.items():
        target=history[-1].target
        authentic=(subject is not None and str(subject.subject_id)==str(cabinet.subject_ref)
            and all(event.revision==index+1 and event.actor_id==cabinet.actor_id
                and event.origin==event.original_origin=='direct'
                and event.attribution=={'subject_id':str(cabinet.subject_ref)}
                and Grant.objects.filter(pk=event.authority_grant_id,user_id=cabinet.actor_id,
                                         profile_id=cabinet.profile_id).exists()
                for index,event in enumerate(history)))
        hidden=FeedbackProjection.objects.filter(profile_id=cabinet.profile_id,target=target,
            signal='visibility',scope='item',value={'value':'hidden'}).exists()
        _,body,erased=reduce_events(history)
        # Unavailable coverage never becomes an absent historical preference.
        # Erased/hidden/deleted streams cannot be restored or displayed.
        local_identity=(target.source_namespace=='enhance-current' or
            target.source_key.startswith(str(cabinet.profile_ref)+':'))
        available=authentic and local_identity and target.kind=='item' and not target.deleted and not hidden and not erased
        complete=complete and available
        result[key]={'history':history,'target':target,'body':body if available else None,
                     'available':available}
    return result,complete


def _vector(streams):
    return [[target,signal,'item',entry['history'][-1].revision,str(entry['history'][-1].pk)]
        for (target,signal),entry in sorted(streams.items())]


def _state_digest(streams):return _digest(_vector(streams))


def _semantic(streams):
    # Explicit null clears and a not-yet-created stream have the same supported
    # semantic value. Revisions still remain in the separate command CAS digest.
    return {(target,signal):entry['body'] for (target,signal),entry in streams.items()
            if entry['available'] and entry['body'] is not None}


class TasteHistory:
    def __init__(self,authority):
        ga._require(type(authority) is ga.PortableAuthority)
        self.authority=authority

    @contextmanager
    def _scope(self,session_ref,profile_ref):
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref),self.authority._lock:
            cabinet,_=ga._bound(self.authority,session_ref,profile_ref)
            ga._require(cabinet.data_schema_version==2,'CAPABILITY_UNAVAILABLE',503)
            yield cabinet
            ga._bound(self.authority,session_ref,profile_ref)

    def _point(self,cabinet,checkpoint_ref):
        row=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=ga._uuid(checkpoint_ref),
            kind='taste.checkpoint').first()
        ga._require(row is not None,'AUTHORITY_DENIED',403)
        ga._require(row.state=='committed' and row.result.get('schema')=='taste-checkpoint/v1',
                    'HISTORY_UNAVAILABLE',410)
        point=row.result
        ga._require(point.get('checkpoint_ref')==str(row.command_id)
            and point.get('installation_ref')==str(cabinet.installation_id)
            and point.get('actor_ref')==str(cabinet.actor_ref)
            and point.get('profile_ref')==str(cabinet.profile_ref)
            and point.get('subject_ref')==str(cabinet.subject_ref)
            and point.get('coverage')==_coverage()
            and point.get('digest')==_digest({key:value for key,value in point.items() if key!='digest'}),
            'HISTORY_UNAVAILABLE',410)
        return row,point

    def _reconstruct(self,cabinet,point,current):
        values={};heads=point['stream_heads']
        ga._require(type(heads) is list and len(heads)<=500 and point['complete'] is True,
                    'HISTORY_UNAVAILABLE',410)
        previous=None
        for head in heads:
            ga._require(type(head) is list and len(head)==5,'HISTORY_UNAVAILABLE',410)
            target,signal,scope,revision,event_ref=head
            key=(_uuid(target),signal)
            ga._require(signal in SIGNALS and scope=='item' and type(revision) is int and revision>0
                and (previous is None or previous<key),'HISTORY_UNAVAILABLE',410)
            previous=key
            entry=current.get(key)
            ga._require(entry is not None and entry['available'],'HISTORY_UNAVAILABLE',410)
            prefix=[event for event in entry['history'] if event.revision<=revision]
            ga._require(len(prefix)==revision and str(prefix[-1].pk)==_uuid(event_ref),
                        'HISTORY_UNAVAILABLE',410)
            _,body,erased=reduce_events(prefix)
            ga._require(not erased,'HISTORY_UNAVAILABLE',410)
            values[key]=body
        return values

    def capture(self,session_ref,*,profile_ref,origin_command_ref=None,origin_run_ref=None):
        """Actual complete vectors only; a GET or empty old run cannot backfill one."""
        from .models import MutationReceipt,SynthesisRun
        profile_ref=_uuid(profile_ref)
        ga._require(origin_command_ref is not None or origin_run_ref is not None,'CONTRACT_INVALID',422)
        if origin_command_ref is not None:origin_command_ref=_uuid(origin_command_ref)
        if origin_run_ref is not None:origin_run_ref=_uuid(origin_run_ref)
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet,transaction.atomic():
            streams,complete=_supported(cabinet);vector=_vector(streams)
            ga._require(complete,'HISTORY_UNAVAILABLE',410)
            if origin_command_ref is not None:
                ga._require(PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=origin_command_ref,
                    state='committed',kind__in=['card.action','card.undo','journey.favourite','taste.restore','taste.undo']).exists()
                    or MutationReceipt.objects.filter(actor_id=cabinet.actor_id,profile_id=cabinet.profile_id,
                        mutation_id=origin_command_ref,state='accepted').exists(),'AUTHORITY_DENIED',403)
            if origin_run_ref is not None:
                ga._require(SynthesisRun.objects.filter(pk=origin_run_ref,actor_id=cabinet.actor_id,
                    profile_id=cabinet.profile_id,installation_id=cabinet.installation_id).exists(),
                    'AUTHORITY_DENIED',403)
            descriptor={'profile_ref':profile_ref,'vector_digest':_digest(vector)}
            for prior in PortableJourneyCommand.objects.filter(cabinet=cabinet,kind='taste.checkpoint',
                    state='committed').order_by('-created_at','-id'):
                if prior.descriptor==descriptor:
                    _,point=self._point(cabinet,str(prior.command_id))
                    ga._bound(self.authority,session_ref,profile_ref)
                    return point
            reference=str(uuid.uuid4())
            point={'schema':'taste-checkpoint/v1','checkpoint_ref':reference,
                'installation_ref':str(cabinet.installation_id),'actor_ref':str(cabinet.actor_ref),
                'profile_ref':profile_ref,'subject_ref':str(cabinet.subject_ref),
                'captured_at':ga._stamp(self.authority.now()),'origin_command_ref':origin_command_ref,
                'origin_run_ref':origin_run_ref,'coverage':_coverage(),'complete':True,'stream_heads':vector}
            point['digest']=_digest(point)
            actor=get_user_model().objects.get(pk=cabinet.actor_id)
            with ga.writer_window(actor,cabinet.profile_id):
                PortableJourneyCommand.objects.create(cabinet=cabinet,command_id=reference,kind='taste.checkpoint',
                    descriptor=descriptor,descriptor_digest=_digest(descriptor),state='committed',result=point,
                    proof={},created_at=self.authority.now())
            return point

    def _display_point(self,point,*,current=False,complete=True):
        return {'checkpoint_ref':point['checkpoint_ref'],'profile_ref':point['profile_ref'],
            'captured_at':point['captured_at'],'origin_label':'Before Enhance' if point['origin_run_ref'] else
                'Recorded native taste choices','current':current,'complete':complete,'coverage_text':COVERAGE_TEXT}

    def compare(self,session_ref,*,profile_ref,checkpoint_ref):
        with self._scope(session_ref,profile_ref) as cabinet:
            _,point=self._point(cabinet,checkpoint_ref)
            streams,complete=_supported(cabinet)
            historical=self._reconstruct(cabinet,point,streams)
            ga._require(complete,'HISTORY_UNAVAILABLE',410)
            changes=[]
            for key in sorted(set(historical)|set(streams)):
                entry=streams[key];before=historical.get(key);now=entry['body']
                if before==now:continue
                target,signal=key
                label=entry['target'].title if entry['target'].source_namespace in NATIVE_NAMESPACES else \
                    'Previously recommended item (source title unavailable)'
                def value(body):return None if body is None else body['text'] if signal=='note' else body['value']
                changes.append({'target_ref':target,'target_label':label,'signal':signal,
                    'historical_value':value(before),'current_value':value(now)})
            matching={key:value for key,value in historical.items() if value is not None}==_semantic(streams)
            restore_available=True
            try:
                self._mutator(cabinet);self._no_attempt(cabinet)
            except ga.AuthorityFault:
                restore_available=False
            return {'profile_ref':str(cabinet.profile_ref),'checkpoint':self._display_point(point,current=matching),
                'changes':changes,'current_state_digest':_state_digest(streams),'checkpoint_digest':point['digest'],
                'timezone_name':'Australia/Melbourne','restore_available':restore_available}

    def index(self,session_ref,*,profile_ref,cursor=None):
        from django.db.models import Q
        with self._scope(session_ref,profile_ref) as cabinet:
            streams,complete=_supported(cabinet)
            rows=PortableJourneyCommand.objects.filter(cabinet=cabinet,kind='taste.checkpoint')
            current_ref=None
            if complete:
                # Exact current heads win even when wall timestamps tie (for
                # example Undo back to an older semantic state). Otherwise use
                # the newest still-reconstructable semantic match, never a run
                # timestamp or an empty legacy input_snapshot.
                exact=rows.filter(state='committed',descriptor__vector_digest=_state_digest(streams)).order_by(
                    '-created_at','-id').first()
                if exact is not None:
                    _,point=self._point(cabinet,str(exact.command_id))
                    self._reconstruct(cabinet,point,streams)
                    current_ref=str(exact.command_id)
                else:
                    for row in rows.filter(state='committed').order_by('-created_at','-id').iterator(chunk_size=20):
                        try:
                            _,point=self._point(cabinet,str(row.command_id))
                            historical=self._reconstruct(cabinet,point,streams)
                        except ga.AuthorityFault:continue
                        if {key:value for key,value in historical.items() if value is not None}==_semantic(streams):
                            current_ref=str(row.command_id);break
            if cursor is not None:
                boundary=rows.filter(command_id=ga._uuid(cursor)).first()
                ga._require(boundary is not None,'AUTHORITY_DENIED',403)
                rows=rows.filter(Q(created_at__lt=boundary.created_at)|Q(created_at=boundary.created_at,id__lt=boundary.pk))
            page=list(rows.order_by('-created_at','-id')[:21]);points=[]
            for row in page[:20]:
                try:
                    _,point=self._point(cabinet,str(row.command_id))
                    self._reconstruct(cabinet,point,streams)
                    points.append(self._display_point(point,current=str(row.command_id)==current_ref))
                except ga.AuthorityFault:
                    points.append({'checkpoint_ref':str(row.command_id),'profile_ref':str(cabinet.profile_ref),
                        'captured_at':ga._stamp(row.created_at),'origin_label':'Checkpoint unavailable',
                        'current':False,'complete':False,'coverage_text':'Current restrictions or missing history prevent reconstruction.'})
            return {'profile_ref':str(cabinet.profile_ref),'checkpoints':points,'timezone_name':'Australia/Melbourne',
                'next_cursor':str(page[19].command_id) if len(page)>20 else None,
                'current_state_digest':_state_digest(streams),'coverage_complete':complete}

    def _authority_digest(self,cabinet,session_ref):
        from .models import Profile,RecoveryAnchor,RecoveryRestriction
        cabinet.refresh_from_db()
        material={'installation_ref':str(cabinet.installation_id),'profile_ref':str(cabinet.profile_ref),
            'actor_ref':str(cabinet.actor_ref),'subject_ref':str(cabinet.subject_ref),
            'owner_revision':cabinet.owner_revision,'policy_epoch':cabinet.policy_epoch,
            'restriction_watermark':cabinet.restriction_watermark,
            'session_digest':hashlib.sha256(session_ref.encode()).hexdigest(),
            'process_generation':str(self.authority.boundary.process_generation),
            'profile':list(Profile.objects.filter(pk=cabinet.profile_id).values()),
            'actor_active':get_user_model().objects.get(pk=cabinet.actor_id).is_active,
            'grants':list(Grant.objects.filter(profile_id=cabinet.profile_id,user_id=cabinet.actor_id).order_by('pk').values()),
            'anchors':list(RecoveryAnchor.objects.order_by('pk').values()),
            'restrictions':list(RecoveryRestriction.objects.order_by('pk').values())}
        # ORM revisions contain UUIDs and timestamps; normalize them before the strict JSON codec.
        return _digest(json.loads(json.dumps(material,default=ga._local_default)))

    def _prior(self,cabinet,command_id,kind,descriptor):
        row=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=ga._uuid(command_id)).first()
        if row is not None:
            ga._require(row.kind==kind and row.descriptor==descriptor and row.descriptor_digest==_digest(descriptor),
                        'IDEMPOTENCY_CONFLICT',409)
            ga._require(row.state=='committed','HISTORY_UNAVAILABLE',410)
        return row

    def _record(self,cabinet,command_id,kind,descriptor,result):
        actor=get_user_model().objects.get(pk=cabinet.actor_id)
        with ga.writer_window(actor,cabinet.profile_id):
            return PortableJourneyCommand.objects.create(cabinet=cabinet,command_id=ga._uuid(command_id),kind=kind,
                descriptor=descriptor,descriptor_digest=_digest(descriptor),state='committed',result=result,proof={},
                created_at=self.authority.now())

    def _mutator(self,cabinet):
        from .feedback import authorize,FeedbackError
        from django.http import Http404
        actor=get_user_model().objects.get(pk=cabinet.actor_id)
        try:authorize(actor,cabinet.profile_id,'mutate')
        except (FeedbackError,Http404):raise ga.AuthorityFault('AUTHORITY_DENIED',403) from None
        return actor

    def _no_attempt(self,cabinet):
        from .models import SynthesisRun
        ga._require(not SynthesisRun.objects.filter(profile_id=cabinet.profile_id,actor_id=cabinet.actor_id,
            installation_id=cabinet.installation_id,contract_version='enhance/v1',attempts=1,
            state__in=['running','unknown']).exists(),'UNKNOWN_RECONCILIATION_REQUIRED',409)

    def preview(self,session_ref,*,profile_ref,checkpoint_ref,expected_state_digest,command_id):
        profile_ref=_uuid(profile_ref);checkpoint_ref=_uuid(checkpoint_ref);command_id=_uuid(command_id)
        _sha(expected_state_digest)
        descriptor={'profile_ref':profile_ref,'checkpoint_ref':checkpoint_ref,
                    'expected_state_digest':expected_state_digest}
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet,transaction.atomic():
            prior=self._prior(cabinet,command_id,'taste.preview',descriptor)
            if prior is not None:return self.preview_projection(session_ref,profile_ref=profile_ref,preview_ref=command_id)
            self._mutator(cabinet);self._no_attempt(cabinet)
            _,point=self._point(cabinet,checkpoint_ref)
            streams,complete=_supported(cabinet)
            ga._require(complete and expected_state_digest==_state_digest(streams),'REVISION_CONFLICT',409)
            self._reconstruct(cabinet,point,streams)
            _,session=ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
            end=min(self.authority.now()+timedelta(minutes=5),ga._end(self.authority,session))
            # Only references and binding digests persist. Compare display values
            # are recomputed from original current-permitted events on each read.
            result={'schema':'taste-restore-preview-reference/v1','preview_ref':command_id,
                'checkpoint_ref':checkpoint_ref,'checkpoint_digest':point['digest'],
                'current_state_digest':_state_digest(streams),'authority_binding_digest':self._authority_digest(cabinet,session_ref),
                'expires_at':ga._stamp(end)}
            self._record(cabinet,command_id,'taste.preview',descriptor,result)
        return self.preview_projection(session_ref,profile_ref=profile_ref,preview_ref=command_id)

    def preview_projection(self,session_ref,*,profile_ref,preview_ref):
        with self._scope(session_ref,profile_ref) as cabinet:
            row=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=ga._uuid(preview_ref),
                kind='taste.preview',state='committed').first()
            ga._require(row is not None,'HISTORY_UNAVAILABLE',410)
            meta=row.result
            ga._require(meta.get('schema')=='taste-restore-preview-reference/v1','HISTORY_UNAVAILABLE',410)
            end=datetime.fromisoformat(meta['expires_at'].replace('Z','+00:00'))
            ga._require(self.authority.now()<end,'PREVIEW_STALE',410)
            comparison=self.compare(session_ref,profile_ref=profile_ref,checkpoint_ref=meta['checkpoint_ref'])
            ga._require(comparison['checkpoint_digest']==meta['checkpoint_digest']
                and comparison['current_state_digest']==meta['current_state_digest']
                and self._authority_digest(cabinet,session_ref)==meta['authority_binding_digest'],
                'REVISION_CONFLICT',409)
            self._mutator(cabinet);self._no_attempt(cabinet)
            preview={'schema':'taste-restore-preview/v1',**{key:meta[key] for key in (
                'preview_ref','checkpoint_ref','checkpoint_digest','current_state_digest','authority_binding_digest','expires_at')},
                'changes':comparison['changes'],'coverage':_coverage(),'restore_available':True}
            preview['preview_digest']=_digest(meta)
            return preview

    def restore(self,session_ref,*,profile_ref,preview_ref,preview_digest,command_id,confirmed):
        from .feedback import MutationCommand,mutate_in_transaction,immediate,FeedbackError,Conflict
        from .recovery import access_barrier
        profile_ref=_uuid(profile_ref);preview_ref=_uuid(preview_ref);command_id=_uuid(command_id)
        _sha(preview_digest)
        ga._require(confirmed=='restore_taste_choices','CONTRACT_INVALID',422)
        descriptor={'profile_ref':profile_ref,'preview_ref':preview_ref,'preview_digest':preview_digest,
                    'confirmed':confirmed}
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            prior=self._prior(cabinet,command_id,'taste.restore',descriptor)
            if prior is not None:return prior.result
            actor=self._mutator(cabinet);self._no_attempt(cabinet)
            with access_barrier() as barrier,immediate(),ga.writer_window(actor,cabinet.profile_id):
                preview=self.preview_projection(session_ref,profile_ref=profile_ref,preview_ref=preview_ref)
                meta=PortableJourneyCommand.objects.get(cabinet=cabinet,command_id=preview_ref).result
                ga._require(_digest(meta)==preview_digest,'REVISION_CONFLICT',409)
                _,point=self._point(cabinet,preview['checkpoint_ref'])
                streams,complete=_supported(cabinet)
                ga._require(complete and _state_digest(streams)==preview['current_state_digest'],
                            'REVISION_CONFLICT',409)
                before=_vector(streams)
                desired=self._reconstruct(cabinet,point,streams)
                children=[]
                for key in sorted(set(streams)|set(desired)):
                    entry=streams[key];value=desired.get(key)
                    if entry['body']==value:continue
                    target,signal=key;last=entry['history'][-1]
                    command=MutationCommand(str(uuid.uuid5(ga._uuid(command_id),target+':'+signal)),
                        target,'item',signal,'item','set',last.revision,str(last.pk),value)
                    try:
                        receipt=mutate_in_transaction(actor=actor,profile_id=cabinet.profile_id,
                                                       command=command,barrier=barrier)
                    except Conflict:raise ga.AuthorityFault('REVISION_CONFLICT',409) from None
                    except FeedbackError:raise ga.AuthorityFault('AUTHORITY_DENIED',403) from None
                    children.append(receipt.event_id)
                # The same IMMEDIATE transaction owns all new statements and
                # their parent. Never reinstall an old projection or old grants.
                cabinet,_=ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
                self._mutator(cabinet)
                after,complete=_supported(cabinet);ga._require(complete,'HISTORY_UNAVAILABLE',410)
                result={'schema':'taste-restore-receipt/v1','restore_command_ref':command_id,
                    'profile_ref':profile_ref,'checkpoint_ref':preview['checkpoint_ref'],
                    'before_vector':before,'after_vector':_vector(after),'child_event_refs':children,
                    'after_state_digest':_state_digest(after),'authority_after_digest':self._authority_digest(cabinet,session_ref),
                    'undo_available':bool(children),'outcome':'restored' if children else 'unchanged'}
                self._record(cabinet,command_id,'taste.restore',descriptor,result)
            self.capture(session_ref,profile_ref=profile_ref,origin_command_ref=command_id)
            return result

    def undo(self,session_ref,*,profile_ref,restore_command_ref,expected_state_digest,command_id,confirmed):
        from .feedback import MutationCommand,mutate_in_transaction,immediate,FeedbackError,Conflict
        from .models import MutationReceipt
        from .recovery import access_barrier
        profile_ref=_uuid(profile_ref);restore_command_ref=_uuid(restore_command_ref);command_id=_uuid(command_id)
        _sha(expected_state_digest)
        ga._require(confirmed=='undo_taste_restore','CONTRACT_INVALID',422)
        descriptor={'profile_ref':profile_ref,'restore_command_ref':restore_command_ref,
                    'expected_state_digest':expected_state_digest,'confirmed':confirmed}
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            prior=self._prior(cabinet,command_id,'taste.undo',descriptor)
            if prior is not None:return prior.result
            actor=self._mutator(cabinet);self._no_attempt(cabinet)
            original=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=restore_command_ref,
                kind='taste.restore',state='committed').first()
            ga._require(original is not None and original.result.get('undo_available') is True,'REVISION_CONFLICT',409)
            with access_barrier() as barrier,immediate(),ga.writer_window(actor,cabinet.profile_id):
                streams,complete=_supported(cabinet);before=_vector(streams)
                ga._require(complete and _state_digest(streams)==expected_state_digest==original.result['after_state_digest']
                    and before==original.result['after_vector']
                    and self._authority_digest(cabinet,session_ref)==original.result['authority_after_digest'],
                    'REVISION_CONFLICT',409)
                children=[]
                for reference in original.result['child_event_refs']:
                    event=FeedbackEvent.objects.filter(pk=ga._uuid(reference),profile_id=cabinet.profile_id,
                        actor_id=cabinet.actor_id,scope='item',signal__in=SIGNALS,operation='set',erased=False).first()
                    ga._require(event is not None,'REVISION_CONFLICT',409)
                    key=(str(event.target_id),event.signal);entry=streams.get(key)
                    ga._require(entry is not None and entry['available'] and entry['history'][-1].pk==event.pk,
                                'REVISION_CONFLICT',409)
                    child_id=str(uuid.uuid5(ga._uuid(restore_command_ref),key[0]+':'+key[1]))
                    ga._require(MutationReceipt.objects.filter(actor=actor,profile_id=cabinet.profile_id,
                        mutation_id=child_id,event_id=event.pk,state='accepted',operation_family='feedback.set',
                        accepted_revision=event.revision).exists(),'REVISION_CONFLICT',409)
                    command=MutationCommand(str(uuid.uuid5(ga._uuid(command_id),key[0]+':'+key[1])),key[0],
                        'item',key[1],'item','reverse',event.revision,str(event.pk),None,str(event.pk))
                    try:
                        receipt=mutate_in_transaction(actor=actor,profile_id=cabinet.profile_id,
                                                       command=command,barrier=barrier)
                    except Conflict:raise ga.AuthorityFault('REVISION_CONFLICT',409) from None
                    except FeedbackError:raise ga.AuthorityFault('AUTHORITY_DENIED',403) from None
                    children.append(receipt.event_id)
                cabinet,_=ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
                self._mutator(cabinet)
                after,complete=_supported(cabinet);ga._require(complete,'HISTORY_UNAVAILABLE',410)
                result={'schema':'taste-undo-receipt/v1','undo_command_ref':command_id,
                    'restore_command_ref':restore_command_ref,'profile_ref':profile_ref,
                    'checkpoint_ref':original.result['checkpoint_ref'],'before_vector':before,
                    'after_vector':_vector(after),'child_event_refs':children,'after_state_digest':_state_digest(after),
                    'authority_after_digest':self._authority_digest(cabinet,session_ref),'outcome':'undone'}
                self._record(cabinet,command_id,'taste.undo',descriptor,result)
            self.capture(session_ref,profile_ref=profile_ref,origin_command_ref=command_id)
            return result


def validate_history_command(raw,row,descriptor,result,cabinet):
    """Closed restore grammar: metadata may reference owned history, never copy it.

    This validates retained structure only. Live reads still recheck current
    authority and restrictions; an isolated restore cannot revive a preview.
    """
    def require(condition):
        if not condition:raise ValueError('Invalid taste history metadata')
    def reference(value):
        require(type(value) is str and str(uuid.UUID(value))==value)
        return uuid.UUID(value).hex
    def digest(value):require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None)
    def vector(value):
        require(type(value) is list and len(value)<=500)
        previous=None
        for head in value:
            require(type(head) is list and len(head)==5)
            target,signal,scope,revision,event=head
            target_id=reference(target);event_id=reference(event);key=(target,signal)
            require(signal in SIGNALS and scope=='item' and type(revision) is int and revision>0
                    and (previous is None or previous<key))
            previous=key
            require(raw.execute('SELECT profile_id,actor_id,target_id,signal,scope,revision FROM shelf_feedbackevent WHERE id=?',
                (event_id,)).fetchone()==(cabinet[5],cabinet[3],target_id,signal,'item',revision))
    command=str(uuid.UUID(row['command_id']));profile=str(uuid.UUID(cabinet[1]));kind=row['kind']
    descriptor_fields={
        'taste.checkpoint':{'profile_ref','vector_digest'},
        'taste.preview':{'profile_ref','checkpoint_ref','expected_state_digest'},
        'taste.restore':{'profile_ref','preview_ref','preview_digest','confirmed'},
        'taste.undo':{'profile_ref','restore_command_ref','expected_state_digest','confirmed'}}
    require(kind in descriptor_fields and set(descriptor)==descriptor_fields[kind]
            and descriptor['profile_ref']==profile and row['state'] in ('committed','retired'))
    for key,value in descriptor.items():
        if key.endswith('_ref'):reference(value)
        elif key.endswith('_digest'):digest(value)
    if kind=='taste.restore':require(descriptor['confirmed']=='restore_taste_choices')
    if kind=='taste.undo':require(descriptor['confirmed']=='undo_taste_restore')
    if row['state']=='retired':
        require(result=={})
        return
    if kind=='taste.checkpoint':
        require(set(result)=={'schema','checkpoint_ref','installation_ref','actor_ref','profile_ref','subject_ref',
            'captured_at','origin_command_ref','origin_run_ref','coverage','complete','stream_heads','digest'})
        require(result['schema']=='taste-checkpoint/v1' and result['checkpoint_ref']==command
            and result['installation_ref']==str(uuid.UUID(cabinet[2])) and result['actor_ref']==str(uuid.UUID(cabinet[4]))
            and result['profile_ref']==profile and result['coverage']==_coverage() and result['complete'] is True)
        subject=raw.execute('SELECT subject_ref FROM shelf_profilecabinetstate WHERE profile_id=?',(cabinet[5],)).fetchone()
        require(subject is not None and result['subject_ref']==str(uuid.UUID(subject[0])))
        vector(result['stream_heads']);digest(result['digest'])
        require(result['digest']==_digest({key:value for key,value in result.items() if key!='digest'})
            and descriptor['vector_digest']==_digest(result['stream_heads']))
        require(result['origin_command_ref'] is not None or result['origin_run_ref'] is not None)
        for key in ('origin_command_ref','origin_run_ref'):
            if result[key] is not None:reference(result[key])
        stamp=datetime.fromisoformat(result['captured_at'].replace('Z','+00:00'))
        require(stamp.tzinfo is not None)
    elif kind=='taste.preview':
        require(set(result)=={'schema','preview_ref','checkpoint_ref','checkpoint_digest','current_state_digest',
            'authority_binding_digest','expires_at'} and result['schema']=='taste-restore-preview-reference/v1'
            and result['preview_ref']==command and result['checkpoint_ref']==descriptor['checkpoint_ref']
            and result['current_state_digest']==descriptor['expected_state_digest'])
        for key in ('checkpoint_digest','current_state_digest','authority_binding_digest'):digest(result[key])
        require(datetime.fromisoformat(result['expires_at'].replace('Z','+00:00')).tzinfo is not None)
    else:
        restore=kind=='taste.restore'
        fields={'schema','profile_ref','checkpoint_ref','before_vector','after_vector','child_event_refs',
                'after_state_digest','authority_after_digest','outcome','restore_command_ref'}
        fields.add('undo_available' if restore else 'undo_command_ref')
        require(set(result)==fields and result['profile_ref']==profile
                and result['schema']==('taste-restore-receipt/v1' if restore else 'taste-undo-receipt/v1'))
        require(result['restore_command_ref']==(command if restore else descriptor['restore_command_ref']))
        if restore:
            require(type(result['undo_available']) is bool and result['outcome'] in ('restored','unchanged'))
        else:require(result['undo_command_ref']==command and result['outcome']=='undone')
        reference(result['checkpoint_ref']);vector(result['before_vector']);vector(result['after_vector'])
        digest(result['after_state_digest']);digest(result['authority_after_digest'])
        require(result['after_state_digest']==_digest(result['after_vector']))
        children=result['child_event_refs']
        require(type(children) is list and len(children)<=500 and len(children)==len(set(children)))
        if restore:require(result['undo_available']==bool(children))
        after={tuple(head[:2]):head for head in result['after_vector']}
        for child in children:
            event_id=reference(child)
            event=raw.execute('SELECT target_id,signal,operation,revision FROM shelf_feedbackevent WHERE id=? AND profile_id=? AND actor_id=?',
                (event_id,cabinet[5],cabinet[3])).fetchone()
            require(event is not None and event[1] in SIGNALS and event[2]==('set' if restore else 'reverse'))
            target=str(uuid.UUID(event[0]));head=after.get((target,event[1]))
            require(head is not None and head[3:]==[event[3],child])
            mutation=uuid.uuid5(uuid.UUID(command),target+':'+event[1]).hex
            receipt=raw.execute('SELECT event_id,state,operation_family,accepted_revision FROM shelf_mutationreceipt WHERE profile_id=? AND actor_id=? AND mutation_id=?',
                (cabinet[5],cabinet[3],mutation)).fetchone()
            require(receipt==(event_id,'accepted','feedback.'+event[2],event[3]))


def capture_native_mutation(actor,profile_id,command):
    """Observe a committed native single-child input, never a batch midpoint."""
    context=ga._CURRENT_WRITER.get()
    if context is None or context[3:5]!=(actor.pk,profile_id) or command.signal not in SIGNALS:return
    authority,session,profile=context[:3]
    cabinet,_=ga._bound(authority,session,profile)
    event=FeedbackEvent.objects.filter(profile_id=profile_id,actor=actor,
        target_id=command.target_id,signal=command.signal,scope='item').select_related('target').first()
    if event is None or event.target.source_namespace not in NAMESPACES:return
    # The guided parent captures only after its progress/slot commit. Its child
    # can already be durable during crash recovery without claiming a point.
    if PortableJourneyCommand.objects.filter(cabinet=cabinet,kind='journey.favourite',
            state='reserved',descriptor__mutation_id=str(command.mutation_id)).exists():return
    capture_committed(authority,session,profile,origin_command_ref=str(command.mutation_id))


def capture_committed(authority,session,profile,**origin):
    try:return TasteHistory(authority).capture(session,profile_ref=profile,**origin)
    except ga.AuthorityFault as error:
        # An erased or unavailable prefix cannot become a complete checkpoint.
        # Keep the accepted input receipt; report the missing point via history.
        if error.code!='HISTORY_UNAVAILABLE':raise
        return None
