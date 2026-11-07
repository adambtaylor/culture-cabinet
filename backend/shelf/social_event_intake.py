"""Pure bounded public-event evidence stored inside SocialDiscoveryState.data.

This module performs no account reads or actions. Capture identity is recorded
separately from the supplied action origin. Withdrawal removes event metadata;
minimal hashed tombstones retain exclusions and prevent stale resurrection.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_OBSERVATIONS = 100
MAX_EVENTS = 100
MAX_AUDIT = 200
MAX_TAINTED_LINEAGES = 100
KNOWN_AGENT_EVENTS = frozenset(('26754161534246333', '1092959976817399'))
BOUNDS = {'platform_event_id': 100, 'canonical_url': 512, 'title': 160,
          'organizer': 160, 'venue': 160, 'raw_date_time': 200,
          'timezone_name': 64, 'observed_at': 64, 'evidence_ref': 240,
          'lineage_ref': 240, 'supersedes': 64, 'conflict_note': 240}
OPTIONAL = frozenset(('organizer', 'venue', 'supersedes', 'conflict_note'))
ENUMS = {'platform': ('facebook', 'instagram'),
         'actor_origin': ('owner_confirmed', 'agent_marked', 'unknown'),
         'signal_type': ('interested', 'attending', 'none'),
         'confidence': ('observed', 'reported', 'uncertain')}
METADATA = ('title', 'organizer', 'venue', 'raw_date_time', 'timezone_name', 'signal_type', 'actor_origin')


class EventIntakeError(ValueError):
    """A bounded intake action cannot be accepted; no state was changed."""


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _text(value, maximum, *, optional=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise EventIntakeError('Invalid or oversized public-event text.')
    result = value.strip()
    if not result and not optional:
        raise EventIntakeError('Required public-event evidence is missing.')
    return result


def _timestamp(value):
    value = _text(value, 64)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise EventIntakeError('Use an ISO timestamp with an explicit timezone.') from None


def event_identity(platform, platform_event_id):
    """Exact platform identity only; titles, venues and times never imply a match."""
    return 'social:event:' + _hash([platform, platform_event_id])


def _url(platform, value, identity):
    value = _text(value, 512)
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.netloc not in (platform + '.com', 'www.' + platform + '.com')
                or parts.query or parts.fragment or '%' in value or '\\' in value
                or any(ord(c) < 33 for c in value)):
            raise ValueError
        pattern = r'/events/([1-9][0-9]{0,24})/?' if platform == 'facebook' else r'/p/([A-Za-z0-9_-]{1,100})/?'
        match = re.fullmatch(pattern, parts.path)
        if not match or match[1] != identity:
            raise ValueError
    except ValueError:
        raise EventIntakeError('Use a clean public event/post HTTPS permalink matching its platform ID.') from None
    return 'https://www.' + platform + '.com' + parts.path.rstrip('/')


def normalize_observation(fields):
    """Validate the allowlisted envelope; never accept private payload extensions."""
    allowed = set(BOUNDS) | set(ENUMS) | {'action', 'public_confirmed', 'mode', 'revision', 'nonce'}
    if set(fields) - allowed:
        raise EventIntakeError('Unsupported public-event evidence field.')
    if fields.get('public_confirmed') is not True:
        raise EventIntakeError('Only explicitly confirmed public cultural events are accepted.')
    row = {name: _text(fields.get(name, ''), limit, optional=name in OPTIONAL)
           for name, limit in BOUNDS.items()}
    for name, choices in ENUMS.items():
        if fields.get(name) not in choices:
            raise EventIntakeError('Invalid public-event ' + name + '.')
        row[name] = fields[name]
    row['canonical_url'] = _url(row['platform'], row['canonical_url'], row['platform_event_id'])
    row['observed_at'] = _timestamp(row['observed_at'])
    if row['timezone_name'] != 'unknown':
        try:
            ZoneInfo(row['timezone_name'])
        except (ZoneInfoNotFoundError, ValueError):
            raise EventIntakeError('Use an observed IANA timezone or unknown.') from None
    if row['supersedes'] and not re.fullmatch(r'[a-f0-9]{64}', row['supersedes']):
        raise EventIntakeError('Invalid superseded observation identity.')
    row['public_confirmed'] = True
    row['id'] = _hash(row)
    row['event_key'] = event_identity(row['platform'], row['platform_event_id'])
    return row


def _state(data):
    return deepcopy(data.get('event_intake', {'version': 1, 'observations': [],
        'events': {}, 'tainted_lineages': [], 'unknown_origins': {}, 'audit': []}))


def apply_event_action(data, fields, *, captured_at, capture_actor):
    """Return complete JSON data after an explicit observe_event/event_withdraw.

    The coordinator supplies the server clock and authenticated capture actor and
    owns authorization/CAS. A 100-event lifetime limit keeps deletion tombstones
    bounded without ever pruning agent exclusions to make room for new events.
    """
    captured_at = _timestamp(captured_at)
    capture_actor = _text(capture_actor, 100)
    state = _state(data)
    action = fields.get('action')
    if action == 'observe_event':
        row = normalize_observation(fields)
        key = row['event_key']
        if datetime.fromisoformat(row['observed_at']) > datetime.fromisoformat(captured_at):
            raise EventIntakeError('Observation time cannot be later than the server capture time.')
        previous = next((item for item in state['observations'] if item['id'] == row['id']), None)
        if previous:
            return deepcopy(data)
        event = state['events'].get(key)
        if event and event['withdrawn']:
            raise EventIntakeError('This event was withdrawn. Stale captures cannot restore it.')
        if len(state['observations']) >= MAX_OBSERVATIONS:
            raise EventIntakeError('The 100-observation limit is reached. Withdraw an event first.')
        if event is None:
            if len(state['events']) >= MAX_EVENTS:
                raise EventIntakeError('The 100-event lifetime limit is reached; exclusions cannot be pruned.')
            event = state['events'][key] = {'tainted': False, 'withdrawn': False}
        if row['supersedes']:
            prior = next((item for item in state['observations'] if item['id'] == row['supersedes']), None)
            if prior is None or prior['event_key'] != key:
                raise EventIntakeError('A correction must reference an existing observation of this exact event.')
        lineage = _hash(row['lineage_ref'])
        if row['actor_origin'] == 'unknown':
            if len(state['unknown_origins']) >= MAX_OBSERVATIONS:
                raise EventIntakeError('The unknown-origin evidence limit is reached; origins cannot be pruned.')
            state['unknown_origins'][row['id']] = {'event_key': key, 'lineage_hash': lineage,
                'evidence_hash': _hash(row['evidence_ref']), 'resolved': False}
        if (row['actor_origin'] == 'owner_confirmed' and row['confidence'] == 'observed'
                and row['supersedes'] in state['unknown_origins']):
            origin = state['unknown_origins'][row['supersedes']]
            if origin['evidence_hash'] != _hash(row['evidence_ref']):
                origin['resolved'] = True
        agent = row['actor_origin'] == 'agent_marked' or (
            row['platform'] == 'facebook' and row['platform_event_id'] in KNOWN_AGENT_EVENTS)
        if agent and lineage not in state['tainted_lineages']:
            if len(state['tainted_lineages']) >= MAX_TAINTED_LINEAGES:
                raise EventIntakeError('The lineage exclusion limit is reached; exclusions cannot be pruned.')
            state['tainted_lineages'].append(lineage)
        event['tainted'] |= agent or lineage in state['tainted_lineages']
        row.update(captured_at=captured_at, capture_actor=capture_actor)
        state['observations'].append(row)
        identity = row['id']
    elif action == 'event_withdraw':
        if set(fields) - {'action', 'identity', 'mode', 'revision', 'nonce'}:
            raise EventIntakeError('Unsupported event withdrawal field.')
        identity = _text(fields.get('identity', ''), 64)
        if not re.fullmatch(r'[a-f0-9]{64}', identity):
            raise EventIntakeError('Invalid observation identity.')
        row = next((item for item in state['observations'] if item['id'] == identity), None)
        if row is None:
            raise EventIntakeError('This observation is no longer available.')
        key = row['event_key']
        state['events'][key]['withdrawn'] = True
        # Preserve taint learned through a shared action lineage before deleting it.
        if any(_hash(item['lineage_ref']) in state['tainted_lineages']
               for item in state['observations'] if item['event_key'] == key):
            state['events'][key]['tainted'] = True
        state['observations'] = [item for item in state['observations'] if item['event_key'] != key]
    else:
        raise EventIntakeError('Invalid public-event intake action.')
    state['audit'] = (state['audit'] + [{'action': action, 'id': identity,
        'event_key': key, 'captured_at': captured_at, 'capture_actor': capture_actor}])[-MAX_AUDIT:]
    result = deepcopy(data)
    result['event_intake'] = state
    return result


def event_intake_view(data):
    """Return detached active evidence/history with explicit conflicts/exclusions."""
    state = _state(data)
    rows = state['observations']
    superseded = {row['supersedes'] for row in rows if row['supersedes']}
    # Hashed unknown-origin receipts survive withdrawal; only a direct explicit
    # correction can resolve them. Deletion must not upgrade linked owner copies.
    unresolved_unknown = [origin for origin in state['unknown_origins'].values()
                          if not origin['resolved']]
    result = []
    for row in rows:
        event = state['events'][row['event_key']]
        if event['withdrawn']:
            continue
        tainted = event['tainted'] or any(
            other['event_key'] == row['event_key'] and _hash(other['lineage_ref']) in state['tainted_lineages']
            for other in rows)
        unknown_origin = any(other['event_key'] == row['event_key']
            or other['lineage_hash'] == _hash(row['lineage_ref']) for other in unresolved_unknown)
        reason = ('Agent action lineage excluded' if tainted else
                  'Superseded evidence' if row['id'] in superseded else
                  'Owner action origin unconfirmed' if row['actor_origin'] != 'owner_confirmed' else
                  'Unknown action origin requires explicit evidence correction' if unknown_origin else
                  'Observation confidence unresolved' if row['confidence'] != 'observed' else
                  'No Interested or Attending signal' if row['signal_type'] == 'none' else '')
        conflicts = [field for field in METADATA if len({other[field] for other in rows
                     if other['event_key'] == row['event_key']}) > 1]
        result.append(dict(row, eligible=not reason, exclusion_reason=reason,
                           conflicts=conflicts, superseded=row['id'] in superseded))
    return result


def event_projection(data):
    return event_intake_view(data)


def event_signals(data):
    """One owner intent per exact event; Attending is stronger than Interested.

    These signals assert neither enjoyment, completed attendance, booking nor
    payment. Followed pages and unknown/agent actions never become taste.
    Contradictory Interested/Attending claims retain both rows and choose stronger
    intent; a later none claim revokes old intent only through explicit supersedes.
    """
    groups = {}
    for row in event_intake_view(data):
        if row['eligible']:
            groups.setdefault(row['event_key'], []).append(row)
    result = []
    for key, rows in sorted(groups.items()):
        row = max(rows, key=lambda item: (item['signal_type'] == 'attending', item['observed_at'], item['id']))
        result.append({'key': key, 'title': row['title'], 'medium': 'event',
            'source': 'Public social event observation', 'basis': 'social_intent', 'value': row['signal_type']})
    return result


def event_revision(data):
    """Evidence revision separate from the minimal synthesis signal schema."""
    return _hash(data.get('event_intake', {}))


def event_candidates(data, existing=()):
    """Public discovery rows, retaining action provenance without taste claims.

    Cross-connector matches require an actual URL or explicit platform ID. A
    similar title/date never merges two events. Existing may contain dicts or
    candidate objects from connector projections.
    """
    def field(item, name):
        return item.get(name, '') if isinstance(item, dict) else getattr(item, name, '')
    def comparable_url(value):
        if not isinstance(value, str):
            return ''
        value = value.rstrip('/')
        for platform in ENUMS['platform']:
            value = value.replace('https://' + platform + '.com/', 'https://www.' + platform + '.com/')
        return value
    known_urls, known_ids = set(), set()
    for item in existing:
        evidence = field(item, 'social_evidence') or ()
        for record in (item, *evidence):
            for name in ('url', 'canonical_url', 'event_url', 'post_url'):
                url = comparable_url(field(record, name))
                if url:
                    known_urls.add(url)
            platform, identity = field(record, 'platform'), field(record, 'platform_event_id')
            if platform and identity:
                known_ids.add((platform, str(identity)))
    groups = {}
    for row in event_intake_view(data):
        if (comparable_url(row['canonical_url']) in known_urls
                or (row['platform'], row['platform_event_id']) in known_ids):
            continue
        groups.setdefault(row['event_key'], []).append(row)
    result = []
    for key, rows in sorted(groups.items()):
        current = [row for row in rows if not row['superseded']]
        row = max(current, key=lambda item: (item['observed_at'], item['id']))
        result.append({'key': key, 'title': row['title'], 'kind': 'event',
            'url': row['canonical_url'], 'platform': row['platform'],
            'platform_event_id': row['platform_event_id'], 'source': 'Public social event observation',
            'provenance': 'browser-observed/manual-handoff', 'retrieved_at': row['captured_at'],
            'social_evidence': rows, 'raw_date_time': row['raw_date_time'],
            'timezone_name': row['timezone_name'], 'venue': row['venue']})
    return result
