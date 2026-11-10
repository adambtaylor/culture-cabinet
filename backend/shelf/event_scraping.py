"""One explicit event read through existing fixed transports and pure parsers.

This driver accepts source IDs, never destinations or source-provided callbacks.
Site parsers retain their own identity, date and coverage semantics.
"""
from datetime import datetime

from shelf.discovery_sources import Outcome, SourceError
from shelf.discovery_transport import PBSIntervalPending

IMPLEMENTED_SOURCES = frozenset({'rrr', 'pbs'})


def collect(source_id, *, now):
    """Return attributed leads or a safe failure; never retry a failed read.

    ``now`` is the caller's aware observation clock, sampled for this action.
    It describes retrieval, not the publisher's update time or a show date.
    """
    try:
        if (type(source_id) is not str or source_id not in IMPLEMENTED_SOURCES
                or not isinstance(now, datetime) or now.utcoffset() is None):
            raise SourceError('Unsupported source or observation clock.')

        # Resolve existing fixed seams at call time: tests can replace transport
        # without adding an arbitrary URL/credential interface to production.
        from shelf import discovery_transport
        if source_id == 'rrr':
            from shelf.rrr_events import parse_listing
            read = discovery_transport.rrr
            message = ('Partial coverage: up to nine cards from one Triple R listing page. '
                       'Source dates omit the year; upcoming status and ticket availability unverified.')
        else:
            from shelf.pbs_events import parse_listing
            # pbs() owns the shared host reservation. Bypassing it here would
            # permit another source action/process to violate the crawl delay.
            read = discovery_transport.pbs
            message = ('Partial PBS coverage: up to nine upcoming-section leads within eight weeks; '
                       'unknown dates are labelled. Exact sessions and ticket availability unverified.')

        status, raw = read()
        if status == 429:
            return Outcome('rate-limited', message='Source rate limited; no retry attempted.')
        if status in (401, 403):
            return Outcome('error', message='Source refused public access; no retry attempted.')
        if type(status) is not int or status != 200:
            return Outcome('error', message='Source request failed; no retry attempted.')

        # Do not apply upcoming_events here. RRR omits years; PBS alone owns
        # its visible-year/window filtering. Both can retain unknown-date leads.
        cards = parse_listing(raw, retrieved_at=now.isoformat())
        if source_id == 'pbs' and not cards:
            return Outcome('empty', cards,
                'No eligible dated PBS leads in this bounded page; this does not establish no events or tickets.')
        return Outcome('ready', cards, message)
    except PBSIntervalPending:
        return Outcome('rate-limited', message='PBS requires ten seconds between shared requests; try again later.')
    except TimeoutError:
        return Outcome('timeout', message='Source timed out; no retry attempted.')
    except (SourceError, KeyError, TypeError, ValueError, RecursionError, AttributeError, OSError):
        # Publisher bodies, parser exception text and transport diagnostics are
        # untrusted and must not reach the caller or become fallback inventory.
        return Outcome('invalid', message='Source response or transport invalid; no candidates accepted.')
