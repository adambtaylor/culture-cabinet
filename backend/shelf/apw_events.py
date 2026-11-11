"""Event-only APW delegation; opening sessions and exhibition runs are separate."""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

from shelf.discovery_sources import SourceError, timestamp
from shelf.event_sources import ListingEvent, EventEvidence, occurrence_event
from shelf.event_listing_transport import ListingIntervalPending
from shelf.gallery_sources import APW_URL, MAX_BYTES, parse_apw, _eligible


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


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now=timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded APW event page.')
    source=parse_apw(raw,retrieved_at=retrieved_at)
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
