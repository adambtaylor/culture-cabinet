"""Owned nonfiction discovery, explicit source reads and inert portable exports."""
from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlsplit

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.cache import caches
from django.db import IntegrityError, OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_GET, require_POST

from shelf import authored_discovery as storage, podcast_sources
from shelf.discovery_state import Conflict, public_authorized
from shelf.provider_views import _profile
from shelf.taste_views import fields

CATALOG_PATH = Path(__file__).with_name('data') / 'nonfiction_catalog.json'
CLUSTERS = ('deep_dives', 'macroeconomics', 'sleep_health', 'living_soil', 'unusual_history')
SOURCE_CACHE = caches['default']
CACHE_SECONDS = 900


class ActionForm(forms.Form):
    mode = forms.ChoiceField(choices=[('household', 'household'), ('sample', 'sample')])
    action = forms.ChoiceField(choices=[(s, s) for s in ('seed_add', 'seed_remove',
        'seed_authorized_defaults', 'shortlist_add', 'shortlist_remove', 'refresh')])
    revision = forms.IntegerField(min_value=0)
    nonce = forms.CharField(max_length=200)
    identity = forms.CharField(max_length=200, required=False)
    topic = forms.CharField(max_length=120, required=False)
    terms = forms.CharField(max_length=120, required=False)
    source_text = forms.CharField(max_length=1000, required=False, strip=False)
    authorization = forms.CharField(max_length=500, required=False)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        action = data.get('action')
        extras = {'identity', 'topic', 'terms', 'source_text', 'authorization'}
        allowed = {'seed_add': {'topic', 'terms', 'source_text'},
                   'seed_authorized_defaults': {'authorization'}}.get(action, {'identity'})
        if any(data.get(k) for k in extras - allowed):
            raise forms.ValidationError('Unexpected action fields.')
        if any(not data.get(k) for k in allowed):
            raise forms.ValidationError('Complete the displayed fields.')
        if action == 'refresh' and (data.get('identity') not in CLUSTERS or data.get('mode') != 'household'):
            raise forms.ValidationError('Choose a displayed topic.')
        result = {k: data[k] for k in ('mode', 'action', 'revision', 'nonce') if k in data}
        result.update({k: data[k] for k in allowed if k in data})
        if action == 'seed_add':
            text = data['source_text']
            result = {k: result[k] for k in ('mode', 'action', 'revision', 'nonce')}
            result['seed'] = {'id': 'authored-' + sha256((data['topic'] + '\0' + text).encode()).hexdigest()[:24],
                'topic': data['topic'], 'terms': [data['terms']], 'personality_seeds': [],
                'source_text': text, 'provenance': 'user_authored',
                'authorization': 'Explicitly added through the nonfiction discovery form.'}
        return result


def headers(response):
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return headers(view(request, *args, **kwargs))
        except (Conflict, ValueError) as error:
            return headers(HttpResponse(str(error) + '\nReload nonfiction discovery before continuing.',
                                        status=409, content_type='text/plain'))
        except (IntegrityError, OperationalError):
            return headers(HttpResponse('Discovery storage is unavailable or busy. Reload before retrying.',
                                        status=503, content_type='text/plain'))
    return call


def mode_for(request):
    mode = request.GET.get('mode', 'household')
    if mode not in ('household', 'sample') or any(k != 'mode' or len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise Http404
    return mode


def safe_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.port else ''
    except ValueError:
        return ''


def display_episode(row):
    item = dict(row)
    for name in ('apple_url', 'source_url'):
        item[name] = safe_url(item.get(name))
    # Only exact episode links are actionable; a show or search URL is insufficient.
    if not item['apple_url'] or urlsplit(item['apple_url']).hostname != 'podcasts.apple.com' or '?i=' not in item['apple_url']:
        item['apple_url'] = ''
    item['books'] = [dict(book, **{name: safe_url(book.get(name)) for name in
        ('publisher_url', 'hardcover_url', 'source_url')}) for book in item.get('books', [])]
    for book in item['books']:
        if book['hardcover_url'] and urlsplit(book['hardcover_url']).hostname not in ('hardcover.app', 'www.hardcover.app'):
            book['hardcover_url'] = ''
    duration = item.get('duration_seconds')
    item['duration_label'] = str(round(duration / 60)) + ' min' if isinstance(duration, (float, int)) and duration > 0 else 'Duration unavailable'
    item['evidence_status'] = 'not_independently_appraised'
    return item


def catalog():
    """The checked-in source freeze is local data; this function never fetches."""
    content = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    return [dict(cluster, episodes=[display_episode(row) for row in cluster['episodes']])
            for cluster in content['clusters']]


def cache_key(request, profile_id, mode, cluster_id):
    owner = [request.user.pk, profile_id, mode, request.session.session_key, cluster_id]
    return 'nonfiction:' + sha256(json.dumps(owner).encode()).hexdigest()


def discovery_catalog(request, profile_id, mode):
    clusters = catalog()
    for cluster in clusters:
        cached = SOURCE_CACHE.get(cache_key(request, profile_id, mode, cluster['id']))
        cluster['refresh_status'] = cached.get('status', '') if cached else ''
        known = {row['key'] for row in cluster['episodes']}
        cluster['episodes'] += [display_episode(row) for row in (cached or {}).get('episodes', []) if row['key'] not in known]
    return clusters


def context(request, profile_id, mode):
    profile = _profile(request, profile_id, 'select')
    public_authorized(request, profile_id)
    ready = storage.available()
    state = storage.owned(request, profile_id, mode) if ready else None
    data = state.data if state else storage.empty_data()
    clusters = discovery_catalog(request, profile_id, mode)
    shortlist = [display_episode(row) for row in data['shortlist']]
    selected = {row['key'] for row in shortlist}
    for cluster in clusters:
        for episode in cluster['episodes']:
            episode['selected'] = episode['key'] in selected
    return {'selected_profile': profile, 'mode': mode, 'ready': ready, 'clusters': clusters,
        'seeds': data['seeds'], 'shortlist': shortlist,
        'revision': state.revision if state else 0,
        'nonce': state.nonce if state else storage.initial_nonce(request.user.pk, profile_id, mode),
        'default_authorization': storage.DEFAULT_AUTHORIZATION,
        'temporary_notice': 'Authored discovery requests and this shortlist are stored locally until you remove them. They are separate from ratings and likes.'}


@login_required
@require_GET
@guard
def index(request, profile_id):
    return render(request, 'cabinet/nonfiction_discovery.html', context(request, profile_id, mode_for(request)))


@login_required
@require_POST
@guard
def action(request, profile_id):
    public_authorized(request, profile_id)
    data = fields(request, ActionForm)
    if data is None:
        return HttpResponse('Invalid bounded discovery form.', status=400, content_type='text/plain')
    if not storage.available():
        raise Conflict('Discovery storage is unavailable.')
    clusters = discovery_catalog(request, profile_id, data['mode'])
    items = {row['key']: row for cluster in clusters for row in cluster['episodes']}
    if data['action'] == 'refresh':
        # State reserves the nonce atomically before this explicit, bounded read.
        state = storage.apply(request, profile_id, data, catalog=items)
        if state is None:
            return redirect('nonfiction_discovery', profile_id=profile_id)
        cluster = next(c for c in clusters if c['id'] == data['identity'])
        terms = next((s['terms'] for s in state.data['seeds'] if s['id'] == cluster['id']), [cluster['label']])
        try:
            episodes = podcast_sources.search_cluster(cluster['id'], terms)
            result = {'status': 'Source search completed; results are discovery leads.', 'episodes': episodes}
        except podcast_sources.PodcastSourceError as error:
            result = {'status': 'Source search unavailable (' + error.status + '). No automatic retry.', 'episodes': []}
        storage.check_result(request, profile_id, state)
        SOURCE_CACHE.set(cache_key(request, profile_id, data['mode'], cluster['id']), result, CACHE_SECONDS)
    else:
        storage.apply(request, profile_id, data, catalog=items)
    return redirect('/profiles/' + str(profile_id) + '/nonfiction/?mode=' + data['mode'])


@login_required
@require_GET
@guard
def export(request, profile_id):
    page = context(request, profile_id, mode_for(request))
    entries = []
    for episode in page['shortlist']:
        books = format_html_join('', '<li><strong>{}</strong> — {}<p>{}</p>{} {}</li>', (
            (book.get('title', ''), book.get('author', ''), book.get('why', ''),
             format_html('<a href="{}">Publisher record</a>', book['publisher_url']) if book['publisher_url'] else '',
             format_html('<a href="{}">Hardcover record</a>', book['hardcover_url']) if book['hardcover_url'] else '')
            for book in episode['books']))
        entries.append(format_html('<article><h2>{}</h2><p>{} · {} · {}</p><p>{}</p>{}<p>Source retrieved: {}. Research claims not independently appraised.</p>{}<ul>{}</ul></article>',
            episode.get('title', ''), episode.get('show', ''), episode.get('published_at') or 'Date unavailable',
            episode['duration_label'], episode.get('why', ''),
            format_html('<p><a href="{}">Open exact episode in Apple Podcasts</a></p>', episode['apple_url']) if episode['apple_url'] else '',
            episode.get('retrieved_at') or 'unknown',
            format_html('<p><a href="{}">Primary source record</a></p>', episode['source_url']) if episode['source_url'] else '', books))
    html = format_html('''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nonfiction shortlist</title><style>body{{font:17px/1.6 system-ui,sans-serif;max-width:48rem;margin:auto;padding:1.2rem;color:#242821;background:#faf8f2}}article{{border-top:1px solid #aaa;padding:1rem 0}}a{{overflow-wrap:anywhere}}h1,h2{{line-height:1.2}}</style></head><body><h1>Your nonfiction shortlist</h1><p>A portable copy of the episodes you saved. Open an episode in Apple Podcasts; add it to Playing Next there if you choose. This file does not change your queue, download audio or start playback.</p><p>This file has no account session or form tokens. Transfer it yourself to open it on another device. Exporting does not deliver it to your phone or sync apps.</p>{}</body></html>''',
        format_html_join('', '{}', ((entry,) for entry in entries)))
    response = HttpResponse(html, content_type='text/html; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="nonfiction-shortlist.html"'
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    return response
