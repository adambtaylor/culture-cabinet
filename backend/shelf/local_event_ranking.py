"""Local ranking/display of already permitted source facts, after inference."""
from datetime import date, datetime
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .local_event_preferences import LocalEventContext, postal_index
from .postal_locality import distance_km

LOCAL_CATEGORIES=frozenset(('event','gallery','art_fair','community'))


def source_facts(card):
    """Caller supplies current retained source facts, never a model hypothesis."""
    values={};references=[];ambiguous=set()
    for fact in card.get('facts',[]):
        name=fact['property'];value=fact['value']
        if name in values and values[name]!=value:ambiguous.add(name)
        else:values[name]=value
        references.append(fact['id'])
    for name in ambiguous:values.pop(name,None)
    return values,tuple(references)


def local_distance(context,card,*,index=None):
    if (context is None or card['category'] not in LOCAL_CATEGORIES or context.reference is None):
        return None,'unknown',()
    if type(context) is not LocalEventContext:raise ValueError('Local selection context required')
    values,references=source_facts(card)
    index=postal_index() if index is None else index
    venue=index.venue_reference(values,evidence_refs=references)
    if venue.reference is None:return None,'unknown',references
    return distance_km(context.reference,venue.reference),venue.basis,references


def rank_key(context,card,*,index=None):
    # No setting means the accepted baseline order. A radius is a soft local
    # preference and can never drop a distant or unlocated eligible candidate.
    if context is None or context.reference is None or card['category'] not in LOCAL_CATEGORIES:return 0,0
    distance,_,_=local_distance(context,card,index=index)
    if distance is None:return 1,0
    return (0 if distance<=context.radius_km else 2),distance


def _opening(values,now):
    status='unknown'
    timezone=values.get('gallery.timezone')
    try:
        if type(timezone) is not str or len(timezone)>80:return status
        zone=ZoneInfo(timezone)
        today=now.astimezone(zone).date()
        start=values.get('gallery.exhibition_starts_on');end=values.get('gallery.exhibition_ends_on')
        start=date.fromisoformat(start) if type(start) is str and len(start)==10 else None
        end=date.fromisoformat(end) if type(end) is str and len(end)==10 else None
        if start and end and end<start:return status
        if end and today>end:return 'past_exhibition'
        opening=values.get('gallery.opening_starts_at')
        opening=datetime.fromisoformat(opening.replace('Z','+00:00')) if type(opening) is str else None
        if opening is not None and opening.utcoffset() is None:opening=None
        if opening is not None and opening>now:return 'upcoming_opening'
        if start is not None and start<=today and end is not None and today<=end:return 'current_exhibition'
        if opening is not None and opening<=now:return 'past_opening'
    except (ValueError,TypeError,ZoneInfoNotFoundError):
        return 'unknown'
    return status


def display(context,card,*,publication_ref,publication_revision,now,permitted_visual_names=(),index=None):
    """Publication/revision-bound owner overlay, never a stored/model fact."""
    if type(context) is not LocalEventContext:raise ValueError('Local selection context required')
    distance,basis,references=local_distance(context,card,index=index)
    label=('Add a postcode to estimate distance.' if context.reference is None else 'Distance unknown.')
    band='unknown'
    if distance is not None:
        label=('Less than 1 km from postcode reference' if distance<1 else
               'Approximately '+str(round(distance))+' km from postcode reference')
        band='in_radius' if distance<=context.radius_km else 'outside_radius'
    values,_=source_facts(card)
    normalize=lambda value:' '.join(unicodedata.normalize('NFC',value).split()).casefold()
    names={normalize(value) for value in permitted_visual_names}
    matched=any(type(value) is str and normalize(value) in names for key,value in values.items()
                if re.fullmatch(r'gallery\.artist_name\.[0-9]',key))
    return dict(schema='local-event-display/v1',candidate_key=card['key'],profile_ref=context.profile_ref,
        publication_ref=publication_ref,publication_revision=publication_revision,
        dataset_revision=context.dataset_revision,location_revision=context.location_revision,
        selection_context_digest=context.digest,distance_label=label,distance_basis=basis,radius_band=band,
        source_refs=list(references),opening_status=_opening(values,now),
        opening_starts_at=values.get('gallery.opening_starts_at'),opening_ends_at=values.get('gallery.opening_ends_at'),
        exhibition_starts_on=values.get('gallery.exhibition_starts_on'),
        exhibition_ends_on=values.get('gallery.exhibition_ends_on'),date_precision=values.get('gallery.date_precision'),
        timezone=values.get('gallery.timezone'),artist_match_status='name_match_unconfirmed' if matched else 'none',
        artist_match_label='Artist name matches your stated favourite; identity unconfirmed.' if matched else '')
