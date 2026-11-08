"""Server-owned coverage inventory; navigation never chooses Enhance scope."""
from dataclasses import dataclass

from .disclosure import canonical_bytes
from .enhance_contract import RegistryView, digest, require


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    group: str


# Subsurfaces remain explicit even when there is no compatible configured API.
# In particular a recording is not an artist, and dining is not a booked outing.
CATEGORIES = (
    Category('book', 'Books', 'books'),
    Category('movie', 'Films', 'films'),
    Category('tv', 'TV series', 'tv'),
    Category('artist', 'Music artists', 'music'),
    Category('recording', 'Recordings and radio', 'music'),
    Category('podcast_episode', 'Nonfiction episodes', 'music'),
    Category('event', 'Events', 'events'),
    Category('gallery', 'Gallery events', 'events'),
    Category('art_fair', 'Art fairs', 'events'),
    Category('community', 'Community events', 'events'),
    Category('regional_day_trip', 'Regional events', 'outings'),
    Category('outing', 'Outings', 'outings'),
    Category('dining', 'Dining', 'outings'),
)
KEYS = tuple(row.key for row in CATEGORIES)
ALIASES = {'books': 'book', 'graphic_novel': 'book', 'graphic_novels': 'book',
           'films': 'movie', 'series': 'tv', 'music': 'artist', 'events': 'event',
           'outings': 'outing', 'podcast': 'podcast_episode'}


def canonical_category(value):
    require(type(value) is str)
    key = ALIASES.get(value, value)
    require(key in KEYS)
    return key


def registry_view(enabled=None):
    """Capture complete enabled coverage from nonsecret backend configuration.

    Only explicit configuration can disable a surface. Keeping unsupported rows
    avoids mistaking a missing API or a hidden sidebar tab for an empty catalogue.
    """
    require(enabled is None or type(enabled) is tuple)
    selected = KEYS if enabled is None else tuple(canonical_category(k) for k in enabled)
    require(selected and len(selected) == len(set(selected)))
    keys = tuple(key for key in KEYS if key in selected)
    material = {'schema': 'category-registry/v1',
                'categories': [vars(row) for row in CATEGORIES if row.key in keys],
                'aliases': ALIASES}
    return RegistryView(digest(canonical_bytes(material)), keys)


def fixed_moshtix_configuration(registry):
    """Explicit integration choice, never a claim of authorization or readiness.

    Keep the complete registry for settlement. An event API cannot quietly stand
    in for music, galleries, outings or any other unsupported surface.
    """
    require(type(registry) is RegistryView and registry == registry_view(registry.categories))
    return (('event', ('moshtix',)),) if 'event' in registry.categories else ()


def registered_source_coverage():
    """Nonsecret registration projection; never configuration or rights proof.

    Empty rows remain explicit. A parser/SourceSpec alone does not establish
    a native backend, owner consent, credentials or permitted live collection.
    Callers must project those current runtime facts independently.
    """
    from .source_adapters import SOURCES
    return tuple({'category': category.key, 'sources': tuple(
        {'source': source.key, 'operations': source.operations, 'kind': source.kind}
        for source in SOURCES if category.key in source.categories)}
        for category in CATEGORIES)
