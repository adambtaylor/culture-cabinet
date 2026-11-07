"""Read-only overview of existing owned rounds, without a new preference store."""
from copy import deepcopy
from datetime import timedelta, date
from zoneinfo import ZoneInfo

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from shelf import durable_intake as durable, intake
from shelf import discovery_reducer, discovery_sources, discovery_views
from shelf.authority import SELECTION_KEY
from shelf.models import DiscoveryState, IntakeRun
from shelf.provider_views import _profile

CHOICES = dict(discovery_reducer.CHOICE_LABELS)


def _catalogue(request, profile_id, now, tables):
    """Refresh choices from their owned run; cached source content keeps its TTL.

    Unlike the intake page this read path never upgrades/imports a session or
    writes a selected run. A stale tab cannot revive a cleared human response.
    """
    raw = request.session.get(intake.SESSION_KEY)
    cached = deepcopy(raw) if (raw and raw.get('actor_id') == request.user.pk
        and raw.get('profile_id') == profile_id and raw.get('expires_at', 0) > now.timestamp()) else None
    run = None
    if IntakeRun._meta.db_table in tables:
        runs = IntakeRun.objects.filter(actor=request.user, profile_id=profile_id, archived_at__isnull=True)
        if cached and cached.get('run_id'):
            run = runs.filter(pk=cached['run_id']).first()
            if run is None:
                cached = None
        elif not cached or not cached.get('candidates'):
            run = runs.first()
    state = durable.hydrate(run, cached) if run else cached
    if state is None:
        return [], [], [], None
    if not run:
        # Recalculate on a copy so the overview does not consume the current round.
        intake.select_round(state)
    rows = []
    for key in state['current']:
        card = state['candidates'][key]
        root = state['candidates'].get(card.get('root'))
        if root and state['choices'].get(root['key']) in ('enjoyed', 'interested'):
            why = f"Provider suggestion from {root['title']} · {CHOICES[state['choices'][root['key']]]}."
        elif card['origin'] == 'search':
            why = 'From your search; awaiting your response.'
        else:
            why = 'Exploration from the source catalogue; no direct relationship established.'
        fetched = discovery_sources.timestamp(card['fetched_at'])
        freshness = ('Retrieval time unknown' if fetched is None else
            'Retrieval time is in the future; freshness unknown' if fetched > now else
            'Older retrieval; source updates unknown' if now - fetched >= timedelta(seconds=intake.TTL) else
            'Recently retrieved; source updates unknown')
        category = (card.get('book_metadata') or {}).get('category', {})
        rows.append({'key': key, 'title': card['title'], 'medium': card['medium'],
            'url': card['url'], 'source': 'Hardcover' if card['provider'] == 'hardcover' else 'TMDB',
            'provenance': 'Provider catalogue retrieval', 'retrieved_at': card['fetched_at'],
            'freshness': freshness, 'why': [why],
            'classification': 'Graphic novel' if category.get('status') == 'present' and category.get('id') == 4 else '',
            'description': card.get('description', '')})
    roots = intake.positive_roots(state)[:5]
    lanes = [{'title': card['title'], 'choice': CHOICES[state['choices'][card['key']]],
              'source': 'Film, series & books', 'url': card['url'],
              'count': sum(c.get('root') == card['key'] and k not in state['choices']
                           for k, c in state['candidates'].items() if k not in state.get('history_only', []))}
             for card in roots]
    # Historical identity snapshots support correction, never current coverage.
    cached_sources = [c['provider'] for key, c in state['candidates'].items()
                      if key not in state.get('history_only', [])] if cached else []
    return rows, lanes, cached_sources, run


def _discovery(request, profile_id, now, tables):
    stored = (DiscoveryState.objects.filter(actor=request.user, profile_id=profile_id,
              mode='household').first() if DiscoveryState._meta.db_table in tables else None)
    history = stored.history if stored else []
    state = discovery_reducer.replay(history)
    cards = discovery_views.candidates(request, profile_id, 'household', history)
    # Old event dates remain available for correction in discovery, but do not
    # occupy recommendation slots. Unknown dates stay explicitly unknown.
    def past(card):
        # An opening is a separate encounter from the continuing exhibition.
        # Never infer a run end where the source did not supply one.
        end = getattr(card, 'exhibition_ends_on', None)
        if end:
            return date.fromisoformat(end) < now.astimezone(ZoneInfo('Australia/Melbourne')).date()
        start = discovery_sources.timestamp(card.starts_at)
        return card.kind == 'event' and start is not None and start <= now

    eligible = [c for c in cards if c.provenance != 'fixture'
        and (c.url is not None or state['choices'].get(c.key) in ('enjoyed', 'interested')) and not (
        past(c))]
    ranked = discovery_reducer.select(eligible, state, unjudged_only=True)
    from shelf import gallery_curation
    selected_keys = {item['card'].key for item in ranked['selection']}
    for card in gallery_curation.select((c for c in eligible if not state['choices'].get(c.key)),
            actor_id=request.user.pk, profile_id=profile_id, limit=2):
        if card.key not in selected_keys:
            ranked['selection'].append({'card': card, 'why': ()})
            selected_keys.add(card.key)
    rows = []
    for item in ranked['selection']:
        card = item['card']
        rows.append({'key': card.key, 'title': card.title,
            'medium': 'music' if card.kind in ('artist', 'recording') else 'event',
            'url': card.url, 'source': card.source, 'provenance': card.provenance,
            'retrieved_at': card.retrieved_at, 'freshness': card.freshness(now),
            'why': tuple(item['why']) + gallery_curation.reasons(card, actor_id=request.user.pk, profile_id=profile_id),
            'date_status': card.date_status(now) if card.kind == 'event' else '',
            'starts_at': card.starts_at, 'venue': card.venue,
            'rrr_event': card if getattr(card, 'source_id', '') == 'rrr' else None,
            'gallery_event': card if hasattr(card, 'opening_date') else None,
            'availability': 'Ticket availability, price and cancellation status unverified.' if card.kind == 'event' else '',
            'classification': 'Place to explore' if card.kind == 'venue' else ''})
    by_key = {c.key: c for c in eligible}
    lanes = [dict(lane, choice=CHOICES[lane['choice']], source='Music & events',
                  url=by_key[lane['key']].url) for lane in ranked['lanes']]
    return rows, lanes, stored


@login_required
@require_GET
def home(request):
    target = request.session.get(SELECTION_KEY)
    return redirect('profiles') if target is None else index(request, target)


@login_required
@require_GET
def index(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    if request.GET:
        raise Http404
    now = timezone.now()
    tables = connection.introspection.table_names()
    catalogue, catalogue_lanes, cached_sources, run = _catalogue(request, profile_id, now, tables)
    discovery, discovery_lanes, stored = _discovery(request, profile_id, now, tables)
    sections = []
    for medium, title, action, empty in (
        ('movie', 'Films', 'catalogue', 'Fetch a catalogue round or search for a film.'),
        ('tv', 'Series', 'catalogue', 'Fetch a catalogue round or search for a series.'),
        ('book', 'Books & graphic novels', 'catalogue', 'Search books or choose Fetch graphic novels in the catalogue.'),
        ('music', 'Music', 'discovery', 'Confirm an artist seed or explicitly request artist radio.'),
        ('event', 'Events & places', 'discovery', 'Explore reported places or explicitly read a public event.')):
        sections.append({'id': medium, 'title': title, 'action': action, 'empty': empty,
                         'cards': [row for row in catalogue + discovery if row['medium'] == medium]})
    coverage = [{'name': name, 'status': 'Cached metadata available' if provider in cached_sources else 'Not fetched or expired',
                 'detail': 'Open the catalogue to request current results. A retrieval time is not the source update time.'}
                for provider, name in (('tmdb', 'TMDB film & series'), ('hardcover', 'Hardcover books & graphic novels'))]
    public_results = list(discovery_views.public_entries(request, profile_id).values())
    _profile(request, profile_id, 'select')
    return render(request, 'cabinet/recommendations.html', {
        **discovery_views.gallery_context(request, profile_id, now),
        'selected_profile': profile, 'sections': sections,
        'lanes': catalogue_lanes + discovery_lanes, 'coverage': coverage,
        'public_results': public_results, 'sources': discovery_sources.source_statuses(),
        'intake_revision': run.revision if run else None,
        'discovery_revision': stored.revision if stored else None,
        'temporary_notice': 'Current rounds only. Catalogue content expires after one hour; public music/event results after 15 minutes. Opening this page does not refresh sources or record a preference.',
    })
