"""NGV's two closed event-index routes; no artwork or gallery acquisition."""
from types import MappingProxyType
from shelf.discovery_sources import SourceError

ROUTES = MappingProxyType({
    'programs': 'https://www.ngv.vic.gov.au/whats-on/programs-events/',
    'exhibitions': 'https://www.ngv.vic.gov.au/whats-on/exhibitions/',
})


def selected_route(value=''):
    if type(value) is not str or value not in ('', *ROUTES):
        raise SourceError('Unknown fixed NGV event route.')
    return value or 'programs'
