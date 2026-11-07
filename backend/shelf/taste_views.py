"""Authenticated unified ratings; source reads and synthesis require explicit POST."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from shelf import taste_funnel as funnel, taste_selection, synthesis
from shelf import discovery_views, discovery_sources, discovery_state as storage
from shelf import intake, provider_catalogue, durable_intake
from shelf.models import DiscoveryState, SynthesisRun
from shelf.provider_views import _profile
from shelf.event_plan_views import _guard


class ActionForm(forms.Form):
    operation=forms.ChoiceField(choices=[(s,s) for s in ('rate','save','unsave','funnel_skip','funnel_next','undo')])
    key=forms.CharField(required=False,max_length=160)
    basis=forms.ChoiceField(required=False,choices=[('interest','Anticipated interest'),('enjoyment','Experienced enjoyment')])
    value=forms.TypedChoiceField(required=False,choices=[('', 'Clear this rating')]+[(i,str(i)) for i in range(1,6)],coerce=int,empty_value=None)
    experienced=forms.BooleanField(required=False)
    revision=forms.IntegerField(min_value=0)
    nonce=forms.CharField(max_length=200)
    preview=forms.CharField(max_length=12000)

    def clean(self):
        data=super().clean();operation=data.get('operation')
        if operation=='rate' and (not data.get('key') or not data.get('basis')):
            raise forms.ValidationError('Choose an identity and rating meaning.')
        if operation=='rate' and data.get('basis')=='enjoyment' and not data.get('experienced'):
            raise forms.ValidationError('Confirm you experienced this item before recording enjoyment.')
        if operation!='rate' and (data.get('basis') or data.get('value') is not None or data.get('experienced')):
            raise forms.ValidationError('Unexpected rating fields.')
        if operation in ('undo','funnel_next') and data.get('key'):
            raise forms.ValidationError('Unexpected item identity.')
        return data


class FetchForm(forms.Form):
    revision=forms.IntegerField(min_value=0)
    nonce=forms.CharField(max_length=200)
    confirmed=forms.BooleanField(required=True)


class SynthesisForm(forms.Form):
    expected_digest=forms.RegexField(r'^[a-f0-9]{64}$')
    confirmed=forms.BooleanField(required=True)


def fields(request,form_type):
    # CSRF middleware may already have consumed multipart input. Bound the
    # parsed scalar form without trying to read that stream a second time.
    try:content_length=int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:return None
    if not 0<=content_length<=16384 or request.FILES:return None
    form=form_type(request.POST)
    if set(request.POST)-{*form.fields,'csrfmiddlewaretoken'}:
        return None
    if any(len(request.POST.getlist(key))!=1 for key in request.POST):return None
    if sum(len(key.encode())+len(value.encode()) for key,value in request.POST.items())>16384:
        return None
    return form.cleaned_data if form.is_valid() else None


def source_failure(failure):
    lane={'books_graphic_novels':'Graphic novels','books_popular':'Books',
        **{key:value[2] for key,value in provider_catalogue.LANES.items()}}.get(failure.get('lane'),'Catalogue source')
    reason={'auth':'provider rejected authorization','metadata':'existing access metadata unavailable',
        'rate':'provider rate limited this request','timeout':'provider timed out',
        'tls':'secure connection unavailable','graphql':'provider could not complete this query'}.get(failure.get('status'),'source response unavailable')
    return lane+': '+reason+'. No automatic retry.'


def headers(response):
    response['Cache-Control']='private, no-store';response['Referrer-Policy']='no-referrer'
    return response


@login_required
@require_GET
@_guard
def index(request,profile_id):
    if set(request.GET)-{'responses'} or len(request.GET.getlist('responses'))>1:
        return HttpResponse('Unknown funnel filter.',status=400)
    try:response_page=int(request.GET.get('responses','1'))
    except ValueError:return HttpResponse('Invalid response page.',status=400)
    if not 1<=response_page<=21:return HttpResponse('Invalid response page.',status=400)
    row,projection,cards=funnel.pool(request,profile_id)
    selected=taste_selection.select(cards,projection)
    historical=list(projection['cards'].values())
    originals={card['key']:card for card in cards}
    recent=list(reversed(historical))[(response_page-1)*24:response_page*24]
    rendered=[originals.get(card['key'],card) for card in recent]+[r['card'] for r in selected['selection']]
    signed=funnel.preview(request,profile_id,row,rendered,
        shown=[r['card']['key'] for r in selected['selection']])
    def display(card,why=()):
        card=originals.get(card['key'],card)
        return {'card':card,'why':why,'ratings':projection['ratings'].get(card['key'],{}),
                'saved':card['key'] in projection['saved']}
    snapshot=funnel.synthesis_snapshot(request.user,profile_id)
    jobs=[]
    if SynthesisRun._meta.db_table in connection.introspection.table_names():
        jobs=[{'run':run,'display':synthesis.status(run,snapshot['input_digest'])}
            for run in SynthesisRun.objects.filter(actor=request.user,profile_id=profile_id).order_by('-created_at')[:5]]
        titles={signal['key']:signal['title'] for signal in snapshot['signals']}
        for job in jobs:
            output=job['display'].get('output')
            if output:
                job['clusters']=[dict(cluster,titles=[titles.get(key,key) for key in cluster['members']])
                    for cluster in output['clusters']]
                job['links']=[dict(link,from_title=titles.get(link['from'],link['from']),to_title=titles.get(link['to'],link['to']))
                    for link in output['links']]
    storage.public_authorized(request,profile_id)
    return headers(render(request,'cabinet/taste_funnel.html',{'selected_profile':_profile(request,profile_id,'select'),
        'cards':[display(r['card'],r['why']) for r in selected['selection']],
        'responses':[display(card) for card in recent],
        'response_page':response_page,'previous_responses':response_page-1 if response_page>1 else None,
        'next_responses':response_page+1 if len(historical)>response_page*24 else None,
        'projection':projection,'selection':selected,'preview':signed,
        'revision':row.revision if row else 0,'nonce':row.nonce if row else storage.initial_nonce(request.user.pk,profile_id,'household'),
        'snapshot':snapshot,'jobs':jobs,'source_results':request.session.get('funnel_source_results',[]),
        'sources':discovery_sources.source_statuses()}))


@login_required
@require_POST
@_guard
def action(request,profile_id):
    data=fields(request,ActionForm)
    if data is None:return HttpResponse('Invalid rating form.',status=400)
    funnel.commit(request,profile_id,data)
    return redirect('taste_funnel',profile_id=profile_id)


@login_required
@require_POST
@_guard
def synergize(request,profile_id):
    data=fields(request,SynthesisForm)
    if data is None:return HttpResponse('Invalid synthesis form.',status=400)
    synthesis.request(request,profile_id,data['expected_digest'])
    return redirect('taste_funnel',profile_id=profile_id)


@login_required
@require_POST
@_guard
def fetch_round(request,profile_id):
    data=fields(request,FetchForm)
    if data is None:return HttpResponse('Invalid source form.',status=400)
    storage.public_authorized(request,profile_id)
    fingerprint=funnel.digest(['funnel-source-read',data])
    with transaction.atomic():
        row=funnel.owned(request.user,profile_id)
        if row is None:
            if data['revision']!=0 or data['nonce']!=storage.initial_nonce(request.user.pk,profile_id,'household'):
                raise storage.Conflict('Stale first source request.')
            row=DiscoveryState.objects.create(actor=request.user,profile_id=profile_id,mode='household',nonce=data['nonce'])
        if data['nonce'] in row.receipts:
            if row.receipts[data['nonce']]!=fingerprint:raise storage.Conflict('Form already used.')
            return redirect('taste_funnel',profile_id=profile_id)
        if row.revision!=data['revision'] or row.nonce!=data['nonce'] or len(row.receipts)>=1000:
            raise storage.Conflict('Stale source request. Reload.')
        import secrets
        next_nonce=secrets.token_urlsafe(24)
        updated=DiscoveryState.objects.filter(pk=row.pk,revision=row.revision,nonce=row.nonce).update(
            revision=row.revision+1,nonce=next_nonce,receipts={**row.receipts,row.nonce:fingerprint})
        if updated!=1:raise storage.Conflict('Concurrent source request.')
        row.refresh_from_db()
    # One explicit bounded fetch; the durable reservation above prevents a
    # double-click from repeating network work. Provider failures remain visible.
    report=[]
    cards,failures=provider_catalogue.initial_deck(actor_id=request.user.pk,profile_id=profile_id)
    storage.check_public_result(request,profile_id,row)
    try:
        graphic_novels,_=provider_catalogue.browse_books('graphic_novels',actor_id=request.user.pk,profile_id=profile_id)
        cards.extend(graphic_novels)
    except provider_catalogue.CatalogueError as error:
        failures.append({'lane':'books_graphic_novels','status':error.status.value})
    storage.check_public_result(request,profile_id,row)
    state=intake.load(request.session,request.user.pk,profile_id) or intake.new_state(request.user.pk,profile_id)
    intake.merge(state,cards);state['started']=True
    report.append({'name':'Films, series and books','status':str(len(cards))+' source candidates',
        'detail':'; '.join(source_failure(f) for f in failures) or 'Provider retrieval; no response inferred.'})
    entries=discovery_views.public_entries(request,profile_id)
    for operation,name in (('rrr','Triple R'),('beat','Beat Magazine'),('pbs','PBS'),('tribe','Tribe Soundsystem'),('soundsystems','Sound Systems of Melbourne')):
        storage.check_public_result(request,profile_id,row)
        result=discovery_sources.fetch_public(operation,'','',timezone.now())
        storage.check_public_result(request,profile_id,row)
        entries[(operation,'','')]={'outcome':result,'at':timezone.now(),'source':name}
        report.append({'name':name,'status':result.status,'detail':result.message})
    # Existing verified artist identities provide independent starting points;
    # no follower graph, private social data or inferred preference is imported.
    seeds=[c for c in discovery_sources.household_candidates() if c.kind=='artist' and c.key.startswith('musicbrainz:artist:')][:2]
    for seed in seeds:
        storage.check_public_result(request,profile_id,row)
        result=discovery_sources.fetch_public('radio',seed.key.split(':')[-1],'medium',timezone.now())
        storage.check_public_result(request,profile_id,row)
        entries[('radio',seed.key.split(':')[-1],'medium')]={'outcome':result,'at':timezone.now(),'source':'ListenBrainz · '+seed.title}
        report.append({'name':'ListenBrainz · '+seed.title,'status':result.status,'detail':result.message})
    discovery_views.PUBLIC_CACHE.set(discovery_views.cache_key(request,profile_id),entries,timeout=900)
    storage.check_public_result(request,profile_id,row)
    request.session[intake.SESSION_KEY]=state
    request.session['funnel_source_results']=report
    return redirect('taste_funnel',profile_id=profile_id)
