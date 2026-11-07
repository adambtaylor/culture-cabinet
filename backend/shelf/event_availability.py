"""Bounded, supplied availability evidence and quiet warning reduction; no I/O.

Seller assertions are not estimates. Source attribution is a trusted caller input,
not source authentication. No counts, polling, notifications or purchase effects.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import math

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import EventEvidence, event_timing, reconcile_events

MAX_INPUTS = 100
MAX_MEMORY = 200
MAX_WARNINGS = 9
MAX_PRIOR = 0.75
MAX_PRIOR_CONFIDENCE = 0.5


class SellerStatus(StrEnum):
    LOW = 'seller-low'
    ALMOST_SOLD_OUT = 'almost-sold-out'
    AVAILABLE = 'available'
    SOLD_OUT = 'sold-out'


def _score(value, maximum=1):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise SourceError('Expected a finite bounded score.')


def _sequence(value, limit):
    if type(value) not in (list, tuple) or len(value) > limit:
        raise SourceError('Expected bounded materialized input.')
    return value


def _start(value):
    parsed = timestamp(value)
    if parsed:
        return parsed.astimezone(timezone.utc).isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise SourceError('An explicit event date or instant is required.') from None


@dataclass(frozen=True)
class AvailabilityScope:
    event_key: str
    session_id: str | None = None
    tier_id: str | None = None
    allocation_id: str | None = None
    market: str = 'primary'

    def __post_init__(self):
        label(self.event_key)
        for value in (self.session_id, self.tier_id, self.allocation_id):
            if value is not None:
                label(value)
        if self.market not in ('primary', 'resale'):
            raise SourceError('Primary and resale markets must remain distinct.')

    @property
    def level(self):
        return ('allocation' if self.allocation_id is not None else
                'tier' if self.tier_id is not None else
                'session' if self.session_id is not None else 'event')

    @property
    def description(self):
        parts = [f'{self.market} {self.level}']
        parts.extend(f'{key} {value}' for key, value in (
            ('session', self.session_id), ('tier', self.tier_id), ('allocation', self.allocation_id))
            if value is not None)
        return '; '.join(parts)


@dataclass(frozen=True)
class SellerObservation:
    scope: AvailabilityScope
    status: SellerStatus
    evidence: EventEvidence
    event_start: str
    confidence: float
    max_age_seconds: int = 900

    def __post_init__(self):
        if type(self.scope) is not AvailabilityScope or type(self.status) is not SellerStatus:
            raise SourceError('Typed seller status and scope required.')
        if type(self.evidence) is not EventEvidence:
            raise SourceError('Sourced seller evidence required.')
        label(self.evidence.source_id)
        label(self.evidence.official_url)
        if (timestamp(self.checked_at) is None
                or self.evidence.provenance not in ('fixture', 'supplied-snapshot', 'live-automated')):
            raise SourceError('Seller source and checked-at required.')
        _start(self.event_start)
        _score(self.confidence)
        _age(self.max_age_seconds)

    @property
    def checked_at(self):
        return self.evidence.retrieved_at


@dataclass(frozen=True)
class EstimatedRisk:
    """A bounded, uncalibrated policy prior, never a probability or ticket count."""
    scope: AvailabilityScope
    score: float
    basis: str
    checked_at: str
    event_start: str
    confidence: float
    max_age_seconds: int = 900

    def __post_init__(self):
        if type(self.scope) is not AvailabilityScope or timestamp(self.checked_at) is None:
            raise SourceError('Risk scope and checked-at required.')
        label(self.basis)
        _score(self.score, MAX_PRIOR)
        _score(self.confidence, MAX_PRIOR_CONFIDENCE)
        _start(self.event_start)
        _age(self.max_age_seconds)


def _age(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise SourceError('Freshness window must be between one second and one day.')


@dataclass(frozen=True)
class EventInterest:
    event_key: str
    saved: bool = False
    fit: float = 0

    def __post_init__(self):
        label(self.event_key)
        if type(self.saved) is not bool:
            raise SourceError('Saved must be explicitly supplied.')
        _score(self.fit)


@dataclass(frozen=True)
class AvailabilityView:
    signal: SellerObservation | EstimatedRisk
    freshness: str


@dataclass(frozen=True)
class WarningMemory:
    scope: AvailabilityScope
    channel: str
    material: tuple
    checked_at: str

    def __post_init__(self):
        if (type(self.scope) is not AvailabilityScope or self.channel not in ('seller', 'estimate', 'event')
                or type(self.material) is not tuple or not 1 <= len(self.material) <= 3
                or any(type(value) is not str or len(value) > 100 for value in self.material)
                or timestamp(self.checked_at) is None):
            raise SourceError('Invalid warning memory.')


@dataclass(frozen=True)
class AvailabilityWarning:
    scope: AvailabilityScope
    kind: str
    message: str
    checked_at: str
    confidence: float | None
    source: str
    reference: str


@dataclass(frozen=True)
class AvailabilityResult:
    views: tuple[AvailabilityView, ...]
    warnings: tuple[AvailabilityWarning, ...]
    memory: tuple[WarningMemory, ...]


def reduce_availability(events, signals, *, now, interests=(), previous=(), limit=MAX_WARNINGS):
    """Latest observations per scope; only material changes consume warning slots.

    Reuse the returned memory on the next explicit evaluation. Saved events precede
    high-fit (>= .8) events, then other events. Timestamps/confidence jitter alone
    never repeat a warning. A full memory fails explicitly instead of evicting old
    warnings. The caller owns durable storage and any later notification permission.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise SourceError('An aware clock is required.')
    if type(limit) is not int or not 0 <= limit <= MAX_WARNINGS:
        raise SourceError('Warning budget exceeded.')
    canonical = reconcile_events(events)
    # Exact source identity reconciliation, never a title/venue-name join.
    aliases = {old.key: current for old in events for current in canonical
               if set(old.evidence) & set(current.evidence)}
    aliases.update({event.key: event for event in canonical})
    memory = {}
    for item in _sequence(previous, MAX_MEMORY):
        if type(item) is not WarningMemory:
            raise SourceError('Typed warning memory required.')
        event = aliases.get(item.scope.event_key)
        scope = replace(item.scope, event_key=event.key) if event else item.scope
        key = scope, item.channel
        if key in memory:
            raise SourceError('Duplicate warning memory.')
        memory[key] = replace(item, scope=scope)
    priorities = {}
    for interest in _sequence(interests, MAX_INPUTS):
        if type(interest) is not EventInterest:
            raise SourceError('Typed event interest required.')
        event = aliases.get(interest.event_key)
        if event:
            priority = (0 if interest.saved else 1 if interest.fit >= .8 else 2, -interest.fit)
            priorities[event.key] = min(priority, priorities.get(event.key, (2, 0)))
    latest = {}
    for signal in _sequence(signals, MAX_INPUTS):
        if type(signal) not in (SellerObservation, EstimatedRisk):
            raise SourceError('Typed seller observation or estimate required.')
        event = aliases.get(signal.scope.event_key)
        if event is None:
            raise SourceError('Availability must address an exact supplied event.')
        if isinstance(signal, SellerObservation):
            evidence = signal.evidence
            if ((evidence.provenance == 'fixture') != (event.provenance == 'fixture')
                    or not any((evidence.source_id == ref.source_id and evidence.event_id is not None
                                and evidence.event_id == ref.event_id)
                               or (evidence.official_url is not None and evidence.official_url == ref.official_url)
                               for ref in event.evidence)):
                raise SourceError('Seller evidence is not bound to this event/source mode.')
        signal = replace(signal, scope=replace(signal.scope, event_key=event.key))
        channel = 'seller' if isinstance(signal, SellerObservation) else 'estimate'
        key = signal.scope, channel
        old = latest.get(key)
        if old is None or timestamp(signal.checked_at) > timestamp(old.checked_at):
            latest[key] = signal
        elif timestamp(signal.checked_at) == timestamp(old.checked_at) and signal != old:
            raise SourceError('Conflicting same-time availability; resolve before warning.')
    views, pending = [], {}
    fresh_sellers = set()
    for (scope, channel), signal in latest.items():
        event = aliases[scope.event_key]
        age = now - timestamp(signal.checked_at)
        event_age = now - timestamp(event.retrieved_at)
        freshness = ('future-checked' if age < timedelta(0) or event_age < timedelta(0) else
                     'stale' if max(age, event_age) > timedelta(seconds=signal.max_age_seconds) else 'fresh')
        timing = event_timing(event, now)
        changed = event.starts_at is not None and _start(signal.event_start) != _start(event.starts_at)
        event_fresh = timedelta(0) <= event_age <= timedelta(seconds=signal.max_age_seconds)
        if event_fresh and timing in ('cancelled', 'postponed'):
            freshness = timing
        elif event_fresh and changed and timing in ('future', 'future-date', 'today-date'):
            freshness = 'event-date-changed'
        elif freshness == 'fresh' and timing not in ('future', 'future-date', 'today-date'):
            freshness = timing
        views.append(AvailabilityView(signal, freshness))
        if channel == 'seller' and freshness == 'fresh':
            fresh_sellers.add(scope)
        if freshness in ('cancelled', 'postponed', 'event-date-changed'):
            scope, channel = AvailabilityScope(event.key), 'event'
            material = (freshness, event.starts_at or '')
            warning = AvailabilityWarning(scope, freshness,
                f'Event source reports {freshness}; confirm the revised plan on the official source.',
                event.retrieved_at, None, event.source_id, event.official_url or '')
        elif freshness != 'fresh' or signal.confidence == 0:
            continue
        elif channel == 'seller':
            material = (signal.status.value, _start(event.starts_at))
            warning = AvailabilityWarning(scope, signal.status.value,
                f'Seller reports {signal.status.value} for {scope.description}; checked {signal.checked_at}.',
                signal.checked_at, signal.confidence, signal.evidence.source_id, signal.evidence.official_url)
        else:
            material = ('elevated' if signal.score >= .5 else 'lower', _start(event.starts_at))
            warning = AvailabilityWarning(scope, 'estimated-risk',
                f'Estimated availability risk for {scope.description}; uncalibrated prior, not seller stock.',
                signal.checked_at, signal.confidence, 'estimate', signal.basis)
        key = scope, channel
        old = memory.get(key)
        if old and timestamp(warning.checked_at) < timestamp(old.checked_at):
            continue
        updated = WarningMemory(scope, channel, material, warning.checked_at)
        if old and old.material == material:
            memory[key] = updated
        elif (channel == 'seller' and material[0] == 'available' and old is None
              or channel == 'estimate' and material[0] == 'lower'):
            memory[key] = updated
        else:
            pending[key] = (warning, updated)
    ordered = sorted(pending.items(), key=lambda pair: (
        priorities.get(pair[0][0].event_key, (2, 0)), pair[0][0].event_key,
        pair[0][1], pair[0][0].description))
    warnings = []
    for key, (warning, updated) in ordered:
        if key[1] == 'estimate' and key[0] in fresh_sellers:
            continue
        if len(warnings) >= limit:
            break
        warnings.append(warning)
        memory[key] = updated
    if len(memory) > MAX_MEMORY:
        raise SourceError('Warning memory budget exceeded; explicit reconciliation required.')
    return AvailabilityResult(tuple(views), tuple(warnings), tuple(memory.values()))
