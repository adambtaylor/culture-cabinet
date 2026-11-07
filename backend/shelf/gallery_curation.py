"""Owner-only editorial hypotheses from the authorized brief; no taste writes.

Source fields describe listings. The text here proposes looking experiments,
never ratings, favourites, image analysis or claims that an artist is liked.
"""
from urllib.parse import urlsplit
from types import SimpleNamespace

BRIEF_LIBRARY_ID = 'libfile_4cca1f8741448191b3ae93ae64ddfcae'
BRIEF_SHA256 = '08c39a725e7612da7725adfaab6357d09b499aa185506b282da469fe4214364a'
PRINCIPLE_QUOTES = (
    'I want to fall into images and explore them.',
    'each time I look at it, I would have a different experience, and each time somebody else looks at it, they have a different experience. And then together we can talk about it and have a different experience, shared experience again.',
    "It's almost like something that you can keep revisiting and have a different emotional experience each time ... It's interpretational.",
)
LIKED_ARTISTS = frozenset(name.casefold() for name in (
    'Kandinsky', 'Wassily Kandinsky', 'Jean-Michel Basquiat', 'Dalí', 'Salvador Dalí',
    'Van Gogh', 'Vincent van Gogh', 'Klimt', 'Gustav Klimt', 'Cézanne', 'Paul Cézanne',
    'J. M. W. Turner', 'Gregory Crewdson', 'Jules de Balincourt', 'Damian Elwes',
))
EXPLORATIONS = {
    'giorgio de chirico': 'test whether architectural order becomes compelling when it feels uncertain',
    'etel adnan': 'test whether compact landscape and colour invite immersion without elaborate narrative',
    'peter doig': 'ask whether ambiguity resides in the place, the paint or an imagined story',
    'anne wallace': 'test cinematic expectation against a narrative that does not resolve',
    'toyin ojih odutola': 'explore figures, material marks and narrative possibilities beyond unpeopled space',
    'julie mehretu': 'test movement through layered abstraction without a stable depicted world',
    'mia boe': 'explore landscape and imagined space while encountering its historical context on its own terms',
    'jess johnson': 'test whether intricate imagined space invites another look or feels overdetermined',
    'brent harris': 'explore symbolic openness and threshold-like spaces',
}
VENUE_EXPERIMENTS = {
    'gertrude.org.au': 'Explore a studio or edition practice beyond the familiar names; ask which work invites a second encounter.',
    'www.australianprintworkshop.com': 'Compare the material and scale of printmaking with a screen encounter; ask what changes in person.',
    'blindside.org.au': 'Test the brief’s hypothesis about thresholds and interrupted narratives against the actual exhibition.',
    'www.seventhgallery.org': 'Try looking first, then a conversation; ask whether another reading changes the encounter.',
    'ccp.org.au': 'Explore photography beyond the known cinematic references; ask what makes an image worth revisiting.',
    'westspace.org.au': 'Explore an unfamiliar contemporary practice; ask whether it opens a new route into sustained looking.',
    'busprojects.org.au': 'Explore experimental practice beyond the familiar image references; allow the encounter to challenge the brief.',
    'melbourneartfair.com.au': 'Compare contrasting practices at actual scale; a broad sampling encounter need not become a purchase.',
    'affordableartfair.com': 'Explore contrasting works before considering an exact offer; the fair name says nothing about the all-in ceiling.',
}


def _owner(actor_id, profile_id):
    # bool is an int in Python, but is never a profile or actor identity.
    return type(actor_id) is int and actor_id == 1 and type(profile_id) is int and profile_id == 1


def principle_quotes(*, actor_id, profile_id):
    return PRINCIPLE_QUOTES if _owner(actor_id, profile_id) else ()


def _artists(card):
    data = getattr(card, 'gallery_data', {})
    names = data.get('artists', ()) if isinstance(data, dict) else ()
    return tuple(name for name in names if isinstance(name, str)) if isinstance(names, (list, tuple)) else ()


def discovery_mode(card):
    """An editorial rotation bucket, never a preference score or source fact."""
    artists = {name.casefold() for name in _artists(card)}
    if artists & LIKED_ARTISTS:
        return 'affinity'
    if artists & EXPLORATIONS.keys():
        return 'bridge'
    return 'unfamiliar'


def reasons(card, *, actor_id, profile_id):
    """Source-attributed artist names are distinct from brief-based hypotheses.

    The caller supplies an authenticated owner actor and the owner profile.
    Upstream authorization remains mandatory; this helper performs no lookup.
    """
    if not _owner(actor_id, profile_id) or getattr(card, 'category', None) not in ('gallery', 'art_fair'):
        return ()
    result = []
    for artist in _artists(card):
        name = artist.casefold()
        if name in LIKED_ARTISTS:
            result.append(f'Qualitative seed: {artist} is a stated artist-level like; this specific work remains an encounter to test, without a rating or favourite.')
        elif name in EXPLORATIONS:
            result.append(f'Curatorial hypothesis — {artist}: {EXPLORATIONS[name]}; an exploratory artist, not an established like.')
    if not result:
        url = getattr(card, 'source_url', None) or getattr(card, 'url', None) or ''
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        experiment = VENUE_EXPERIMENTS.get(host)
        if experiment:
            result.append(f'Curatorial hypothesis — {experiment}')
        else:
            result.append('Unfamiliar discovery experiment: look before reading the context, then ask what invites another encounter; no preference is established.')
    result.append('Looking question: quiet space, expressive or symbolic depth, and awe or storms can each invite immersion; visual busyness is not a score. Revisit or discuss the encounter without assuming a shared preference.')
    return tuple(result)


def select(cards, *, actor_id, profile_id, limit=6):
    """Rotate affinity, bridge and unfamiliar leads without inventing relevance.

    Preserve order within each bucket. No implicit revisit history is generated.
    Cards are returned intact, keeping source observations separate from reasons.
    """
    if not _owner(actor_id, profile_id) or type(limit) is not int or limit < 1:
        return ()
    buckets = {name: [] for name in ('affinity', 'bridge', 'unfamiliar')}
    seen = set()
    for card in cards:
        key = getattr(card, 'key', None)
        if key in seen or getattr(card, 'category', None) not in ('gallery', 'art_fair'):
            continue
        seen.add(key)
        buckets[discovery_mode(card)].append(card)
    result = []
    while any(buckets.values()) and len(result) < limit:
        for bucket in buckets.values():
            if bucket and len(result) < limit:
                result.append(bucket.pop(0))
    return tuple(result)


def offer_reasons(offer, *, actor_id, profile_id):
    """An editorial offer projection is not an event or an ownership record."""
    if not isinstance(offer, dict):
        return ()
    view = SimpleNamespace(category='art_fair',
        source_url=offer.get('source_url') or offer.get('offer_url'),
        gallery_data={'artists': (offer.get('artist'),)})
    return reasons(view, actor_id=actor_id, profile_id=profile_id)
