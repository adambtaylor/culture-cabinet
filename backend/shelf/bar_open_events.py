"""Recognize Bar Open's official recurrence shell without inventing a gig."""
from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://baropen.com.au/make-it-up-club/'


class _Programme(_Listing):
    def captures_root(self, tag, attributes):
        return tag == 'h1' or tag == 'div' and 'entry-content' in attributes.get('class','').split()


def parse_listing(raw, *, retrieved_at):
    if type(raw) is not bytes or len(raw) > MAX_BYTES or timestamp(retrieved_at) is None:
        raise SourceError('Invalid bounded Bar Open programme observation.')
    parser = _Programme(); parser.feed(raw.decode('utf-8')); parser.close()
    if parser.stack:
        raise SourceError('Truncated Bar Open page.')
    heading = _one([node for node in parser.columns if node.tag == 'h1'])
    content = _one([node for node in parser.columns if node.matches('div','entry-content')])
    if (label(heading.text()) != 'Make It Up Club'
            or 'Upcoming Events' not in [node.text() for node in content.find('h3')]
            or not any(node.text().startswith('Held every Tuesday night at Bar Open') for node in content.find('p'))):
        raise SourceError('Unrecognized Bar Open programme layout.')
    # The observed programme has social links and a weekly description, not
    # dated event rows. Neither copyright years nor Tuesdays establish a next
    # occurrence. No external/social endpoint is followed to fill that gap.
    from shelf.event_scraping import ProgrammeUnavailable
    raise ProgrammeUnavailable
