"""One explicit event read through existing fixed transports and pure parsers.

This driver accepts source IDs, never destinations or source-provided callbacks.
Site parsers retain their own identity, date and coverage semantics.
"""
from datetime import datetime

from shelf.discovery_sources import Outcome, SourceError
from shelf.discovery_transport import PBSIntervalPending
from shelf.event_listing_transport import ListingIntervalPending, ListingPolicyUnavailable

from shelf.event_source_registry import SOURCES, get_source, source_route

IMPLEMENTED_SOURCES = frozenset(key for key, source in SOURCES.items() if source.implemented)


def collect(source_id, *, now, event_route=''):
    """One fixed acquisition or a truthful unavailable outcome; no retries.

    ``now`` is the caller's aware observation clock, never a source show date.
    The closed descriptor owns format/coverage policy; clients cannot replace it.
    """
    try:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise SourceError('An aware observation clock is required.')
        source = get_source(source_id)
        route = source_route(source_id, event_route)
        if not source.implemented:
            status = 'permission-required' if source.access_state == 'permission-required' else 'unavailable'
            return Outcome(status, message=source.unavailable_message)
        route_options = {'event_route': route} if route else {}
        status, raw = source.acquire(now, **route_options)
        if status == 429:
            return Outcome('rate-limited', message='Source rate limited; no retry attempted.')
        if status in (401, 403):
            return Outcome('error', message='Source refused public access; no retry attempted.')
        if type(status) is not int or status != 200:
            return Outcome('error', message='Source request failed; no retry attempted.')

        # Do not apply upcoming_events here. RRR omits years; PBS alone owns
        # its visible-year/window filtering. Both can retain unknown-date leads.
        if type(raw) is not bytes or len(raw) > source.max_bytes:
            raise SourceError('Bounded source response required.')
        cards = source.parse(raw, now=now, retrieved_at=now.isoformat(), **route_options)
        if not cards:
            return Outcome('empty', cards,
                'No eligible leads in this bounded page; this does not establish no events or tickets.')
        return Outcome('ready', tuple(cards), source.coverage)
    except ListingPolicyUnavailable:
        return Outcome('permission-required', message='Source access policy is unverified; no listing request sent.')
    except ListingIntervalPending:
        return Outcome('rate-limited', message='Source requires a shared origin interval; no request sent.')
    except PBSIntervalPending:
        return Outcome('rate-limited', message='PBS requires ten seconds between shared requests; try again later.')
    except TimeoutError:
        return Outcome('timeout', message='Source timed out; no retry attempted.')
    except (SourceError, KeyError, TypeError, ValueError, RecursionError, AttributeError, OSError):
        # Publisher bodies, parser exception text and transport diagnostics are
        # untrusted and must not reach the caller or become fallback inventory.
        return Outcome('invalid', message='Source response or transport invalid; no candidates accepted.')
