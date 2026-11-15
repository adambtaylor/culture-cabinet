"""Closed native action commands; one parent owns both typed feedback children."""
from dataclasses import asdict, dataclass
import uuid

from . import grant_authority as ga

SCHEMA = 'native-card-actions/v1'
ACTIONS = ('like', 'dislike', 'favourite', 'obtain')
LABELS = {'like': 'Like', 'dislike': "Don't like", 'favourite': 'Favourite', 'obtain': 'Obtain'}
DIRECTIONS = dict(zip(ACTIONS, ('right', 'left', 'up', 'down')))
ACTION_FIELDS = ('profile_ref', 'publication_ref', 'publication_revision', 'target_ref',
                 'interest_revision', 'saved_revision', 'command_id', 'action')
UNDO_FIELDS = ('profile_ref', 'original_command_id', 'command_id', 'interest_revision', 'saved_revision')
RECEIPT_FIELDS = ('command_id', 'target_ref', 'requested_action', 'preference_outcome', 'selected_state',
                  'interest_revision', 'saved_revision', 'child_event_refs', 'undo_available',
                  'ranking_state', 'obtain', 'next_action')


def revision(value, *, positive=False):
    ga._require(type(value) is int and int(positive) <= value <= 2**53-1, 'CONTRACT_INVALID', 422)
    return value


@dataclass(frozen=True)
class CardAction:
    profile_ref: str
    publication_ref: str
    publication_revision: int
    target_ref: str
    interest_revision: int
    saved_revision: int
    command_id: str
    action: str

    def __post_init__(self):
        for key in ('profile_ref', 'publication_ref', 'target_ref', 'command_id'):
            value = getattr(self, key)
            ga._require(type(value) is str and str(ga._uuid(value)) == value, 'CONTRACT_INVALID', 422)
        revision(self.publication_revision, positive=True)
        revision(self.interest_revision); revision(self.saved_revision)
        ga._require(type(self.action) is str and self.action in ACTIONS, 'CONTRACT_INVALID', 422)

    @classmethod
    def parse(cls, value):
        ga._require(type(value) is dict and set(value) == set(ACTION_FIELDS), 'CONTRACT_INVALID', 422)
        return cls(**value)

    def descriptor(self):
        return {key: value for key, value in asdict(self).items() if key != 'command_id'}


@dataclass(frozen=True)
class CardUndo:
    profile_ref: str
    original_command_id: str
    command_id: str
    interest_revision: int
    saved_revision: int

    def __post_init__(self):
        for key in ('profile_ref', 'original_command_id', 'command_id'):
            value = getattr(self, key)
            ga._require(type(value) is str and str(ga._uuid(value)) == value, 'CONTRACT_INVALID', 422)
        revision(self.interest_revision); revision(self.saved_revision)

    @classmethod
    def parse(cls, value):
        ga._require(type(value) is dict and set(value) == set(UNDO_FIELDS), 'CONTRACT_INVALID', 422)
        return cls(**value)

    def descriptor(self):
        return {key: value for key, value in asdict(self).items() if key != 'command_id'}


def desired_values(action):
    ga._require(type(action) is str and action in ACTIONS, 'CONTRACT_INVALID', 422)
    # Obtain is exactly the favourite transition. Acquisition navigation never
    # manufactures a third preference tier or a second saved event.
    return ({'interest': {'value': 'yes' if action == 'like' else 'no', 'basis': 'anticipated'}, 'saved': None}
            if action in ('like', 'dislike') else {'interest': None, 'saved': {'value': True}})


def selected_state(values):
    if values.get('saved') == {'value': True}:
        return 'favourite'
    interest = values.get('interest')
    return ('like' if interest == {'value': 'yes', 'basis': 'anticipated'} else
            'dislike' if interest == {'value': 'no', 'basis': 'anticipated'} else 'none')


def action_forms(*, profile_ref, publication_ref, publication_revision, target_ref,
                 interest_revision, saved_revision, enabled=True, disabled_reason='', obtain_state='unavailable'):
    rows = []
    command_id = str(uuid.uuid4())
    for action in ACTIONS:
        command = CardAction(profile_ref, publication_ref, publication_revision, target_ref,
                             interest_revision, saved_revision, command_id, action)
        rows.append({'action': action, 'label': LABELS[action], 'direction': DIRECTIONS[action], 'enabled': enabled,
            'disabled_reason': disabled_reason, 'form_action': '/cabinet/actions/',
            'fields': asdict(command), 'obtain_state': obtain_state if action == 'obtain' else 'not_requested'})
    return rows


def history_digest(versions):
    import hashlib
    from .disclosure import canonical_bytes
    return hashlib.sha256(canonical_bytes(versions)).hexdigest()


class CardActions:
    def __init__(self, authority, *, service):
        ga._require(type(authority) is ga.PortableAuthority and service.authority is authority)
        self.authority, self.service = authority, service

    def _prior(self, cabinet, command_id, kind, descriptor):
        from .models import PortableJourneyCommand
        row = PortableJourneyCommand.objects.filter(cabinet=cabinet, command_id=command_id).first()
        if row is not None:
            ga._require(row.kind == kind and row.descriptor == descriptor
                and row.descriptor_digest == history_digest(descriptor) and row.state == 'committed',
                'IDEMPOTENCY_CONFLICT', 409)
        return row

    def receipt(self, session_ref, profile_ref, command_id):
        from .models import PortableJourneyCommand
        from .disclosure import canonical_bytes, read_json
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            cabinet, _ = ga._bound(self.authority, session_ref, profile_ref)
            row = PortableJourneyCommand.objects.filter(cabinet=cabinet, command_id=ga._uuid(command_id),
                kind__in=['card.action', 'card.undo'], state='committed').first()
            ga._require(row is not None, 'AUTHORITY_DENIED', 403)
            return read_json(canonical_bytes(row.result))

    def _target(self, session_ref, command):
        import hashlib
        from .models import FeedbackTarget
        from .portable_reactions import target_ref
        current = self.service.current_for_interaction(session_ref, profile_ref=command.profile_ref)
        ga._require(current.get('publication_ref') == command.publication_ref
            and current.get('publication_revision') == command.publication_revision, 'PREVIEW_STALE', 409)
        manifest = current.get('manifest')
        ga._require(type(manifest) is dict, 'PREVIEW_STALE', 409)
        card = next((card for group in manifest['categories'] for card in group['cards']
                     if target_ref(command.profile_ref, card['key']) == command.target_ref), None)
        ga._require(card is not None and type(card['title']) is str and 0 < len(card['title']) <= 200,
                    'AUTHORITY_DENIED', 403)
        digest = hashlib.sha256(card['key'].encode()).hexdigest()
        target = FeedbackTarget.objects.filter(source_namespace='enhance-current', source_key=digest,
            kind='item', deleted=False).first()
        return target, card, digest

    def _signals(self, cabinet, target):
        from .models import FeedbackEvent
        from .feedback import reduce_events
        result = {}
        for signal in ('interest', 'saved'):
            events = list(FeedbackEvent.objects.filter(profile_id=cabinet.profile_id, target=target,
                signal=signal, scope='item').order_by('revision')) if target else []
            effective, body, erased = reduce_events(events) if events else (None, None, False)
            ga._require(not erased, 'AUTHORITY_DENIED', 403)
            result[signal] = {'revision': events[-1].revision if events else 0,
                'head': str(events[-1].pk) if events else None, 'body': body, 'effective': effective}
        return result

    def _check_revisions(self, signals, command):
        ga._require(all(signals[signal]['revision'] == getattr(command, signal+'_revision')
                        for signal in ('interest', 'saved')), 'REVISION_CONFLICT', 409)

    def _frame(self, session_ref, cabinet, publication_ref, target):
        import hashlib
        from .models import PortableCabinetManifest,FeedbackSubject
        publication=PortableCabinetManifest.objects.select_related('run').filter(pk=publication_ref,cabinet=cabinet).first()
        if publication is None:
            ga._require(ga._fixture_database() and not callable(getattr(self.service,'_store',None)),
                        'PREVIEW_STALE',409)
            return None  # Atomic unit fixture cannot issue interaction continuity.
        store=self.service._store(session_ref,str(cabinet.profile_ref),str(publication.run.portable_connection_id))
        current=store.current()
        return dict(record=current.record(),grants=current.view.grants,
            history_versions=ga._history_revisions(cabinet),
            session_digest=hashlib.sha256(session_ref.encode()).hexdigest(),
            process_generation=str(self.authority.boundary.process_generation),
            feedback_subject_present=FeedbackSubject.objects.filter(profile_id=cabinet.profile_id).exists(),
            target_present=target is not None,
            original_expires_at=publication.expires_at.isoformat())

    def _record(self, cabinet, command, *, kind, target, children, before, subject_before, values, session_ref):
        from .models import PortableJourneyCommand
        after = ga._history_revisions(cabinet)
        requested = command.action if kind == 'card.action' else 'undo'
        signals = self._signals(cabinet, target)
        obtain = dict(state='navigation_pending' if requested == 'obtain' else 'not_requested',
                      code='OK', href=None, job_ref=None)
        result = dict(command_id=command.command_id,
            target_ref=command.target_ref if kind == 'card.action' else values['target_ref'],
            requested_action=requested, preference_outcome='committed' if children else 'unchanged',
            selected_state=selected_state({s: signals[s]['body'] for s in signals}),
            interest_revision=signals['interest']['revision'], saved_revision=signals['saved']['revision'],
            child_event_refs=[str(e) for e in children], undo_available=bool(children) and kind == 'card.action',
            ranking_state='feedback_pending' if children else values.get('ranking_state', 'feedback_pending'),
            obtain=obtain, next_action='enhance')
        before_frame=values.get('before_frame')
        proof=dict(schema='card-action-unit-fixture/v1')
        if before_frame is not None:
            after_frame=self._frame(session_ref,cabinet,values['publication_ref'],target)
            actual_after,authority_digest=_row_material(cabinet)
            ga._require(authority_digest==values['authority_digest'],'PREVIEW_STALE',409)
            inventory=_guard_inventory()
            ga._require(inventory==values['guard_inventory'],'PREVIEW_STALE',409)
            delta,advance,receipt_refs=_prove_card_rows(cabinet,command,kind,target,values['before_rows'],
                actual_after,children,inventory)
            _check_frame_edge(before_frame,after_frame,advance)
            proof=dict(schema='card-action-continuity/v2',parent_command_ref=command.command_id,
                installation_ref=str(cabinet.installation_id),actor_ref=str(cabinet.actor_ref),
                profile_ref=str(cabinet.profile_ref),publication_ref=values['publication_ref'],
                publication_revision=values['publication_revision'],before_actual_frame=before_frame,
                after_actual_frame=after_frame,unchanged_authority_material_digest=authority_digest,
                permitted_row_delta_digest=history_digest(delta),child_event_refs=result['child_event_refs'],
                child_receipt_refs=receipt_refs,guard_inventory_digest=history_digest(inventory),
                derived_floor_advance=advance,original_expires_at=before_frame['original_expires_at'])
        PortableJourneyCommand.objects.create(cabinet=cabinet, command_id=command.command_id,
            kind=kind, descriptor=command.descriptor(), descriptor_digest=history_digest(command.descriptor()),
            state='committed', result=result, proof=proof, created_at=self.authority.now())
        return result

    def project(self, session_ref, *, profile_ref, target_ref):
        from .models import PortableJourneyCommand
        from .portable_authority import AuthorityFault
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref),self.authority._lock:
            cabinet,_=ga._bound(self.authority,session_ref,profile_ref)
            current=self.service.current_for_interaction(session_ref,profile_ref=profile_ref)
            probe=CardAction(profile_ref,current['publication_ref'],current['publication_revision'],
                target_ref,0,0,str(uuid.uuid4()),'like')
            target,card,_=self._target(session_ref,probe)
            signals=self._signals(cabinet,target)
            enabled=True;reason=''
            try:self.authority.authorize(session_ref,sensitive=True,touch=False)
            except AuthorityFault:enabled=False;reason='REAUTHENTICATION_REQUIRED'
            commands=list(PortableJourneyCommand.objects.filter(cabinet=cabinet,state='committed',
                kind__in=['card.action','card.undo'],result__target_ref=target_ref))
            transitions=[row for row in commands if row.result.get('child_event_refs')]
            last=max(transitions,key=lambda row:row.result['interest_revision']+row.result['saved_revision']) if transitions else None
            undo=None
            if last and last.kind=='card.action' and all(last.result[s+'_revision']==signals[s]['revision']
                    for s in ('interest','saved')):
                undo=dict(form_action='/cabinet/actions/undo/',fields=dict(profile_ref=profile_ref,
                    original_command_id=str(last.command_id),command_id=str(uuid.uuid4()),
                    interest_revision=signals['interest']['revision'],saved_revision=signals['saved']['revision']),
                    enabled=enabled,disabled_reason=reason)
            return dict(schema=SCHEMA,profile_ref=profile_ref,publication_ref=current['publication_ref'],
                publication_revision=current['publication_revision'],target_ref=target_ref,
                interest_revision=signals['interest']['revision'],saved_revision=signals['saved']['revision'],
                selected_state=selected_state({s:signals[s]['body'] for s in signals}),
                ranking_state=current.get('ranking_state','fresh'),last_transition_ref=str(last.command_id) if last else None,
                actions=action_forms(profile_ref=profile_ref,publication_ref=current['publication_ref'],
                    publication_revision=current['publication_revision'],target_ref=target_ref,
                    interest_revision=signals['interest']['revision'],saved_revision=signals['saved']['revision'],
                    enabled=enabled,disabled_reason=reason),undo=undo,
                receipt_href='/cabinet/actions/'+str(last.command_id)+'/' if last else None)

    def apply(self, session_ref, command):
        from django.contrib.auth import get_user_model
        from .models import FeedbackTarget, FeedbackSubject
        from .feedback import MutationCommand, immediate, mutate_in_transaction, Conflict, FeedbackError
        from .recovery import access_barrier
        ga._require(type(command) is CardAction, 'CONTRACT_INVALID', 422)
        with ga.current_writer(self.authority, session_ref, command.profile_ref) as cabinet:
            # Exact replay is a historical owned receipt and precedes current
            # card resolution. It never grants old source or send authority.
            prior = self._prior(cabinet, command.command_id, 'card.action', command.descriptor())
            if prior:
                return self.receipt(session_ref, command.profile_ref, command.command_id)
            target, card, digest = self._target(session_ref, command)
            actor = get_user_model().objects.get(pk=cabinet.actor_id)
            with access_barrier() as barrier, immediate(), ga.writer_window(actor, cabinet.profile_id):
                self._check_revisions(self._signals(cabinet, target), command)
                before = ga._history_revisions(cabinet)
                before_frame=self._frame(session_ref,cabinet,command.publication_ref,target)
                before_rows,authority_digest=_row_material(cabinet)
                inventory=_guard_inventory()
                subject_before = FeedbackSubject.objects.filter(profile_id=cabinet.profile_id).exists()
                if target is None:
                    target = FeedbackTarget.objects.create(id=uuid.uuid4(), kind='item', title=card['title'],
                        category=card['category'], source_namespace='enhance-current', source_key=digest)
                signals = self._signals(cabinet, target)
                children = []
                for signal, body in desired_values(command.action).items():
                    current = signals[signal]
                    if current['body'] == body:
                        continue
                    child = MutationCommand(str(uuid.uuid5(ga._uuid(command.command_id), signal)), str(target.pk),
                        'item', signal, 'item', 'set', current['revision'], current['head'], body)
                    try:
                        accepted = mutate_in_transaction(actor=actor, profile_id=cabinet.profile_id,
                                                         command=child, barrier=barrier)
                    except Conflict:
                        raise ga.AuthorityFault('REVISION_CONFLICT', 409) from None
                    except FeedbackError:
                        raise ga.AuthorityFault('AUTHORITY_DENIED', 403) from None
                    children.append(accepted.event_id)
                result = self._record(cabinet, command, kind='card.action', target=target, children=children,
                    before=before, subject_before=subject_before, session_ref=session_ref, values=dict(
                        publication_ref=command.publication_ref, publication_revision=command.publication_revision,
                        before_frame=before_frame,before_rows=before_rows,authority_digest=authority_digest,
                        guard_inventory=inventory))
            if command.action == 'obtain':
                result = self._obtain(session_ref, command, card, result)
            from .portable_taste_history import capture_committed
            capture_committed(self.authority,session_ref,command.profile_ref,origin_command_ref=command.command_id)
            return result

    def undo(self, session_ref, command):
        from django.contrib.auth import get_user_model
        from .models import PortableJourneyCommand, FeedbackTarget, FeedbackSubject, FeedbackEvent
        from .feedback import MutationCommand, immediate, mutate_in_transaction, Conflict, FeedbackError
        from .recovery import access_barrier
        ga._require(type(command) is CardUndo, 'CONTRACT_INVALID', 422)
        with ga.current_writer(self.authority, session_ref, command.profile_ref) as cabinet:
            prior = self._prior(cabinet, command.command_id, 'card.undo', command.descriptor())
            if prior:
                return self.receipt(session_ref, command.profile_ref, command.command_id)
            original = PortableJourneyCommand.objects.filter(cabinet=cabinet,
                command_id=command.original_command_id, kind='card.action', state='committed').first()
            ga._require(original is not None and original.result.get('undo_available') is True,
                        'REVISION_CONFLICT', 409)
            target = None
            for candidate in FeedbackTarget.objects.filter(source_namespace='enhance-current',deleted=False):
                if str(uuid.uuid5(ga._uuid(command.profile_ref),'candidate:'+candidate.source_key))==original.result['target_ref']:
                    target=candidate;break
            ga._require(target is not None, 'AUTHORITY_DENIED', 403)
            actor = get_user_model().objects.get(pk=cabinet.actor_id)
            with access_barrier() as barrier, immediate(), ga.writer_window(actor, cabinet.profile_id):
                signals = self._signals(cabinet, target)
                self._check_revisions(signals, command)
                originals = list(FeedbackEvent.objects.filter(profile_id=cabinet.profile_id,
                    actor_id=cabinet.actor_id, target=target, pk__in=original.result['child_event_refs']))
                ga._require(len(originals) == len(original.result['child_event_refs']) and originals,
                            'REVISION_CONFLICT', 409)
                for event in originals:
                    ga._require(event.signal in signals and event.operation == 'set'
                        and signals[event.signal]['revision'] == event.revision, 'REVISION_CONFLICT', 409)
                before = ga._history_revisions(cabinet)
                before_frame=self._frame(session_ref,cabinet,original.descriptor['publication_ref'],target)
                before_rows,authority_digest=_row_material(cabinet)
                inventory=_guard_inventory()
                subject_before = FeedbackSubject.objects.filter(profile_id=cabinet.profile_id).exists()
                children = []
                for event in originals:
                    current = signals[event.signal]
                    child = MutationCommand(str(uuid.uuid5(ga._uuid(command.command_id), event.signal)),
                        str(target.pk), 'item', event.signal, 'item', 'reverse', current['revision'],
                        current['head'], None, str(event.pk))
                    try:
                        accepted = mutate_in_transaction(actor=actor, profile_id=cabinet.profile_id,
                            command=child, barrier=barrier)
                    except Conflict:
                        raise ga.AuthorityFault('REVISION_CONFLICT', 409) from None
                    except FeedbackError:
                        raise ga.AuthorityFault('AUTHORITY_DENIED', 403) from None
                    children.append(accepted.event_id)
                result = self._record(cabinet, command, kind='card.undo', target=target, children=children,
                    before=before, subject_before=subject_before, session_ref=session_ref, values=dict(
                        target_ref=original.result['target_ref'], publication_ref=original.descriptor['publication_ref'],
                        publication_revision=original.descriptor['publication_revision'],before_frame=before_frame,
                        before_rows=before_rows,authority_digest=authority_digest,guard_inventory=inventory))
            from .portable_taste_history import capture_committed
            capture_committed(self.authority,session_ref,command.profile_ref,origin_command_ref=command.command_id)
            return result

    def _obtain(self, session_ref, command, card, result):
        # Preference has already committed. A missing acquisition adapter is a
        # truthful partial result; it cannot roll back or duplicate Favourite.
        from .models import PortableJourneyCommand
        from django.db import transaction
        outcome = dict(state='unavailable', code='UNSUPPORTED_CATEGORY', href=None, job_ref=None)
        if card.get('medium') == 'tv':
            from urllib.parse import urlencode
            from .acquisition_contract import Blocked
            from .portable_saved_authority import (select_portable_profile, selected_context,
                                                   portable_saved_scope)
            from .portable_obtain_origin import resolve_current_origin
            try:
                selection = select_portable_profile(self.authority, session_ref, profile_ref=command.profile_ref)
                token = selected_context(self.authority, session_ref, selection)
                with portable_saved_scope(self.authority, session_ref, profile_ref=command.profile_ref,
                                          selection_token=token) as access:
                    # Resolve current attribution locally. Opening the chooser
                    # never searches, downloads or purchases anything.
                    resolve_current_origin(access, publication_revision=command.publication_revision,
                        candidate_key=card['key'], scope='series_s01e01', now=self.authority.now())
                    from .models import AcquisitionJob
                    from .portable_obtain_origin import candidate_key_digest
                    jobs=list(AcquisitionJob.objects.filter(actor_id=access.identity.actor_id,
                        profile_id=access.identity.profile_id,publication_origin__profile_ref=command.profile_ref,
                        publication_origin__publication_revision=command.publication_revision,
                        publication_origin__candidate_key_digest=candidate_key_digest(card['key']))[:2])
                    if len(jobs)==1:
                        from .obtain_native_context import native_job_context
                        context=native_job_context(access,job_id=jobs[0].pk,operation='view')
                        context.revalidate()
                        outcome=dict(state='resume_ready',code='OK',
                            href='/cabinet/obtain/jobs/'+str(jobs[0].pk)+'/',job_ref=str(jobs[0].pk))
                self.selection = selection  # Private native-session state, never a durable receipt.
                if outcome['state']!='resume_ready':
                    outcome = dict(state='entry_ready', code='OK', href='/cabinet/obtain/options/?'+urlencode(
                        dict(profile_ref=command.profile_ref, publication_revision=command.publication_revision,
                             candidate_key=card['key'])), job_ref=None)
            except (ga.AuthorityFault, Blocked):
                # The acknowledged Favourite remains true even if navigation
                # loses current source or session authority after that commit.
                outcome = dict(state='blocked', code='CAPABILITY_UNAVAILABLE', href=None, job_ref=None)
        with ga.current_writer(self.authority, session_ref, command.profile_ref) as cabinet, \
                transaction.atomic(), ga._write_window():
            result = {**result, 'obtain': outcome}
            PortableJourneyCommand.objects.filter(cabinet=cabinet, command_id=command.command_id).update(result=result)
        return result


def _historical_rows(cabinet,current,excluded):
    from copy import deepcopy
    from types import SimpleNamespace
    from .feedback import reduce_events
    result=deepcopy(current)
    for edge in excluded:
        for identifier in edge.proof['child_event_refs']:result['FeedbackEvent'].pop(identifier,None)
        for identifier in edge.proof['child_receipt_refs']:result['MutationReceipt'].pop(str(identifier),None)
        frame=edge.proof['before_actual_frame']
        if not frame['feedback_subject_present']:
            result['FeedbackSubject']={key:row for key,row in result['FeedbackSubject'].items()
                if row['profile_id']!=cabinet.profile_id}
        if not frame['target_present']:
            target=current['FeedbackEvent'][edge.proof['child_event_refs'][0]]['target_id']
            result['FeedbackTarget'].pop(target,None)
    groups={}
    for event in result['FeedbackEvent'].values():
        if event['profile_id']==cabinet.profile_id:
            groups.setdefault((event['target_id'],event['signal'],event['scope']),[]).append(event)
    projections={key:row for key,row in current['FeedbackProjection'].items() if row['profile_id']!=cabinet.profile_id}
    for key,events in groups.items():
        events.sort(key=lambda e:e['revision'])
        matching=[(pk,row) for pk,row in current['FeedbackProjection'].items()
            if (row['profile_id'],row['target_id'],row['signal'],row['scope'])==(cabinet.profile_id,*key)]
        ga._require(len(matching)==1,'PREVIEW_STALE',409)
        history=[SimpleNamespace(pk=e['id'],operation=e['operation'],body=e['body'],erased=e['erased'],
                                 reverses_id=e['reverses_id']) for e in events]
        effective,body,erased=reduce_events(history)
        pk,row=matching[0]
        projections[pk]={**row,'revision':events[-1]['revision'],'effective_event_id':effective,
                         'value':body,'erased':erased}
    result['FeedbackProjection']=projections
    return result


def verify_transition_chain(cabinet,publication,current,authority,session_ref):
    # Restored or incomplete coordination data must deny the deck, rather than
    # turning a malformed historical proof into an unhandled page error.
    from django.core.exceptions import ObjectDoesNotExist
    try:
        return _verify_transition_chain(cabinet,publication,current,authority,session_ref)
    except (KeyError, TypeError, ValueError, IndexError, ObjectDoesNotExist):
        raise ga.AuthorityFault('PREVIEW_STALE',409) from None


def _verify_transition_chain(cabinet,publication,current,authority,session_ref):
    """A2 comparison-only continuity; real current floors never move backwards."""
    from .models import PortableJourneyCommand,FeedbackTarget,FeedbackSubject
    from types import SimpleNamespace
    import hashlib
    inventory=_guard_inventory();inventory_digest=history_digest(inventory)
    actual_rows,material_digest=_row_material(cabinet)
    record=current.record();cursor=publication.bindings
    rows=list(PortableJourneyCommand.objects.filter(cabinet=cabinet,state='committed',
        kind__in=['card.action','card.undo']).order_by('created_at','id')[:1001])
    ga._require(len(rows)<=1000,'PREVIEW_STALE',409)
    candidates=[row for row in rows if row.proof.get('schema')=='card-action-continuity/v2'
        and row.proof.get('publication_ref')==str(publication.pk)
        and row.proof.get('publication_revision')==publication.publication_revision and row.proof.get('child_event_refs')]
    chain=[];used=set()
    fields={'schema','parent_command_ref','installation_ref','actor_ref','profile_ref','publication_ref',
        'publication_revision','before_actual_frame','after_actual_frame','unchanged_authority_material_digest',
        'permitted_row_delta_digest','child_event_refs','child_receipt_refs','guard_inventory_digest',
        'derived_floor_advance','original_expires_at'}
    while cursor!=record:
        matches=[row for row in candidates if row.pk not in used and row.proof['before_actual_frame']['record']==cursor]
        ga._require(len(matches)==1,'PREVIEW_STALE',409)
        row=matches[0];proof=row.proof
        ga._require(set(proof)==fields and proof['parent_command_ref']==str(row.command_id)
            and proof['installation_ref']==str(cabinet.installation_id) and proof['actor_ref']==str(cabinet.actor_ref)
            and proof['profile_ref']==str(cabinet.profile_ref) and proof['guard_inventory_digest']==inventory_digest
            and proof['unchanged_authority_material_digest']==material_digest
            and proof['original_expires_at']==publication.expires_at.isoformat()
            and proof['child_event_refs']==row.result['child_event_refs']
            and row.descriptor_digest==history_digest(row.descriptor),'PREVIEW_STALE',409)
        _check_frame_edge(proof['before_actual_frame'],proof['after_actual_frame'],proof['derived_floor_advance'])
        chain.append(row);used.add(row.pk);cursor=proof['after_actual_frame']['record']
    ga._require(chain,'PREVIEW_STALE',409)
    session_digest=hashlib.sha256(session_ref.encode()).hexdigest()
    for index,row in enumerate(chain):
        proof=row.proof;a,b=proof['before_actual_frame'],proof['after_actual_frame']
        ga._require(a['session_digest']==b['session_digest']==session_digest
            and a['process_generation']==b['process_generation']==str(authority.boundary.process_generation),
            'PREVIEW_STALE',409)
        if index:
            previous=chain[index-1].proof['after_actual_frame']
            # Target presence belongs to this target's local delta; every other
            # actual frame field must be continuous across different cards.
            ga._require({k:v for k,v in previous.items() if k!='target_present'}==
                {k:v for k,v in a.items() if k!='target_present'},'PREVIEW_STALE',409)
        before=_historical_rows(cabinet,actual_rows,chain[index:])
        after=_historical_rows(cabinet,actual_rows,chain[index+1:])
        event=actual_rows['FeedbackEvent'].get(proof['child_event_refs'][0])
        ga._require(event is not None,'PREVIEW_STALE',409)
        target=FeedbackTarget.objects.get(pk=event['target_id'])
        command=CardAction.parse({**row.descriptor,'command_id':str(row.command_id)}) if row.kind=='card.action' else \
                CardUndo.parse({**row.descriptor,'command_id':str(row.command_id)})
        delta,advance,receipts=_prove_card_rows(cabinet,command,row.kind,target,before,after,proof['child_event_refs'],inventory)
        ga._require(advance==proof['derived_floor_advance'] and receipts==proof['child_receipt_refs']
            and history_digest(delta)==proof['permitted_row_delta_digest'],'PREVIEW_STALE',409)
        for frame,material in ((a,before),(b,after)):
            event_ids=[key for key,e in material['FeedbackEvent'].items() if e['profile_id']==cabinet.profile_id]
            versions=ga._history_revisions(cabinet,event_ids=event_ids,
                feedback_subject_present=frame['feedback_subject_present'])
            ga._require(versions==frame['history_versions']
                and history_digest(versions)==frame['record']['bindings']['snapshot_revision'],'PREVIEW_STALE',409)
    terminal=chain[-1].proof['after_actual_frame']
    ga._require(terminal['record']==record and terminal['grants']==current.view.grants
        and terminal['history_versions']==ga._history_revisions(cabinet),'PREVIEW_STALE',409)
    return True


def _local_rows(model):
    """Detached local evidence only; these private rows never become model input."""
    import json
    return json.loads(json.dumps(list(model.objects.order_by('pk').values()),sort_keys=True,
        separators=(',',':'),ensure_ascii=False,default=ga._local_default))


def _guard_inventory():
    from django.db import connection
    ga._synthesis_guards()
    rows=connection.connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
    return dict(rows)


def _row_material(cabinet):
    from django.contrib.auth import get_user_model
    from . import models
    feedback={name:{str(row['id'] if 'id' in row else row['profile_id']):row for row in _local_rows(getattr(models,name))}
        for name in ('FeedbackEvent','FeedbackProjection','FeedbackSubject','FeedbackTarget','MutationReceipt')}
    authority={name:_local_rows(getattr(models,name)) for name in ('Profile','Grant','PortableInstallation',
        'PortableConnection','PortableDisclosureGrant','RecoveryAnchor','RecoveryRestriction',
        'ProfileCabinetState','SavedCredentialBinding','PortableSourceCheckAttempt')}
    for row in authority['ProfileCabinetState']:
        if row['profile_id']==cabinet.profile_id:
            row.pop('policy_epoch');row.pop('restriction_watermark')
    authority['owner_accounts']=_local_rows(get_user_model())
    # Progress is coordination, but local selection context is authoritative.
    fields={field.name for field in models.PortableJourneyState._meta.fields}
    location_fields={'home_country','home_postcode','radius_km','location_revision'}
    authority['local_context']=(list(models.PortableJourneyState.objects.order_by('cabinet_id').values(
        'cabinet_id',*sorted(location_fields))) if location_fields<=fields else [])
    return feedback,history_digest(authority)


def _row_delta(before,after):
    changes=[]
    for table in before:
        for key in sorted(set(before[table])|set(after[table])):
            old,new=before[table].get(key),after[table].get(key)
            if old!=new:
                changes.append(dict(table=table,key=key,operation='insert' if old is None else 'delete' if new is None else 'update',
                    before_digest=history_digest(old) if old is not None else None,
                    after_digest=history_digest(new) if new is not None else None))
    return changes


def _prove_card_rows(cabinet,command,kind,target,before,after,children,inventory):
    """Check actual row changes and derive their floor effect from named guards."""
    from dataclasses import asdict
    from .feedback import MutationCommand,envelope_hash,reduce_events
    from .models import FeedbackEvent
    ga._require(len(children)<=2 and len(set(children))==len(children),'PREVIEW_STALE',409)
    changed_signals=set();receipts=[]
    for identifier in children:
        event=after['FeedbackEvent'].get(str(identifier))
        ga._require(event is not None and str(identifier) not in before['FeedbackEvent']
            and event['profile_id']==cabinet.profile_id and event['actor_id']==cabinet.actor_id
            and event['target_id']==str(target.pk) and event['signal'] in ('interest','saved')
            and event['scope']=='item' and not event['erased']
            and event['origin']==event['original_origin']=='direct','PREVIEW_STALE',409)
        signal=event['signal'];ga._require(signal not in changed_signals,'PREVIEW_STALE',409);changed_signals.add(signal)
        previous=sorted([e for e in before['FeedbackEvent'].values() if (e['profile_id'],e['target_id'],e['signal'],e['scope'])
            ==(cabinet.profile_id,str(target.pk),signal,'item')],key=lambda e:e['revision'])
        ga._require(event['revision']==(previous[-1]['revision'] if previous else 0)+1,'PREVIEW_STALE',409)
        if kind=='card.action':
            ga._require(event['operation']=='set' and event['body']==desired_values(command.action)[signal]
                and event['reverses_id'] is None and event['corrects_id'] is None,'PREVIEW_STALE',409)
        else:
            from .models import PortableJourneyCommand
            original=PortableJourneyCommand.objects.get(cabinet=cabinet,command_id=command.original_command_id)
            ga._require(event['operation']=='reverse' and event['body'] is None
                and event['reverses_id'] in original.result['child_event_refs'],'PREVIEW_STALE',409)
        mutation=MutationCommand(str(uuid.uuid5(ga._uuid(command.command_id),signal)),str(target.pk),'item',signal,
            'item',event['operation'],event['revision']-1,previous[-1]['id'] if previous else None,
            event['body'],event['reverses_id'],event['corrects_id'])
        payload=asdict(mutation);payload.pop('mutation_id')
        expected=envelope_hash('feedback.'+event['operation'],cabinet.profile_id,payload)
        matches=[r for key,r in after['MutationReceipt'].items() if key not in before['MutationReceipt']
            and r['mutation_id']==mutation.mutation_id and r['event_id']==identifier]
        ga._require(len(matches)==1,'PREVIEW_STALE',409)
        receipt=matches[0]
        ga._require(receipt['actor_id']==cabinet.actor_id and receipt['profile_id']==cabinet.profile_id
            and receipt['target_id']==str(target.pk) and receipt['accepted_revision']==event['revision']
            and receipt['operation_family']=='feedback.'+event['operation'] and receipt['state']=='accepted'
            and receipt['payload_sha256']==expected,'PREVIEW_STALE',409)
        receipts.append(receipt['id'])
        from types import SimpleNamespace
        history=[SimpleNamespace(pk=e['id'],operation=e['operation'],body=e['body'],erased=e['erased'],
            reverses_id=e['reverses_id']) for e in sorted(after['FeedbackEvent'].values(),key=lambda e:e['revision'])
            if (e['profile_id'],e['target_id'],e['signal'],e['scope'])==(cabinet.profile_id,str(target.pk),signal,'item')]
        effective,body,erased=reduce_events(history)
        projections=[p for p in after['FeedbackProjection'].values() if (p['profile_id'],p['target_id'],p['signal'],p['scope'])
            ==(cabinet.profile_id,str(target.pk),signal,'item')]
        ga._require(len(projections)==1 and projections[0]['revision']==event['revision']
            and projections[0]['effective_event_id']==(str(effective) if effective else None)
            and projections[0]['value']==body and projections[0]['erased']==erased,'PREVIEW_STALE',409)
    delta=_row_delta(before,after);advance=0
    for change in delta:
        table,key,operation=change['table'],change['key'],change['operation']
        row=after[table].get(key)
        ga._require(operation!='delete','PREVIEW_STALE',409)
        if table=='FeedbackEvent':
            ga._require(operation=='insert' and key in children,'PREVIEW_STALE',409)
        elif table=='FeedbackProjection':
            ga._require(row['profile_id']==cabinet.profile_id and row['target_id']==str(target.pk)
                and row['signal'] in changed_signals and row['scope']=='item','PREVIEW_STALE',409)
        elif table=='MutationReceipt':
            ga._require(operation=='insert' and row['id'] in receipts,'PREVIEW_STALE',409)
        elif table=='FeedbackSubject':
            ga._require(operation=='insert' and bool(children) and row['profile_id']==cabinet.profile_id
                and row['subject_id']==str(cabinet.subject_ref),'PREVIEW_STALE',409)
        else:
            ga._require(table=='FeedbackTarget' and operation=='insert' and key==str(target.pk)
                and row['kind']=='item' and row['source_namespace']=='enhance-current'
                and not row['deleted'],'PREVIEW_STALE',409)
        trigger=inventory.get('current_'+table.lower()+'_'+operation+'_floor')
        if trigger is not None:
            ga._require('policy_epoch=policy_epoch+1' in trigger
                and 'restriction_watermark=restriction_watermark+1' in trigger,'PREVIEW_STALE',409)
            advance+=1
    return delta,advance,receipts


def _check_frame_edge(before,after,advance):
    from .disclosure import canonical_bytes,read_json
    left=read_json(canonical_bytes(before['record']));right=after['record']
    a,b=left['bindings'],right['bindings']
    ga._require(b['policy_epoch']-a['policy_epoch']==advance
        and int(b['restriction_watermark'])-int(a['restriction_watermark'])==advance,'PREVIEW_STALE',409)
    grants=[{**grant,'policy_epoch':b['policy_epoch']} for grant in before['grants']]
    ga._require(grants==after['grants'] and history_digest(grants)==b['grants_digest'],'PREVIEW_STALE',409)
    for field in ('snapshot_revision','policy_epoch','restriction_watermark','grants_digest'):a[field]=b[field]
    ga._require(left==right and before['session_digest']==after['session_digest']
        and before['process_generation']==after['process_generation'],'PREVIEW_STALE',409)


def validate_journey_history(raw):
    """Validate retained coordination structure without reviving live authority.

    A restored source check or continuity frame remains historical: process,
    session, expiry and actual-row proof checks still run on every current read.
    """
    import re
    from .models import PortableJourneyState,PortableJourneyCommand,PortableSourceCheckAttempt
    from .portable_journey import STEPS,PROVIDER_ADAPTERS
    present=raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shelf_portablejourneycommand'").fetchone()
    if not present:return
    def objects(model):
        table=model._meta.db_table
        columns=[row[1] for row in raw.execute('PRAGMA table_info('+table+')')]
        if set(columns)!={field.column for field in model._meta.local_fields}:raise ValueError('Unknown journey schema')
        cursor=raw.execute('SELECT * FROM '+table)
        return [dict(zip([column[0] for column in cursor.description],row)) for row in cursor]
    def document(value):
        from .disclosure import read_json
        return read_json(value.encode('utf-8'))
    def identifier(value):return str(uuid.UUID(str(value)))
    cabinets={row[0]:row for row in raw.execute(
        'SELECT profile_id,profile_ref,installation_id,actor_id,actor_ref,profile_id FROM shelf_profilecabinetstate')}
    commands={}
    proof_fields={'schema','parent_command_ref','installation_ref','actor_ref','profile_ref','publication_ref',
        'publication_revision','before_actual_frame','after_actual_frame','unchanged_authority_material_digest',
        'permitted_row_delta_digest','child_event_refs','child_receipt_refs','guard_inventory_digest',
        'derived_floor_advance','original_expires_at'}
    frame_fields={'record','grants','history_versions','session_digest','process_generation',
                  'feedback_subject_present','target_present','original_expires_at'}
    for row in objects(PortableJourneyCommand):
        cabinet=cabinets[row['cabinet_id']]
        descriptor,result,proof=(document(row[key]) for key in ('descriptor','result','proof'))
        if any(type(value) is not dict for value in (descriptor,result,proof)):raise ValueError('Invalid command document')
        if row['descriptor_digest']!=history_digest(descriptor):raise ValueError('Invalid command digest')
        command_id=identifier(row['command_id']);kind=row['kind']
        if kind not in {'card.action','card.undo','journey.name','journey.step','journey.provider.configure',
            'journey.provider.save','journey.favourite','journey.prepare','journey.location','source.check',
            'taste.checkpoint','taste.preview','taste.restore','taste.undo'}:raise ValueError('Unknown command')
        if row['state'] not in {'reserved','partial','unknown','committed'} and not (
            kind.startswith('taste.') and row['state']=='retired'):raise ValueError('Invalid command state')
        if kind.startswith('taste.'):
            from .portable_taste_history import validate_history_command
            validate_history_command(raw,row,descriptor,result,cabinet)
        if kind in ('card.action','card.undo'):
            command=(CardAction if kind=='card.action' else CardUndo).parse({**descriptor,'command_id':command_id})
            if command.profile_ref!=identifier(cabinet[1]) or row['state']!='committed':raise ValueError('Foreign action')
            # Unit-only receipts deliberately cannot survive a native restore.
            if set(proof)!=proof_fields or proof['schema']!='card-action-continuity/v2':raise ValueError('Invalid proof')
            if (proof['parent_command_ref']!=command_id or proof['profile_ref']!=identifier(cabinet[1])
                or proof['installation_ref']!=identifier(cabinet[2]) or proof['actor_ref']!=identifier(cabinet[4])
                or proof['child_event_refs']!=result['child_event_refs']):raise ValueError('Foreign proof')
            revision(proof['publication_revision'],positive=True);revision(proof['derived_floor_advance'])
            identifier(proof['publication_ref'])
            for key in ('unchanged_authority_material_digest','permitted_row_delta_digest','guard_inventory_digest'):
                if type(proof[key]) is not str or re.fullmatch('[0-9a-f]{64}',proof[key]) is None:raise ValueError('Invalid digest')
            events=proof['child_event_refs'];receipts=proof['child_receipt_refs']
            if (type(events) is not list or len(events)>2 or len(set(events))!=len(events)
                or type(receipts) is not list or len(receipts)!=len(events)):raise ValueError('Invalid children')
            for event_ref,receipt_ref in zip(events,receipts):
                event=raw.execute('SELECT profile_id,actor_id FROM shelf_feedbackevent WHERE id=?',
                                  (uuid.UUID(event_ref).hex,)).fetchone()
                receipt=raw.execute('SELECT profile_id,actor_id,event_id FROM shelf_mutationreceipt WHERE id=?',
                                    (receipt_ref,)).fetchone()
                if (event!=(cabinet[5],cabinet[3]) or receipt!=(cabinet[5],cabinet[3],uuid.UUID(event_ref).hex)):
                    raise ValueError('Missing owned action child')
            for key in ('before_actual_frame','after_actual_frame'):
                frame=proof[key]
                if type(frame) is not dict or set(frame)!=frame_fields:raise ValueError('Invalid frame')
                if (frame['original_expires_at']!=proof['original_expires_at'] or
                    any(type(frame[field]) is not bool for field in ('feedback_subject_present','target_present'))):
                    raise ValueError('Invalid frame binding')
            _check_frame_edge(proof['before_actual_frame'],proof['after_actual_frame'],proof['derived_floor_advance'])
        elif proof:raise ValueError('Unexpected command proof')
        commands[identifier(row['id'])]=(row,descriptor,result)
    for row in objects(PortableJourneyState):
        cabinet=cabinets[row['cabinet_id']]
        revision(row['revision'])
        from .local_event_preferences import validate_setting
        revision(row['location_revision'])
        if any(row[key] is not None for key in ('home_country','home_postcode','radius_km')):
            validate_setting(row['home_country'],row['home_postcode'],row['radius_km'])
        if type(row['next_noise']) is not int or not 0<=row['next_noise']<=100:raise ValueError('Invalid Noise')
        if row['step'] not in STEPS:raise ValueError('Invalid journey step')
        for key in ('completed','skipped'):
            value=document(row[key])
            if type(value) is not list or len(value)!=len(set(value)) or any(step not in STEPS for step in value):
                raise ValueError('Invalid progress')
        connections=document(row['connections']);slots=document(row['slots'])
        if type(connections) is not dict or set(connections)-set(PROVIDER_ADAPTERS):raise ValueError('Invalid connections')
        for provider,refs in connections.items():
            if type(refs) is not list or len(refs)!=len(PROVIDER_ADAPTERS[provider]) or len(set(refs))!=len(refs):
                raise ValueError('Invalid provider rows')
            for ref in refs:
                source=raw.execute('SELECT installation_id,adapter FROM shelf_portableconnection WHERE id=?',
                                   (uuid.UUID(ref).hex,)).fetchone()
                if not source or identifier(source[0])!=identifier(cabinet[2]) or source[1] not in PROVIDER_ADAPTERS[provider]:
                    raise ValueError('Foreign provider row')
        if type(slots) is not dict or len(slots)>70:raise ValueError('Invalid favourites')
        from .category_registry import KEYS
        for key,slot in slots.items():
            category,number=key.rsplit(':',1)
            if category not in (*KEYS,'visual_artist') or number not in ('1','2','3','4','5') or set(slot)!={'entry_ref','saved_revision'}:
                raise ValueError('Invalid favourite slot')
            identifier(slot['entry_ref']);revision(slot['saved_revision'],positive=True)
    for row in objects(PortableSourceCheckAttempt):
        command,descriptor,result=commands[identifier(row['command_id'])]
        binding,outcome=document(row['binding']),document(row['outcome'])
        cabinet=cabinets[row['cabinet_id']]
        if (command['kind']!='source.check' or command['cabinet_id']!=row['cabinet_id'] or
            row['state'] not in {'sending','passed','failed','unknown'} or type(outcome) is not dict or
            type(binding) is not dict or binding.get('schema')!='native-source-check-binding/v1' or
            binding.get('profile_ref')!=identifier(cabinet[1]) or
            binding.get('operation_digest')!=row['operation_digest']):raise ValueError('Invalid source check')
