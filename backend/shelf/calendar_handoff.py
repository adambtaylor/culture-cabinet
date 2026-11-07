"""Pure calendar commands and non-inviting ICS; persistence/authority belong to the caller.

Never accept these records as browser authority. The authenticated integration must
resolve targets, recheck grants, and atomically save receipts before any external I/O.
"""
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shelf.acquisition_contract import Blocked, ContractError, _text


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:@-]{1,160}', value):
        raise ContractError('Invalid identity')
    return value


def https_url(value, *, private=False):
    """Public source links have no query/fragment; no redirects are followed here."""
    _text(value, 2048)
    decoded = value
    for _ in range(4):
        if (not decoded.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in decoded)
                or '\\' in decoded):
            raise ContractError('Unsafe HTTPS URL')
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise ContractError('Excessively encoded URL')
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'https' and parts.hostname and parts.username is None
                 and parts.password is None and parts.port in (None, 443)
                 and re.fullmatch(r'[A-Za-z0-9.-]+', parts.hostname)
                 and not parts.fragment and (private or not parts.query))
    except ValueError:
        valid = False
    if not valid:
        raise ContractError('Safe HTTPS link required')
    return value


def instant(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ContractError('Offset-aware timestamp required') from None
    if result.utcoffset() is None or result.microsecond or result.utcoffset().microseconds:
        raise ContractError('Offset-aware whole-second timestamp required')
    return result


def zone(value):
    try:
        return ZoneInfo(value)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ContractError('Known IANA timezone required') from None


def local_time(value, timezone_name, *, fold=None):
    """Resolve a wall clock explicitly; reject DST gaps and unresolved overlaps."""
    if (type(value) is not str
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?', value)):
        raise ContractError('Explicit local date and time required')
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ContractError('Invalid local time') from None
    if naive.tzinfo is not None or naive.microsecond or (fold is not None and
            (type(fold) is not int or fold not in (0, 1))):
        raise ContractError('Naive whole-second local time and explicit fold required')
    tz = zone(timezone_name)
    choices = [naive.replace(tzinfo=tz, fold=f) for f in (0, 1)]
    valid = [c for c in choices if c.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive]
    if not valid:
        raise Blocked('Local time does not exist at DST transition')
    if choices[0].utcoffset() != choices[1].utcoffset() and fold is None:
        raise Blocked('Ambiguous local time requires fold 0 or 1')
    return choices[fold or 0].isoformat()


@dataclass(frozen=True)
class Event:
    event_id: str
    session_id: str
    title: str
    start: str | None
    end: str | None
    timezone: str | None
    location: str
    official_source: str
    source_label: str
    status: str = 'unknown'
    source_verified: bool = False
    privacy: str = 'private'

    def __post_init__(self):
        identifier(self.event_id)
        identifier(self.session_id)
        for value in (self.title, self.location, self.source_label):
            _text(value, 500)
        https_url(self.official_source)
        if (self.status not in ('scheduled', 'tentative', 'cancelled', 'unknown')
                or type(self.source_verified) is not bool
                or self.privacy not in ('private', 'public', 'confidential')):
            raise ContractError('Invalid event status, provenance or privacy')
        if self.timezone is not None:
            zone(self.timezone)
        for value in (self.start, self.end):
            if value is not None:
                instant(value)
        if self.start and self.end and instant(self.end) <= instant(self.start):
            raise ContractError('Event end must follow start')

    def blockers(self, *, cancellation=False):
        result = []
        if None in (self.start, self.end, self.timezone):
            result.append('Exact start, end and timezone required')
        if not self.source_verified and self.status != 'tentative':
            result.append('Official source unverified')
        if self.status == 'unknown':
            result.append('Event status unknown')
        if self.status == 'cancelled' and not cancellation:
            result.append('Event cancelled')
        return result

    def preview(self):
        values = asdict(self)
        if self.timezone:
            for key in ('start', 'end'):
                if values[key]:
                    values[key] = instant(values[key]).astimezone(zone(self.timezone)).isoformat()
        return values


@dataclass(frozen=True)
class CalendarTarget:
    """Resolved by the authenticated root, never guessed from a person's name."""
    calendar_id: str
    label: str

    def __post_init__(self):
        identifier(self.calendar_id)
        _text(self.label)


@dataclass(frozen=True)
class Recipient:
    """Trusted root resolution; this value does not confer permission to invite."""
    recipient_id: str
    label: str
    address: str = field(repr=False)

    def __post_init__(self):
        identifier(self.recipient_id)
        _text(self.label)
        # Deliberately narrow mailbox subset, without display-name/header syntax.
        if (type(self.address) is not str or len(self.address) > 254
                or not re.fullmatch(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', self.address)):
            raise ContractError('Resolved recipient mailbox required')


@dataclass(frozen=True)
class CalendarReceipt:
    key: str
    revision: int
    operation: str
    target: CalendarTarget | None
    recipient: Recipient | None
    preview_digest: str
    state: str = 'approved'

    def __post_init__(self):
        identifier(self.key)
        if (type(self.revision) is not int or self.revision < 0
                or self.operation not in ('export', 'add', 'share', 'invite')
                or self.state not in ('approved', 'submitted_unknown', 'confirmed', 'failed')
                or not re.fullmatch(r'[a-f0-9]{64}', self.preview_digest)):
            raise ContractError('Invalid calendar receipt')
        if self.operation == 'export':
            if self.target is not None or self.recipient is not None or self.state != 'approved':
                raise ContractError('Export cannot address a calendar or recipient')
        elif type(self.target) is not CalendarTarget:
            raise ContractError('Resolved calendar required')
        if self.operation in ('share', 'invite'):
            if type(self.recipient) is not Recipient:
                raise ContractError('Resolved recipient required')
        elif self.recipient is not None:
            raise ContractError('Add/export cannot imply an invitation')


class CalendarAdapter:
    def check(self, receipt):
        raise Blocked('Calendar adapter unconfigured; no calendar writes or invitations')


@dataclass(frozen=True)
class CalendarHandoff:
    actor_id: str
    request_id: str
    event: Event
    modified_at: str
    revision: int = 0
    receipts: tuple[CalendarReceipt, ...] = ()
    description: str = ''  # Shared with every invitee; never private ticket/origin data.

    def __post_init__(self):
        identifier(self.actor_id)
        identifier(self.request_id)
        instant(self.modified_at)
        if type(self.description) is not str or len(self.description) > 12000 or any(ord(c) < 32 and c != '\n' for c in self.description):
            raise ContractError('Invalid calendar description')
        if type(self.event) is not Event or type(self.revision) is not int or self.revision < 0:
            raise ContractError('Invalid calendar intent')
        if type(self.receipts) is not tuple or len(self.receipts) > 200:
            raise ContractError('Calendar receipt budget exceeded')
        keys = set()
        for receipt in self.receipts:
            if (type(receipt) is not CalendarReceipt or receipt.key in keys
                    or receipt.revision > self.revision):
                raise ContractError('Invalid calendar receipt history')
            keys.add(receipt.key)
            if receipt.revision == self.revision and receipt.preview_digest != self.preview(
                    receipt.operation, target=receipt.target, recipient=receipt.recipient)['digest']:
                raise ContractError('Receipt does not match calendar revision')

    def _actor(self, actor_id):
        if actor_id != self.actor_id:
            raise Blocked('Calendar actor mismatch')

    @property
    def uid(self):
        # No personal identity or mailbox is exposed through the UID.
        return digest(['calendar-v1', self.actor_id, self.event.event_id,
                       self.event.session_id]) + '@culture-cabinet.invalid'

    def _cancellation_allowed(self):
        return any(r.revision < self.revision and (r.operation == 'export'
                   or (r.operation == 'add' and r.state == 'confirmed')) for r in self.receipts)

    def preview(self, operation='export', *, target=None, recipient=None, recipients=None):
        if recipients is not None:
            return self._recipients_preview(operation, target, recipient, recipients)
        if operation not in ('export', 'add', 'share', 'invite'):
            raise ContractError('Unknown calendar command')
        if target is not None and type(target) is not CalendarTarget:
            raise ContractError('Invalid calendar target')
        if recipient is not None and type(recipient) is not Recipient:
            raise ContractError('Invalid recipient')
        if operation == 'export' and (target is not None or recipient is not None):
            raise ContractError('Export is non-inviting')
        if operation == 'add' and recipient is not None:
            raise ContractError('Add is separate from Share/invite')
        blockers = self.event.blockers(cancellation=self._cancellation_allowed())
        if operation != 'export' and target is None:
            blockers.append('Calendar target unresolved')
        if operation in ('share', 'invite') and recipient is None:
            blockers.append('Recipient unresolved; separate sharing approval required')
        values = {'actor_id': self.actor_id, 'request_id': self.request_id,
                  'revision': self.revision, 'event': self.event.preview(),
                  'modified_at': self.modified_at, 'uid': self.uid, 'operation': operation,
                  'change': ('cancel' if self.event.status == 'cancelled' else
                             'update' if self.revision else 'create'),
                  'target': asdict(target) if target else None,
                  'recipient': asdict(recipient) if recipient else None}
        if self.description:
            values['description'] = self.description
        return {**values, 'digest': digest(values), 'blockers': blockers,
                'effect': 'Local ICS export only' if operation == 'export' else 'Approval only; adapter blocked by default',
                'privacy_label': f'{self.event.privacy.title()} event; sharing requires its own command',
                'source_label': self.event.source_label}

    def _recipients_preview(self, operation, target, recipient, recipients):
        """Read-only grouping of existing exact per-mailbox commands.

        Preserve multiple mailboxes for one resolved person. The group digest is
        not an approval receipt; each address still needs its own explicit command.
        """
        if (operation not in ('share', 'invite') or recipient is not None
                or type(recipients) not in (list, tuple) or not 1 <= len(recipients) <= 10
                or any(type(item) is not Recipient for item in recipients)):
            raise ContractError('Share/invite requires a bounded explicit resolved recipient list')
        if len({item.address for item in recipients}) != len(recipients):
            raise ContractError('Duplicate recipient mailbox')
        previews = [self.preview(operation, target=target, recipient=item) for item in recipients]
        values = {'actor_id': self.actor_id, 'request_id': self.request_id,
                  'revision': self.revision, 'event': self.event.preview(),
                  'operation': operation, 'target': asdict(target) if target else None,
                  'recipients': [asdict(item) for item in recipients],
                  'command_digests': [item['digest'] for item in previews]}
        return {**values, 'digest': digest(values), 'previews': previews,
                'blockers': list(dict.fromkeys(reason for item in previews for reason in item['blockers'])),
                'effect': 'Preview only; each mailbox requires a separate exact approval; adapter blocked by default',
                'privacy_label': f'{self.event.privacy.title()} event; sharing requires its own command',
                'source_label': self.event.source_label}

    def revise(self, event, *, actor_id, expected_revision, modified_at, description=None):
        self._actor(actor_id)
        if expected_revision != self.revision:
            raise Blocked('Stale calendar revision')
        if (event.event_id, event.session_id) != (self.event.event_id, self.event.session_id):
            raise Blocked('Different event/session requires a separate intent')
        description = self.description if description is None else description
        if event == self.event and description == self.description:
            return self
        if instant(modified_at) < instant(self.modified_at):
            raise ContractError('Calendar update timestamp moved backwards')
        return replace(self, event=event, description=description, revision=self.revision + 1, modified_at=modified_at)

    def approve(self, *, actor_id, expected_revision, preview_digest, key,
                operation='export', target=None, recipient=None, user_confirmed=False):
        self._actor(actor_id)
        preview = self.preview(operation, target=target, recipient=recipient)
        if (user_confirmed is not True or expected_revision != self.revision
                or preview_digest != preview['digest']):
            raise Blocked('Explicit approval of the exact calendar preview required')
        if preview['blockers']:
            raise Blocked('; '.join(preview['blockers']))
        receipt = CalendarReceipt(key, self.revision, operation, target, recipient, preview_digest)
        if any(r.key == key and r.preview_digest != preview_digest for r in self.receipts):
            raise Blocked('Idempotency key already belongs to another command')
        for existing in self.receipts:
            if existing.preview_digest == preview_digest:
                return self, existing
        return replace(self, receipts=(*self.receipts, receipt)), receipt

    def _receipt(self, key, actor_id):
        self._actor(actor_id)
        for receipt in self.receipts:
            if receipt.key == key:
                return receipt
        raise Blocked('Unknown calendar receipt')

    def reserve(self, key, *, actor_id, adapter=None):
        """Return (state, receipt, fresh). Persist atomically before downstream I/O.

        An unknown result is retained across revision changes. Reconcile it before
        another command to that same calendar/recipient, even with a new key.
        """
        receipt = self._receipt(key, actor_id)
        if receipt.state != 'approved':
            return self, receipt, False
        if receipt.revision != self.revision or receipt.operation == 'export':
            raise Blocked('Current external calendar approval required')
        if any(r.state == 'submitted_unknown' and r.target.calendar_id == receipt.target.calendar_id
               and (r.recipient.recipient_id if r.recipient else None)
                   == (receipt.recipient.recipient_id if receipt.recipient else None)
               for r in self.receipts):
            raise Blocked('Reconcile unknown calendar submission before another command')
        (adapter or CalendarAdapter()).check(receipt)
        updated = replace(receipt, state='submitted_unknown')
        return self._replace_receipt(updated), updated, True

    def confirm(self, key, *, actor_id, externally_verified=False):
        receipt = self._receipt(key, actor_id)
        if externally_verified is not True or receipt.state not in ('submitted_unknown', 'confirmed'):
            raise Blocked('Verified external calendar outcome required')
        return self._replace_receipt(replace(receipt, state='confirmed'))

    def fail(self, key, *, actor_id, externally_verified=False):
        receipt = self._receipt(key, actor_id)
        if externally_verified is not True or receipt.state not in ('submitted_unknown', 'failed'):
            raise Blocked('Verified failed calendar outcome required')
        return self._replace_receipt(replace(receipt, state='failed'))

    def _replace_receipt(self, receipt):
        return replace(self, receipts=tuple(receipt if r.key == receipt.key else r for r in self.receipts))

    def export_ics(self, key, *, actor_id):
        receipt = self._receipt(key, actor_id)
        if receipt.operation != 'export' or receipt.revision != self.revision:
            raise Blocked('Current explicit Export approval required')
        event = self.event
        if event.blockers(cancellation=self._cancellation_allowed()):
            raise Blocked('Calendar event unavailable')
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Culture Cabinet//Calendar handoff//EN',
                 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'BEGIN:VEVENT', f'UID:{self.uid}',
                 f'SEQUENCE:{self.revision}', f'DTSTAMP:{_utc(self.modified_at)}',
                 f'LAST-MODIFIED:{_utc(self.modified_at)}', f'DTSTART:{_utc(event.start)}',
                 f'DTEND:{_utc(event.end)}', f'SUMMARY:{_ics_text(event.title)}',
                 f'LOCATION:{_ics_text(event.location)}', f'URL:{event.official_source}',
                 f'DESCRIPTION:{_ics_text(self.description or "Source: " + event.source_label)}',
                 f'X-CULTURE-CABINET-TIMEZONE:{_ics_text(event.timezone)}',
                 f'CLASS:{event.privacy.upper()}',
                 'STATUS:CANCELLED' if event.status == 'cancelled' else 'STATUS:TENTATIVE' if event.status == 'tentative' else 'STATUS:CONFIRMED',
                 'END:VEVENT', 'END:VCALENDAR']
        # PUBLISH never emits ATTENDEE/ORGANIZER, including after a separate invite.
        return ('\r\n'.join(_fold(line) for line in lines) + '\r\n').encode('utf-8')

    def to_dict(self):
        data = asdict(self)
        if not self.description:
            data.pop('description')
        return {'schema': 2 if self.description else 1, **data}

    @classmethod
    def from_dict(cls, value):
        """Restore trusted server storage, not an unsigned browser-supplied receipt."""
        try:
            data = dict(value)
            if data.get('schema') == 1 and 'description' not in data:
                data['description'] = ''
            if set(data) != {'schema', *(f.name for f in fields(cls))}:
                raise ValueError
            if type(data['schema']) is not int or data.pop('schema') not in (1, 2):
                raise ValueError
            data['event'] = Event(**data['event'])
            data['receipts'] = tuple(CalendarReceipt(**{**r,
                'target': CalendarTarget(**r['target']) if r['target'] else None,
                'recipient': Recipient(**r['recipient']) if r['recipient'] else None}) for r in data['receipts'])
            return cls(**data)
        except (KeyError, TypeError, ValueError):
            raise ContractError('Invalid stored calendar handoff') from None


def _utc(value):
    utc = instant(value).astimezone(timezone.utc)
    return f'{utc.year:04d}{utc:%m%dT%H%M%SZ}'


def _ics_text(value):
    return value.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _fold(line):
    """RFC5545's 75-octet limit includes the continuation space, never split UTF-8."""
    chunks, current, size = [], '', 0
    for char in line:
        width = len(char.encode('utf-8'))
        if size + width > 75:
            chunks.append(current)
            current, size = ' ', 1
        current += char
        size += width
    chunks.append(current)
    return '\r\n'.join(chunks)
