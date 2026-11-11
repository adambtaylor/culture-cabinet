"""Event-only APW delegation; opening sessions and exhibition runs are separate."""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from html import escape

from shelf.discovery_sources import SourceError, timestamp
from shelf.event_sources import ListingEvent, EventEvidence, occurrence_event
from shelf.event_listing_transport import ListingIntervalPending
from shelf.gallery_sources import APW_URL, MAX_BYTES, parse_apw, _eligible, _document
from shelf.rrr_events import _one


def acquire(now):
    from shelf.gallery_transport import read, GalleryRateLimited
    try:
        # Delegate the exact homepage to its existing host lock/backoff/client.
        # The public path has no native authority token: True selects read's
        # nonwaiting mode, so a busy origin refuses instead of sleeping/retrying.
        # Normal discovery authority is revalidated by its enclosing handler.
        return read(APW_URL, revalidate=lambda: True)
    except GalleryRateLimited:
        raise ListingIntervalPending from None


@dataclass(frozen=True)
class APWRun(ListingEvent):
    exhibition_starts_on: str | None = None
    exhibition_ends_on: str | None = None
    opening_date: str | None = None

    def date_status(self, now):
        return ('Source exhibition run: '+self.listed_date+
            '. Calendar range only; opening sessions, daily hours and attendance are separate.')


def _event_panel(raw, retrieved_at):
    # The live homepage has five hero panels, most promotional. Select only a
    # panel with an explicit opening or run, then reuse the immutable gallery
    # field validator on those exact escaped labels. Provenance/hash still bind
    # the original response, never this in-memory metadata projection.
    heroes=[node for node in _document(raw,retrieved_at) if node.matches('div','sidebar__hero')]
    if not 1<=len(heroes)<=48:
        raise SourceError('Missing or oversized APW hero region.')
    selected=[]
    for hero in heroes:
        title=_one(hero.find('p','sidebar__hero-text')).text()
        if not title.startswith('Australian Print Workshop '):
            raise SourceError('Unrecognized APW hero identity.')
        openings=[node.text() for node in hero.find('a','sidebar__hero-action')
                  if node.text().startswith('Opening ')]
        runs=hero.find(css='sidebar__hero-dates')
        if openings or runs:
            if len(openings)>1 or len(runs)>1:
                raise SourceError('Ambiguous APW event panel.')
            selected.append((title,openings[0] if openings else '',runs[0].text() if runs else ''))
    if not selected:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    if len(selected)!=1:
        raise SourceError('Multiple APW event panels need explicit coverage.')
    title,opening,run=selected[0]
    minimal=('<div class="sidebar__hero"><p class="sidebar__hero-text">'+escape(title)+'</p>'+
        ('<a class="sidebar__hero-action">'+escape(opening)+'</a>' if opening else '')+
        ('<p class="sidebar__hero-dates">'+escape(run)+'</p>' if run else '')+'</div>')
    return parse_apw(minimal.encode(),retrieved_at=retrieved_at)


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now=timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded APW event page.')
    source=_event_panel(raw,retrieved_at)
    evidence=EventEvidence('apw',None,APW_URL,retrieved_at,provenance,sha256(raw).hexdigest(),1)
    common=dict(key='fixture:event:pending',title=source.title,kind='event',
        source='Australian Print Workshop',provenance=provenance,rank=1,
        retrieved_at=retrieved_at,url=None if provenance=='fixture' else APW_URL,
        source_id='apw',official_url=APW_URL,venue=source.venue,category='gallery',evidence=(evidence,))
    events=[]
    start=timestamp(source.starts_at)
    if start and now<=start<=now+timedelta(weeks=8):
        events.append(occurrence_event(ListingEvent(**common,source_start=source.starts_at,
            starts_at=source.starts_at,event_timezone='Australia/Melbourne',date_precision='time',
            listed_date=source.opening_wording,
            source_notice='Source opening session; admission and availability unverified.')))
    if source.exhibition_starts_on and source.exhibition_ends_on:
        run=APWRun(**common,source_start=source.exhibition_starts_on,
            source_end=source.exhibition_ends_on,date_precision='date',end_precision='date',
            exhibition_starts_on=source.exhibition_starts_on,
            exhibition_ends_on=source.exhibition_ends_on,listed_date=source.run_wording)
        if _eligible(run,now):events.append(occurrence_event(run))
    if not events:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(events)
