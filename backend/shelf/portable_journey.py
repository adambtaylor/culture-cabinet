"""Guided journey projections and resumable nonsecret coordination primitives."""
import uuid
from . import grant_authority as ga
from .card_actions import revision
from .category_registry import CATEGORIES, Category

SCHEMA = 'guided-owner-journey/v2'
STEPS = ('identity', 'providers', 'favourites', 'events_location', 'prepare', 'finish')
INTAKE_CATEGORIES = (*CATEGORIES, Category('visual_artist','Visual artists','art'))


def form(action, fields, *, enabled=True, reason=''):
    return {'form_action': action, 'fields': fields, 'enabled': enabled, 'disabled_reason': reason}


def journey_actions(*, profile_ref, journey_ref, expected_revision, owner_revision, step):
    ga._require(step in STEPS, 'CONTRACT_INVALID', 422)
    revision(expected_revision); revision(owner_revision)
    bound = profile_ref is not None and journey_ref is not None
    base = {'profile_ref': profile_ref, 'journey_ref': journey_ref, 'expected_revision': expected_revision}
    return {'name': form('/cabinet/journey/name', {'expected_owner_revision': owner_revision,
                'label': '', 'command_id': str(uuid.uuid4())}, enabled=not bound),
        'step': {action: form('/cabinet/journey/step', {**base, 'command_id': str(uuid.uuid4()),
            'step': step, 'action': action}, enabled=bound) for action in ('continue', 'back', 'skip')},
        'prepare': form('/cabinet/journey/prepare', {**base, 'command_id': str(uuid.uuid4()),
            'connection_ref': None, 'client_key': str(uuid.uuid4()), 'refresh_nonce': str(uuid.uuid4())},
            enabled=False, reason='INFERENCE_CONNECTION_REQUIRED')}


def favourite_slot(*, profile_ref, journey_ref, expected_revision, category, slot,
                   entry_ref=None, title='', saved_revision=0):
    ga._require(category in tuple(c.key for c in INTAKE_CATEGORIES) and type(slot) is int and 1 <= slot <= 5,
                'CONTRACT_INVALID', 422)
    revision(expected_revision); revision(saved_revision)
    entry_ref = str(ga._uuid(entry_ref)) if entry_ref else str(uuid.uuid4())
    base = dict(profile_ref=profile_ref, journey_ref=journey_ref, expected_revision=expected_revision,
                category=category, slot=slot, entry_ref=entry_ref, base_revision=saved_revision)
    add = form('/cabinet/journey/favourites/add', {**base, 'command_id': str(uuid.uuid4()),
        'mutation_id': str(uuid.uuid4()), 'title': title})
    remove = form('/cabinet/journey/favourites/remove', {**base, 'command_id': str(uuid.uuid4()),
        'mutation_id': str(uuid.uuid4())}, enabled=saved_revision > 0 and bool(title))
    return {'slot': slot, 'entry_ref': entry_ref, 'title': title, 'current_saved_revision': saved_revision,
            'mutation_id': add['fields']['mutation_id'], 'removable': remove['enabled'],
            'actions': {'add': add, 'remove': remove}}


def journey_projection(*, profile_ref, profile_label, journey_ref, revision, step, steps,
                       providers, favourite_categories, categories, preparation, completion_receipts, actions,
                       location=None,next_noise=None):
    ga._require(step in STEPS and type(profile_label) is str and len(profile_label) <= 80,
                'CONTRACT_INVALID', 422)
    # This detached primitive-only shape is the shared UI port. Authority and
    # persistent receipts stay behind the native service, never in a template.
    from .disclosure import canonical_bytes, read_json
    value = dict(schema=SCHEMA, profile_ref=profile_ref, profile_label=profile_label,
        journey_ref=journey_ref, revision=revision, step=step, steps=steps, providers=providers,
        favourite_categories=favourite_categories, categories=categories, preparation=preparation,
        completion_receipts=completion_receipts, actions=actions, location=location)
    if next_noise is not None:value['next_noise']=next_noise
    return read_json(canonical_bytes(value))


def _current_favourite_slots(cabinet,journey):
    """Project guided identities onto current authentic saved-event heads."""
    from .models import PortableJourneyCommand
    from .portable_taste_history import _supported
    if journey is None:return {}
    # History restore and Undo append events without rewriting journey metadata.
    # Commands locate original guided identities; only event heads decide Saved/CAS.
    candidates={value['entry_ref']:key for key,value in journey.slots.items()}
    commands=PortableJourneyCommand.objects.filter(cabinet=cabinet,kind='journey.favourite',
        state='committed').order_by('created_at','pk')
    for command in commands:
        value=command.descriptor
        if value['journey_ref']==str(journey.pk):
            candidates.setdefault(value['entry_ref'],value['category']+':'+str(value['slot']))
    streams,_=_supported(cabinet);active={}
    for (_,signal),stream in streams.items():
        target=stream['target']
        namespace=('owner-stated-visual-artist' if target.category=='visual_artist' else 'owner-stated-favourite')
        if signal!='saved' or target.source_namespace!=namespace or not stream['available'] or stream['body']!={'value':True}:
            continue
        prefix=str(cabinet.profile_ref)+':'
        entry=target.source_key.removeprefix(prefix)
        preferred=candidates.get(entry)
        if preferred and target.source_key==prefix+entry and preferred.split(':')[0]==target.category:
            active[entry]=(preferred,stream['history'][-1].revision)
    slots={}
    # Keep occupied current slots first. Reinstated original identities take their
    # old slot when free, then the first free slot; never hide an overflow.
    for entry in candidates:
        if entry not in active:continue
        preferred,saved_revision=active[entry]
        category=preferred.split(':')[0]
        choices=[preferred,*[category+':'+str(number) for number in range(1,6)]]
        key=next((key for key in choices if key not in slots),None)
        ga._require(key is not None,'REVISION_CONFLICT',409)
        slots[key]=dict(entry_ref=entry,saved_revision=saved_revision)
    return slots


class PortableJourney:
    def __init__(self, authority):
        ga._require(type(authority) is ga.PortableAuthority)
        self.authority = authority

    def _digest(self, value):
        from .card_actions import history_digest
        return history_digest(value)

    def _prior(self, cabinet, command_id, kind, descriptor):
        from .models import PortableJourneyCommand
        row = PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=ga._uuid(command_id)).first()
        if row:
            ga._require(row.kind == kind and row.descriptor == descriptor
                and row.descriptor_digest == self._digest(descriptor),'IDEMPOTENCY_CONFLICT',409)
        return row

    def _record(self, cabinet, command_id, kind, descriptor, result, *, state='committed'):
        from .models import PortableJourneyCommand
        from .disclosure import canonical_bytes
        ga._require(len(canonical_bytes(descriptor))<=8192,'CONTRACT_INVALID',422)
        return PortableJourneyCommand.objects.create(cabinet=cabinet,command_id=ga._uuid(command_id),
            kind=kind,descriptor=descriptor,descriptor_digest=self._digest(descriptor),state=state,
            result=result,proof={},created_at=self.authority.now())

    def name(self, session_ref, *, expected_owner_revision, label, command_id):
        from .models import ProfileCabinetState, PortableJourneyState
        ga._uuid(command_id); revision(expected_owner_revision,positive=True)
        descriptor = dict(expected_owner_revision=expected_owner_revision,label=label)
        with ga.recovery_guard(self.authority,session_ref) as checkpoint,self.authority._lock,ga._checked_transaction(checkpoint):
            installation,session=ga._owner(self.authority,session_ref,sensitive=True)
            cabinet=ProfileCabinetState.objects.filter(installation=installation,actor_id=session.owner_id).first()
            if cabinet:
                ga._bound(self.authority,session_ref,str(cabinet.profile_ref),sensitive=True)
                prior=self._prior(cabinet,command_id,'journey.name',descriptor)
                ga._require(prior is not None and prior.state=='committed','REVISION_CONFLICT',409)
                return prior.result
            profile_ref=ga.create_owner_profile(self.authority,session_ref,
                expected_owner_revision=expected_owner_revision,label=label)
            cabinet,_=ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
            with ga._write_window():
                journey=PortableJourneyState.objects.create(id=uuid.uuid5(cabinet.profile_ref,'guided-owner-journey:v2'),
                    cabinet=cabinet,created_at=self.authority.now(),
                    updated_at=self.authority.now(),completed=['identity'])
                result=dict(profile_ref=profile_ref,journey_ref=str(journey.pk),revision=journey.revision,step=journey.step)
                self._record(cabinet,command_id,'journey.name',descriptor,result)
            return result

    def _state(self, session_ref, profile_ref, journey_ref, *, sensitive=False, expected_revision=None):
        from .models import PortableJourneyState
        cabinet,_=ga._bound(self.authority,session_ref,profile_ref,sensitive=sensitive)
        journey=PortableJourneyState.objects.filter(pk=ga._uuid(journey_ref),cabinet=cabinet).first()
        if journey is None and expected_revision==0:
            from django.db import transaction
            ga._require(str(uuid.uuid5(cabinet.profile_ref,'guided-owner-journey:v2'))==journey_ref
                and not PortableJourneyState.objects.filter(cabinet=cabinet).exists(),'AUTHORITY_DENIED',403)
            # Explicit first command on an already chosen owned cabinet starts
            # progress. A GET projects this deterministic ref without writing it.
            with transaction.atomic(),ga._write_window():
                journey=PortableJourneyState.objects.create(id=ga._uuid(journey_ref),cabinet=cabinet,
                    revision=0,created_at=self.authority.now(),updated_at=self.authority.now(),completed=['identity'])
        ga._require(journey is not None,'AUTHORITY_DENIED',403)
        return cabinet,journey

    def step(self, session_ref, *, profile_ref, journey_ref, expected_revision, command_id, step, action):
        revision(expected_revision);ga._uuid(command_id)
        ga._require(step in STEPS and action in ('continue','back','skip'),'CONTRACT_INVALID',422)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
                        step=step,action=action)
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref) as checkpoint, \
                self.authority._lock,ga._checked_transaction(checkpoint):
            cabinet,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.step',descriptor)
            if prior:return prior.result
            ga._require(journey.revision==expected_revision and journey.step==step,'REVISION_CONFLICT',409)
            index=STEPS.index(step)
            if action=='back':
                journey.step=STEPS[max(1,index-1)]
            else:
                collection='skipped' if action=='skip' else 'completed'
                setattr(journey,collection,list(dict.fromkeys([*getattr(journey,collection),step])))
                journey.step=STEPS[min(len(STEPS)-1,index+1)]
            journey.revision+=1;journey.updated_at=self.authority.now()
            result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,step=journey.step)
            with ga._write_window():
                journey.save(update_fields=['step','completed','skipped','revision','updated_at'])
                self._record(cabinet,command_id,'journey.step',descriptor,result)
            return result

    def _pending(self, cabinet, *, exclude=None):
        from .models import PortableJourneyCommand
        rows=PortableJourneyCommand.objects.filter(cabinet=cabinet,state__in=['reserved','partial','unknown'],
            kind__startswith='journey.')
        if exclude is not None:rows=rows.exclude(pk=exclude)
        ga._require(not rows.exists(),'UNKNOWN_RECONCILIATION_REQUIRED',409)

    def _advance(self, journey):
        journey.revision+=1;journey.updated_at=self.authority.now()
        journey.save(update_fields=['revision','connections','slots','run_ref','updated_at'])

    def configure(self, session_ref, *, profile_ref, journey_ref, expected_revision, command_id,
                  provider, confirmed):
        from .source_authority import configure_source
        ga._require(provider in PROVIDER_ADAPTERS and confirmed is True,'CONTRACT_INVALID',422)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
                        provider=provider,confirmed=True)
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref) as checkpoint, \
                self.authority._lock,ga._checked_transaction(checkpoint):
            cabinet,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.provider.configure',descriptor)
            if prior:return prior.result
            self._pending(cabinet)
            ga._require(journey.revision==expected_revision,'REVISION_CONFLICT',409)
            refs=[]
            for adapter in PROVIDER_ADAPTERS[provider]:
                if provider=='openai':
                    from .models import PortableConnection
                    existing=list(PortableConnection.objects.filter(installation=cabinet.installation,
                        adapter='openai_responses',removed=False))
                    ga._require(len(existing)<=1,'REVISION_CONFLICT',409)
                    if existing:
                        result=existing[0]
                        ga._require(result.model=='gpt-6-astra' and result.reasoning=='high',
                                    'CAPABILITY_UNAVAILABLE',503)
                    else:
                        result=self.authority._source_vault.configure(session_ref,
                            dict(adapter='openai_responses',model='gpt-6-astra',reasoning='high'))
                    refs.append(str(result.pk))
                else:refs.append(configure_source(self.authority,session_ref,confirmed=True,adapter=adapter))
            with ga._write_window():
                journey.connections={**journey.connections,provider:refs};self._advance(journey)
                result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,
                            connection_refs=refs,code='CONFIGURED')
                self._record(cabinet,command_id,'journey.provider.configure',descriptor,result)
            return result

    def save_provider(self, session_ref, *, profile_ref, journey_ref, expected_revision,
                      command_id, provider, secret, storage):
        from django.db import transaction
        from .models import PortableConnection,PortableJourneyCommand,SavedCredentialBinding
        from .portable_authority import AuthorityFault
        ga._require(provider in ('openai','tmdb','hardcover') and storage=='persistent','CONTRACT_INVALID',422)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
                        provider=provider,storage=storage)
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            _,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.provider.save',descriptor)
            if prior and prior.state in ('committed','unknown'):
                return prior.result  # Ignore posted secret entirely on replay.
            rows=list(PortableConnection.objects.filter(pk__in=journey.connections.get(provider,[]),
                installation=cabinet.installation,removed=False).order_by('adapter'))
            ga._require(len(rows)==len(PROVIDER_ADAPTERS[provider]) and
                {row.adapter for row in rows}==set(PROVIDER_ADAPTERS[provider]),'REVISION_CONFLICT',409)
            if prior:
                ga._require(prior.state=='partial','UNKNOWN_RECONCILIATION_REQUIRED',409)
                acknowledgements=prior.result.get('bindings',[])
                ga._require(acknowledgements,'UNKNOWN_RECONCILIATION_REQUIRED',409)
                anchor=acknowledgements[0]
                first=SavedCredentialBinding.objects.filter(pk=anchor['connection_ref'],state='ACTIVE',
                    generation=anchor['saved_generation'],store_revision=anchor['store_revision'],removed=False).first()
                ga._require(first is not None,'REVISION_CONFLICT',409)
                unacknowledged=[row.pk for row in rows if not any(
                    ack['connection_ref']==str(row.pk) for ack in acknowledgements)]
                # A crash after a later OS write can leave an unacknowledged
                # saved row. Do not mistake reconciliation for permission to
                # replace it again. Only an untouched fresh child can resume.
                ga._require(not SavedCredentialBinding.objects.filter(pk__in=unacknowledged).exists(),
                    'UNKNOWN_RECONCILIATION_REQUIRED',409)
                private=self.authority._source_vault.resolve(session_ref,first.pk,anchor['config_revision'])
            else:
                self._pending(cabinet)
                ga._require(journey.revision==expected_revision,'REVISION_CONFLICT',409)
                ga._require(type(secret) is str,'CONTRACT_INVALID',422)
                try:private=secret.encode('utf-8','strict')
                except UnicodeError:raise ga.AuthorityFault('CONTRACT_INVALID',422) from None
                ga._require(1<=len(private)<=8192,'CONTRACT_INVALID',422)
                acknowledgements=[]
                with transaction.atomic(),ga._write_window():
                    prior=self._record(cabinet,command_id,'journey.provider.save',descriptor,
                        dict(code='PENDING',bindings=[]),state='reserved')
            code='SAVED';state='committed'
            try:
                for row in rows:
                    if any(ack['connection_ref']==str(row.pk) for ack in acknowledgements):continue
                    self.authority._source_vault.replace(session_ref,row.pk,row.config_revision,private,storage='persistent')
                    row.refresh_from_db();saved=SavedCredentialBinding.objects.get(pk=row.pk,state='ACTIVE',removed=False)
                    acknowledgements.append(dict(connection_ref=str(row.pk),config_revision=row.config_revision,
                        credential_generation=row.credential_generation,saved_generation=str(saved.generation),
                        store_revision=saved.store_revision))
                    # Publish each definite child acknowledgement before another
                    # OS operation; a partial TMDB fan-out never says all saved.
                    with transaction.atomic(),ga._write_window():
                        PortableJourneyCommand.objects.filter(pk=prior.pk).update(state='partial',
                            result=dict(code='PARTIAL',bindings=acknowledgements))
            except AuthorityFault:
                code='SAVE_UNKNOWN';state='unknown'
            finally:
                private=None;secret=None
            with transaction.atomic(),ga._write_window():
                if state=='committed':self._advance(journey)
                result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,
                    code=code,bindings=acknowledgements)
                PortableJourneyCommand.objects.filter(pk=prior.pk).update(state=state,result=result)
            return result

    def favourite(self, session_ref, *, profile_ref, journey_ref, expected_revision, command_id,
                  category, slot, entry_ref, mutation_id, base_revision, title=None, remove=False):
        from django.db import transaction
        from .models import PortableJourneyCommand
        from .portable_taste_inputs import PortableTasteInputs
        ga._require(category in tuple(row.key for row in INTAKE_CATEGORIES) and type(slot) is int and 1<=slot<=5,
                    'CONTRACT_INVALID',422)
        ga._uuid(entry_ref);ga._uuid(mutation_id);revision(base_revision)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
            category=category,slot=slot,entry_ref=entry_ref,mutation_id=mutation_id,base_revision=base_revision,
            remove=remove,**({} if remove else {'title':title}))
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            _,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.favourite',descriptor)
            if prior and prior.state=='committed':return prior.result
            ga._require(remove or type(title) is str and 1<=len(title)<=200 and bool(title.strip())
                and not any(ord(c)<32 or ord(c)==127 for c in title),'CONTRACT_INVALID',422)
            self._pending(cabinet,exclude=prior.pk if prior else None)
            slots=_current_favourite_slots(cabinet,journey)
            key=category+':'+str(slot)
            # An interrupted exact parent resumes its reserved child receipt;
            # new commands must bind the current event-derived slot revision.
            old=(journey.slots if prior else slots).get(key)
            ga._require(journey.revision==expected_revision and
                (not old or old['entry_ref']==entry_ref and old['saved_revision']==base_revision),
                'REVISION_CONFLICT',409)
            if prior is None and category=='visual_artist' and not remove:
                from .portable_taste_inputs import validate_visual_artist
                validate_visual_artist(cabinet,profile_ref,entry_ref,title)
            if prior is None:
                from .models import FeedbackTarget,FeedbackEvent,MutationReceipt
                namespace='owner-stated-visual-artist' if category=='visual_artist' else 'owner-stated-favourite'
                target=FeedbackTarget.objects.filter(source_namespace=namespace,
                    source_key=profile_ref+':'+entry_ref,kind='item').first()
                ga._require(not remove or target is not None,'REVISION_CONFLICT',409)
                if target is not None:
                    ga._require(not target.deleted and target.category==category,'REVISION_CONFLICT',409)
                    ga._require(remove or target.title==title,'FAVOURITE_REPLACEMENT_REQUIRED',409)
                events=FeedbackEvent.objects.filter(profile_id=cabinet.profile_id,target=target,
                    signal='saved',scope='item').order_by('revision') if target else FeedbackEvent.objects.none()
                latest=events.last()
                ga._require(not events.filter(erased=True).exists() and
                    base_revision==(latest.revision if latest else 0) and
                    not MutationReceipt.objects.filter(actor_id=cabinet.actor_id,mutation_id=mutation_id).exists(),
                    'REVISION_CONFLICT',409)
                # Definite identity/title/CAS refusals precede reservation.
                # An existing reserved parent still resumes its committed child
                # through the original receipt, never through a blanket cleanup.
                with transaction.atomic(),ga._write_window():
                    prior=self._record(cabinet,command_id,'journey.favourite',descriptor,{},state='reserved')
            # Existing mutation receipt resumes an interrupted progress write;
            # same child UUID returns its original event instead of duplicating it.
            accepted=PortableTasteInputs(self.authority).mutate_favourite(session_ref,profile_ref,
                entry_ref=entry_ref,mutation_id=mutation_id,base_revision=base_revision,
                category=category,title=title,remove=remove)
            if remove:slots.pop(key,None)
            else:slots[key]=dict(entry_ref=entry_ref,saved_revision=accepted['revision'])
            with transaction.atomic(),ga._write_window():
                journey.slots=slots;self._advance(journey)
                result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,
                    entry_ref=entry_ref,saved_revision=accepted['revision'],event_ref=accepted['event_ref'])
                PortableJourneyCommand.objects.filter(pk=prior.pk).update(state='committed',result=result)
            from .portable_taste_history import capture_committed
            capture_committed(self.authority,session_ref,profile_ref,origin_command_ref=command_id)
            return result


    def location(self, session_ref, *, profile_ref, journey_ref, expected_revision,command_id,
                 action,country=None,postcode=None,radius_km=None):
        from .local_event_preferences import validate_setting
        ga._require(action in ('save','skip','remove'),'CONTRACT_INVALID',422)
        ga._uuid(command_id);revision(expected_revision)
        values=validate_setting(country,postcode,radius_km) if action=='save' else (None,None,None)
        ga._require(action=='save' or country is postcode is radius_km is None,'CONTRACT_INVALID',422)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
                        action=action,**(dict(zip(('country','postcode','radius_km'),values)) if action=='save' else {}))
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref) as checkpoint, \
                self.authority._lock,ga._checked_transaction(checkpoint):
            cabinet,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,
                                        expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.location',descriptor)
            if prior:return prior.result
            self._pending(cabinet)
            ga._require(journey.revision==expected_revision,'REVISION_CONFLICT',409)
            if action!='skip' and (journey.home_country,journey.home_postcode,journey.radius_km)!=values:
                journey.home_country,journey.home_postcode,journey.radius_km=values
                journey.location_revision+=1
            # Skip keeps an existing setting. Neither navigation nor the radius
            # creates a feedback signal or an outbound source/model parameter.
            if journey.step=='events_location':
                collection='skipped' if action=='skip' else 'completed'
                setattr(journey,collection,list(dict.fromkeys([*getattr(journey,collection),'events_location'])))
                journey.step='prepare'
            journey.revision+=1;journey.updated_at=self.authority.now()
            result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,
                location_revision=journey.location_revision,step=journey.step,code='LOCATION_'+action.upper())
            with ga._write_window():
                journey.save(update_fields=['home_country','home_postcode','radius_km','location_revision',
                    'step','completed','skipped','revision','updated_at'])
                self._record(cabinet,command_id,'journey.location',descriptor,result)
            return result

    def prepare(self, session_ref, *, profile_ref, journey_ref, expected_revision,
                command_id, connection_ref, client_key, refresh_nonce,noise=None):
        from django.db import transaction
        from .models import PortableJourneyCommand,SynthesisRun
        from .enhance_contract import EnhanceFault
        from .disclosure import DisclosureError
        from .portable_enhance_service import get_service
        for value in (command_id,connection_ref,client_key,refresh_nonce):ga._uuid(value)
        descriptor=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=expected_revision,
            connection_ref=connection_ref,client_key=client_key,refresh_nonce=refresh_nonce)
        if noise is not None:
            from .enhance_selection import RunSelectionPolicy
            RunSelectionPolicy(noise);descriptor['noise']=noise
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            _,journey=self._state(session_ref,profile_ref,journey_ref,sensitive=True,expected_revision=expected_revision)
            prior=self._prior(cabinet,command_id,'journey.prepare',descriptor)
            if prior and prior.state=='committed':
                if prior.result.get('schema')=='journey-prepare-refusal/v1':
                    raise ga.AuthorityFault(prior.result['code'],prior.result['status'])
                return prior.result
            self._pending(cabinet,exclude=prior.pk if prior else None)
            ga._require(journey.revision==expected_revision and
                connection_ref in journey.connections.get('openai',[]),'REVISION_CONFLICT',409)
            if prior is None:
                with transaction.atomic(),ga._write_window():
                    prior=self._record(cabinet,command_id,'journey.prepare',descriptor,{},state='reserved')
            # Start only constructs the existing reviewable Enhance plan. Neither
            # this form nor a replay confirms source access or personal inference.
            try:
                outcome=get_service(self.authority).start(session_ref,profile_ref=profile_ref,
                    connection_ref=connection_ref,client_key=client_key,refresh_nonce=refresh_nonce,
                    **({'noise':noise} if noise is not None else {}))
            except (ga.AuthorityFault,EnhanceFault,DisclosureError) as fault:
                # Start only plans. A known refusal with no admitted owned run
                # is a completed no-effect command. An admitted child or unknown
                # exception retains the reservation for exact receipt resumption.
                if not SynthesisRun.objects.filter(installation=cabinet.installation,actor_id=cabinet.actor_id,
                        profile_id=cabinet.profile_id,contract_version='enhance/v1',client_key=client_key).exists():
                    ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
                    with transaction.atomic(),ga._write_window():
                        PortableJourneyCommand.objects.filter(pk=prior.pk,state='reserved').update(state='committed',
                            result=dict(schema='journey-prepare-refusal/v1',profile_ref=profile_ref,
                                journey_ref=journey_ref,revision=journey.revision,code=fault.code,
                                status=getattr(fault,'status',422)))
                raise ga.AuthorityFault(fault.code,getattr(fault,'status',422)) from None
            run_ref=str(ga._uuid(outcome['run_ref']))
            with transaction.atomic(),ga._write_window():
                journey.run_ref=ga._uuid(run_ref);self._advance(journey)
                result=dict(profile_ref=profile_ref,journey_ref=journey_ref,revision=journey.revision,
                    run_ref=run_ref,connection_ref=connection_ref,code='PREPARED')
                PortableJourneyCommand.objects.filter(pk=prior.pk).update(state='committed',result=result)
            return result

    def project(self, session_ref, *, profile_ref=None):
        from .local_event_preferences import location_projection
        from .models import PortableJourneyState,ProfileCabinetState,PortableConnection,SavedCredentialBinding,FeedbackTarget
        from .source_connection_validation import SourceConnectionValidation
        from .portable_authority import AuthorityFault
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref),self.authority._lock:
            installation,session=ga._owner(self.authority,session_ref)
            if profile_ref is None:
                return journey_projection(profile_ref=None,profile_label='',journey_ref=None,revision=0,
                    step='identity',steps=[dict(step=key,label=key.title(),state='current' if key=='identity' else 'pending')
                        for key in STEPS],providers=[],favourite_categories=[],
                    categories=[dict(category=row.key,label=row.label,state='not_started',code='',count=0,
                        publication_ref=None,publication_revision=None,ranking_state='none',cards=[],
                        next_action=None,href=None) for row in CATEGORIES],
                    preparation=dict(state='not_started',run_ref=None,stage='',code='',href=None,
                        publication_ref=None,publication_revision=None,partial=False),completion_receipts=[],
                    actions=journey_actions(profile_ref=None,journey_ref=None,expected_revision=0,
                        owner_revision=session.owner_revision,step='identity'))
            cabinet,_=ga._bound(self.authority,session_ref,profile_ref)
            journey=PortableJourneyState.objects.filter(cabinet=cabinet).first()
            journey_ref=str(journey.pk if journey else uuid.uuid5(cabinet.profile_ref,'guided-owner-journey:v2'))
            rev=journey.revision if journey else 0;step=journey.step if journey else 'providers'
            connections=journey.connections if journey else {};slots=_current_favourite_slots(cabinet,journey)
            recent=True
            try:self.authority.authorize(session_ref,sensitive=True,touch=False)
            except AuthorityFault:recent=False
            actions=journey_actions(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=rev,
                owner_revision=session.owner_revision,step=step)
            actions['reauthenticate']=form('/setup/form/reauthenticate',{'passphrase':''},enabled=not recent)
            providers=[]
            checks=getattr(self.authority,'_guided_source_checks',None)
            if checks is None:
                checks=SourceConnectionValidation(self.authority);self.authority._guided_source_checks=checks
            for provider,adapters in PROVIDER_ADAPTERS.items():
                rows=list(PortableConnection.objects.filter(pk__in=connections.get(provider,[]),
                    installation=installation,removed=False).order_by('adapter'))
                bound=len(rows)==len(adapters)
                keyed=provider in ('openai','tmdb','hardcover')
                descriptions=[self.authority._source_vault.describe(session_ref,row.pk,row.config_revision)
                              for row in rows] if keyed else []
                saved=[SavedCredentialBinding.objects.filter(pk=row.pk).first() for row in rows] if keyed else []
                all_saved=bool(bound and saved and all(s and not s.removed for s in saved))
                available=bool(all_saved and all(d.get('credential_available') for d in descriptions))
                storage=('pending_delete' if any(s and s.state=='PENDING_DELETE' for s in saved) else
                    'pending_write' if any(s and s.state=='PENDING_WRITE' for s in saved) else
                    'saved_available' if available else 'saved_locked' if all_saved and
                    any(d.get('credential_store_state')=='locked' for d in descriptions) else
                    'saved_unavailable' if all_saved else 'removed' if any(s and s.removed for s in saved) else 'absent')
                test=dict(state='not_tested',test_scope='',test_current=False,observed_at=None,expires_at=None,code='')
                if not keyed:test.update(state='not_applicable',test_scope='collection_outcomes')
                elif provider=='openai' and bound:
                    from django.conf import settings
                    status=settings.PORTABLE_CONNECTIONS.status(session_ref,rows[0].pk,rows[0].config_revision)
                    test.update(state='passed' if status['connection_ready'] else 'not_tested',
                        test_current=status['connection_ready'],test_scope='neutral_model_transport')
                elif bound and all_saved:
                    try:test=checks.current(session_ref,profile_ref=profile_ref,provider=provider)
                    except AuthorityFault as fault:test['code']=fault.code
                base=dict(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=rev)
                config=form('/cabinet/journey/providers/'+provider+'/configure',
                    {**base,'command_id':str(uuid.uuid4()),'confirmed':'yes'},enabled=recent)
                save=form('/cabinet/journey/providers/'+provider+'/save',
                    {**base,'command_id':str(uuid.uuid4()),'secret':'','storage':'persistent'},
                    enabled=bool(keyed and bound and recent and storage not in ('pending_write','pending_delete','saved_locked')))
                test_action=form('/cabinet/journey/providers/'+provider+'/test',
                    {**base,'command_id':str(uuid.uuid4()),'confirmed':'yes'},enabled=bool(keyed and bound and available and recent))
                if provider=='openai':
                    test_action=form('/setup/form/preview',{'connection_ref':str(rows[0].pk) if bound else '',
                        'revision':rows[0].config_revision if bound else 0},enabled=bool(bound and available and recent))
                providers.append(dict(provider_id=provider,label=PROVIDER_LABELS[provider],optional=provider!='openai',
                    required_for_inference=provider=='openai',connection_refs=[str(row.pk) for row in rows],
                    config_revisions=[row.config_revision for row in rows],storage_state=storage,
                    credential_saved=all_saved,credential_available=available,test_state=test['state'],
                    test_scope=test.get('test_scope',''),test_current=test['test_current'],
                    test_observed_at=test['observed_at'],test_expires_at=test['expires_at'],test_code=test['code'],
                    next_action='configure' if not bound else 'save' if keyed and not all_saved else
                        'test' if keyed and not test['test_current'] else 'continue',
                    actions=({ 'configure':config, 'save':save, **({'test':test_action} if provider!='openai' else {})}
                        if keyed else {'configure':config})))
            favourites=[]
            for category in INTAKE_CATEGORIES:
                entries=[]
                for number in range(1,6):
                    stored=slots.get(category.key+':'+str(number));title='';entry=None;saved_revision=0
                    if stored:
                        entry=stored['entry_ref'];saved_revision=stored['saved_revision']
                        namespace='owner-stated-visual-artist' if category.key=='visual_artist' else 'owner-stated-favourite'
                        target=FeedbackTarget.objects.filter(source_namespace=namespace,
                            source_key=profile_ref+':'+entry,category=category.key,deleted=False).first()
                        ga._require(target is not None,'REVISION_CONFLICT',409)
                        title=target.title
                    value=favourite_slot(profile_ref=profile_ref,journey_ref=journey_ref,expected_revision=rev,
                        category=category.key,slot=number,entry_ref=entry,title=title,saved_revision=saved_revision)
                    for action in value['actions'].values():action['enabled']=action['enabled'] and recent
                    entries.append(value)
                favourites.append(dict(category=category.key,label=category.label,group=category.group,slots=entries))
            categories=[dict(category=row.key,label=row.label,state='not_started',code='',count=0,
                publication_ref=None,publication_revision=None,ranking_state='none',cards=[],next_action='prepare',
                href='/cabinet?profile_ref='+profile_ref+'&category='+row.key) for row in CATEGORIES]
            preparation=dict(state='not_started',run_ref=str(journey.run_ref) if journey and journey.run_ref else None,
                stage='',code='',href=None,publication_ref=None,publication_revision=None,partial=False)
            service=getattr(self.authority,'_enhance_service',None)
            if preparation['run_ref'] and service is not None:
                status=service.status(session_ref,run_ref=preparation['run_ref'])
                preparation.update(state=status['state'],stage=status['stage'],code=status.get('code',''),
                    href='/enhance?run_ref='+preparation['run_ref'])
            if cabinet.active_manifest_ref and service is not None:
                try:
                    current=service.current_for_interaction(session_ref,profile_ref=profile_ref)
                    indexed={row['category']:row for row in current['manifest']['categories']}
                    for category in categories:
                        row=indexed[category['category']]
                        category.update(state=row['state'],code=row['code'],count=len(row['cards']),cards=row['cards'],
                            publication_ref=current['publication_ref'],publication_revision=current['publication_revision'],
                            ranking_state=current['ranking_state'],next_action='enhance')
                    preparation.update(publication_ref=current['publication_ref'],
                        publication_revision=current['publication_revision'],partial=any(r['state']!='fresh' for r in categories))
                except AuthorityFault as fault:
                    for category in categories:category.update(state='unavailable',code=fault.code)
            model=next((row for row in providers if row['provider_id']=='openai'),None)
            if model and model['test_current'] and model['connection_refs']:
                actions['prepare']['fields']['connection_ref']=model['connection_refs'][0]
                actions['prepare']['enabled']=recent
                actions['prepare']['disabled_reason']='' if recent else 'REAUTHENTICATION_REQUIRED'
            next_noise=journey.next_noise if journey is not None else 25
            actions['prepare']['fields']['noise']=next_noise
            return journey_projection(profile_ref=profile_ref,profile_label=cabinet.profile.label,
                journey_ref=journey_ref,revision=rev,step=step,
                steps=[dict(step=key,label=key.title(),state='current' if key==step else
                    'completed' if journey and key in journey.completed else
                    'skipped' if journey and key in journey.skipped else 'pending') for key in STEPS],
                providers=providers,favourite_categories=favourites,categories=categories,
                preparation=preparation,completion_receipts=[],actions=actions,
                location=location_projection(cabinet,journey_ref=journey_ref,expected_revision=rev,enabled=recent),next_noise=next_noise)


PROVIDER_ADAPTERS = {'openai':('openai_responses',),'tmdb':('tmdb_movie','tmdb_tv'),
    'hardcover':('hardcover',),'musicbrainz':('musicbrainz_recording','musicbrainz_artist'),
    'apple':('apple_podcast',)}

PROVIDER_LABELS = {'openai':'OpenAI','tmdb':'TMDB','hardcover':'Hardcover','musicbrainz':'MusicBrainz','apple':'Apple Podcasts'}
