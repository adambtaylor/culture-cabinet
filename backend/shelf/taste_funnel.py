"""Unified rating UI over existing owned discovery history and provider caches.

Numeric responses never rewrite earlier qualitative responses or provider facts.
Only judged identity metadata is durable; source decks retain their existing TTL.
"""
from copy import deepcopy
import json
import secrets
import time

from django.core import signing
from django.db import transaction
from django.utils import timezone

from shelf import discovery_state as storage, discovery_views, discovery_sources, intake, durable_intake
from shelf import taste_selection, social_event_intake, authored_discovery, authored_observations
from shelf.calendar_handoff import digest
from shelf.feedback_scale import REACTION_LABELS
from shelf.models import DiscoveryState, IntakeRun, SavedItem, SocialDiscoveryState, AuthoredDiscoveryState
from shelf.provider_views import _profiles

MEDIA=('movie','tv','book','music','event')


def authorize(actor,profile_id):
    if not _profiles(actor,'select').filter(pk=profile_id).exists():
        from django.http import Http404
        raise Http404


def owned(actor,profile_id):
    authorize(actor,profile_id)
    return DiscoveryState.objects.filter(actor=actor,profile_id=profile_id,mode='household').first()


def state_for(request,profile_id):
    storage.public_authorized(request,profile_id)
    row=owned(request.user,profile_id)
    projection=taste_selection.project(row.history if row else [])
    existing=[f"{item.provider}:{item.medium}:{item.source_id}" for item in SavedItem.objects.filter(actor=request.user,profile_id=profile_id,list_kind='favorites')]
    projection['saved']=list(dict.fromkeys(projection['saved']+existing))
    return row,projection


def catalogue_card(card):
    selected=durable_intake.snapshot(card)
    return {'key':selected['key'],'title':selected['title'],'medium':selected['medium'],
        'source':'Hardcover' if selected['provider']=='hardcover' else 'TMDB','url':selected['url'],
        'provenance':'live-automated','lane':selected['lane'],'roots':[selected['root']] if selected['root'] else [],
        'fetched_at':selected['fetched_at']}


def pool(request,profile_id):
    row,projection=state_for(request,profile_id)
    cards={}
    cached=intake.load(request.session,request.user.pk,profile_id)
    if cached:
        for key,card in cached['candidates'].items():
            if key not in cached.get('history_only',[]):cards[key]=catalogue_card(card)
    for card in discovery_views.candidates(request,profile_id,'household',row.history if row else []):
        if card.provenance=='fixture' or card.kind not in ('event','artist','recording') or not card.url:
            continue
        # Source constraints affect this deck, never the meaning of a rating.
        start=discovery_sources.timestamp(card.starts_at)
        if card.kind=='event' and start and start<=timezone.now():continue
        cards[card.key]={'key':card.key,'title':card.title,'medium':'event' if card.kind=='event' else 'music',
            'source':card.source,'url':card.url,'provenance':card.provenance,
            'lane':('venue:'+card.venue_key) if card.venue_key else card.source+':'+card.category,
            'roots':list(card.roots),'fetched_at':card.retrieved_at,
            'date_label':card.date_status(timezone.now()) if card.kind=='event' else '',
            'source_date':card.starts_at,'venue':card.venue,
            'source_freshness':card.freshness(timezone.now()),
            'source_publication':getattr(card,'publication_label',''),
            'source_build':getattr(card,'feed_build_label',''),
            'location_status':getattr(card,'location_status',''),
            'source_attributes':getattr(card,'source_attributes',()),
            'availability':'Unverified; rating interest does not reserve or confirm attendance.' if card.kind=='event' else ''}
    authorize(request.user,profile_id)
    return row,projection,list(cards.values())


def identity_snapshot(card):
    return {name:deepcopy(card[name]) for name in ('key','title','medium','source','url','provenance','lane','roots','fetched_at')}


def preview(request,profile_id,row,cards,*,shown):
    return signing.dumps({'actor':request.user.pk,'profile':profile_id,
        'revision':row.revision if row else 0,'nonce':row.nonce if row else storage.initial_nonce(request.user.pk,profile_id,'household'),
        'cards':{c['key']:digest(identity_snapshot(c)) for c in cards},'shown':shown,
        'favourites':digest(list(SavedItem.objects.filter(actor=request.user,profile_id=profile_id,list_kind='favorites').order_by('id').values_list('id',flat=True)))},salt='taste-funnel-v1',compress=True)


def commit(request,profile_id,fields):
    storage.public_authorized(request,profile_id)
    row,projection,cards=pool(request,profile_id)
    known={c['key']:c for c in cards}
    known.update({key:value for key,value in projection['cards'].items() if key not in known})
    try:bound=signing.loads(fields['preview'],salt='taste-funnel-v1',max_age=900)
    except signing.BadSignature:raise storage.Conflict('This round expired. Reload before rating.') from None
    operation,key=fields['operation'],fields.get('key','')
    fingerprint=digest(fields)
    if bound.get('actor')!=request.user.pk or bound.get('profile')!=profile_id:
        raise storage.Conflict('The selected profile changed.')
    if operation in ('rate','funnel_skip','save','unsave'):
        if key not in known or bound['cards'].get(key)!=digest(identity_snapshot(known[key])):
            raise storage.Conflict('The source identity changed or was not shown. Reload.')
        card=identity_snapshot(known[key])
    else:card=None
    from shelf.grant_authority import writer_window
    with transaction.atomic(), writer_window(request.user, profile_id):
        storage.public_authorized(request,profile_id)
        row=owned(request.user,profile_id)
        if row is None:
            if fields['revision']!=0 or fields['nonce']!=storage.initial_nonce(request.user.pk,profile_id,'household'):
                raise storage.Conflict('Stale first round.')
            row=DiscoveryState.objects.create(actor=request.user,profile_id=profile_id,mode='household',nonce=fields['nonce'])
        if fields['nonce'] in row.receipts:
            if row.receipts[fields['nonce']]!=fingerprint:raise storage.Conflict('This form was used for another action.')
            return row
        if (row.revision!=fields['revision'] or row.nonce!=fields['nonce'] or bound['revision']!=row.revision
                or bound['nonce']!=row.nonce):raise storage.Conflict('Another tab changed your responses. Reload.')
        if len(row.history)>=500 or len(row.receipts)>=1000:raise storage.Conflict('History limit reached. Export before continuing.')
        current_favourites=digest(list(SavedItem.objects.filter(actor=request.user,profile_id=profile_id,list_kind='favorites').order_by('id').values_list('id',flat=True)))
        if current_favourites!=bound['favourites']:raise storage.Conflict('Your favourites changed in another view. Reload.')
        event={'operation':operation,'key':key,'choice':'','at':timezone.now().isoformat(),
            'origin':'authenticated-human-post','mode':'household'}
        if card:
            event['card']=card
            event['source']={'name':card['source'],'provenance':card['provenance'],'rank':0,'retrieved_at':card['fetched_at']}
        if operation=='rate':event.update(basis=fields['basis'],value=fields['value'],experienced=fields['experienced'])
        elif operation=='funnel_next':
            ranked=taste_selection.select(cards,taste_selection.project(row.history))
            shown=[r['card']['key'] for r in ranked['selection']]
            if not shown or shown!=bound.get('shown'):
                raise storage.Conflict('The current source round changed or expired. Reload.')
            event['shown']=shown
        elif operation not in ('funnel_skip','save','unsave','undo'):
            raise ValueError('Unknown rating action.')
        history=row.history+[event]
        if operation in ('save','unsave') and card['medium'] in ('movie','tv','book'):
            # Catalogue favourites keep their existing canonical store and Obtain
            # links. They do not become numeric ratings or a second saved list.
            from shelf import saved
            if operation=='save':
                try:saved.save_candidate(actor=request.user,profile_id=profile_id,session=request.session,item_id=key,list_kind='favorites')
                except saved.SavedError:raise storage.Conflict('Refresh the catalogue identity before favouriting it.') from None
            else:
                provider,medium,source_id=key.split(':')
                SavedItem.objects.filter(actor=request.user,profile_id=profile_id,provider=provider,medium=medium,source_id=int(source_id),list_kind='favorites').delete()
            history=row.history
        taste_selection.project(history)  # Validate typed values and experience before persistence.
        storage.public_authorized(request,profile_id)
        changed=DiscoveryState.objects.filter(pk=row.pk,revision=row.revision,nonce=row.nonce).update(
            history=history,receipts={**row.receipts,row.nonce:fingerprint},revision=row.revision+1,nonce=secrets.token_urlsafe(24))
        if changed!=1:raise storage.Conflict('Concurrent response. Reload.')
        row.refresh_from_db();return row


def synthesis_snapshot(actor,profile_id):
    """Minimal explicit responses and owner-confirmed social intent, never inferred likes."""
    authorize(actor,profile_id)
    row=owned(actor,profile_id)
    projection=taste_selection.project(row.history if row else [])
    signals=[]
    def add(card,basis,value):
        if card and card.get('medium') in MEDIA:
            signals.append({k:card[k] for k in ('key','title','medium','source')}|{'basis':basis,'value':value})
    def add_feedback(card, choice, note=None):
        basis = 'anticipated_reaction' if choice in dict(REACTION_LABELS) else 'legacy_choice'
        add(card, basis, choice)
        if note:
            add(card, 'feedback_note', json.dumps({'observations': [{'reaction': choice, 'text': note}]},
                ensure_ascii=True, separators=(',', ':')))
    for key,ratings in sorted(projection['ratings'].items()):
        for basis,value in sorted(ratings.items()):add(projection['cards'].get(key),basis,value)
    for key in sorted(projection['saved']):add(projection['cards'].get(key),'favourite',True)
    for key,choice in sorted(projection['legacychoices'].items()):
        card=projection['cards'].get(key)
        if card is None and row:
            previous=next((e for e in reversed(row.history) if e.get('key')==key and e.get('source')),None)
            kind=key.split(':')[1] if ':' in key else ''
            if previous and kind in ('event','artist','recording'):
                card={'key':key,'title':'Title not retained for this earlier response','medium':'event' if kind=='event' else 'music','source':previous['source']['name']}
        add_feedback(card, choice, projection.get('notes', {}).get(key))
    revisions={'discovery':digest({k:projection[k] for k in ('ratings','saved','legacychoices')}),'intake':{},'saved_items':''}
    if row and any('note' in e or e.get('choice') in dict(REACTION_LABELS) for e in row.history):
        revisions['discovery_feedback'] = digest({'revision': row.revision, 'history': row.history})
    for run in IntakeRun.objects.filter(actor=actor,profile_id=profile_id,archived_at__isnull=True).order_by('id')[:20]:
        choices,cards=durable_intake.effective(run)
        feedback = durable_intake.effective_feedback(run)
        if choices:revisions['intake'][str(run.pk)]=digest(choices)
        if any('note' in e or e.get('choice') in dict(REACTION_LABELS) for e in run.history):
            revisions['intake'][str(run.pk)] = digest({'revision': run.revision, 'history': run.history})
        for key,choice in sorted(choices.items()):
            add_feedback(catalogue_card(cards[key]), choice, feedback[key].get('note'))
    saved=list(SavedItem.objects.filter(actor=actor,profile_id=profile_id,list_kind='favorites').order_by('provider','medium','source_id').values(
        'id','provider','medium','source_id','title')[:500])
    revisions['saved_items']=digest([{k:v for k,v in item.items() if k!='id'} for item in saved])
    for item in saved:
        add({'key':f"{item['provider']}:{item['medium']}:{item['source_id']}",'title':item['title'],
            'medium':item['medium'],'source':item['provider']},'favourite',True)
    social=SocialDiscoveryState.objects.filter(actor=actor,profile_id=profile_id,mode='household').first()
    social_data=social.data if social else {}
    signals.extend(social_event_intake.event_signals(social_data))
    # Bind provenance as well as emitted signals: a withdrawal or newly discovered
    # agent lineage must invalidate an earlier supervised synthesis receipt.
    revisions['social_intent']={'revision':social.revision if social else 0,
        'fingerprint':social_event_intake.event_revision(social_data)}
    authored=AuthoredDiscoveryState.objects.filter(actor=actor,profile_id=profile_id,mode='household').first()
    authored_data=authored.data if authored else authored_discovery.empty_data()
    signals.extend(authored_discovery.signals(authored_data))
    signals.extend(authored_observations.signals(authored_data))
    revisions['authored_discovery']={'revision':authored.revision if authored else 0,
        'fingerprint':authored_discovery.revision(authored_data)}
    # Collapse exact duplicate statements, retaining conflicting explicit legacy
    # statements visibly; no guessed cross-stream timestamps or numeric mapping.
    groups={}
    for signal in signals:groups.setdefault((signal['key'],signal['basis']),[]).append(signal)
    signals=[]
    for group in groups.values():
        signal=group[0].copy();values=sorted({str(s['value']) for s in group})
        if signal['basis'] == 'feedback_note':
            observations = {}
            for item in group:
                for observation in json.loads(item['value'])['observations']:
                    observations[json.dumps(observation, sort_keys=True)] = observation
            if len(observations) > 20:
                raise storage.Conflict('Choose a smaller note history before synthesis.')
            signal['value'] = json.dumps({'observations': [observations[k] for k in sorted(observations)]},
                ensure_ascii=True, separators=(',', ':'))
        elif len(values)>1:
            signal['value'] = values if signal['basis'] == 'anticipated_reaction' else 'Conflicting historical responses: '+' / '.join(values)
        signals.append(signal)
    signals.sort(key=lambda s:(s['key'],s['basis'],str(s['value'])))
    if len(signals)>500:raise storage.Conflict('Choose a smaller explicit history before synthesis.')
    result={'actor_id':actor.pk,'profile_id':profile_id,'mode':'household','revision_map':revisions,'signals':signals}
    result['input_digest']=digest(result)
    authorize(actor,profile_id)
    return result
