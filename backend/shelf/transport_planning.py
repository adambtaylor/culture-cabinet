"""Source-backed constraints and explicitly proposed timing; no routing requests."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from shelf.calendar_handoff import instant, local_time
from shelf.transport_watch import preview_windows

REVIEWED_AT = datetime(2026, 10, 30, 0, 1, tzinfo=timezone.utc)
RULES_URL = 'https://transport.vic.gov.au/plan-a-journey/travel-tips-and-resources/bringing-items-on-public-transport/bicycles-and-public-transport'
JOURNEY_URL = 'https://transport.vic.gov.au/journey'
TIMETABLES_URL = 'https://transport.vic.gov.au/timetables'
WORKS_URL = 'https://transport.vic.gov.au/plan-a-journey'
STATIONS = (('batman', 'Batman Station'), ('coburg', 'Coburg Station'), ('merlynston', 'Merlynston Station'))
ORIGINS = (*STATIONS, ('flinders', 'Flinders Street Station'), ('southern-cross', 'Southern Cross Station'),
           ('richmond', 'Richmond Station'), ('brunswick', 'Brunswick Station'))
MODES = (('metro_train', 'Metropolitan train'), ('tram', 'Tram'), ('bus', 'Bus'),
         ('replacement_bus', 'Metropolitan rail replacement bus'), ('train_tram', 'Metropolitan train then tram'))
BIKES = (('full_size', 'Full-size pedal bicycle'), ('factory_ebike', 'Factory-built full-size e-bike'),
         ('folded', 'Folding bicycle, folded within official size limits'),
         ('converted_ebike', 'Converted e-bike'))


def bicycle_rule(bike, mode, carry, now):
    if bike not in dict(BIKES) or mode not in dict(MODES) or type(carry) is not bool:
        raise ValueError("Explicit bicycle, supported mode and carriage choice required.")
    if not REVIEWED_AT <= now < REVIEWED_AT + timedelta(days=30):
        return {'status': 'unverified', 'message': 'Guidance review is stale or dated in the future. Check the current official rules.'}
    if not carry:
        return {'status': 'conditional', 'message': 'Leave the bicycle outside ticketed areas. Parking, locks and access are unverified.'}
    if bike == 'converted_ebike':
        return {'status': 'blocked', 'message': 'Converted e-bikes are banned on trains and in ticketed areas; this plan cannot carry one.'}
    if mode != 'metro_train' and bike != 'folded':
        return {'status': 'blocked', 'message': 'A full-size bicycle cannot accompany this tram/bus leg, including a metropolitan replacement bus. Change mode or leave it parked.'}
    if bike == 'folded':
        return {'status': 'conditional', 'message': 'Folding bikes must meet the official size limits and not obstruct aisles; your selection is a declaration, not a measured check.'}
    return {'status': 'conditional', 'message': 'Metropolitan trains permit these bikes subject to space/access needs. Avoid the first door of the first carriage and keep aisles clear. E-bikes must be switched off.'}


def cycle_links(origin):
    """Supported Google Maps URLs; no request, key, geocoding or saved origin."""
    if not origin:
        return []
    base = 'https://www.google.com/maps/dir/?'
    return [{'station': station,
        'outward': base + urlencode({'api': 1, 'origin': origin, 'destination': station + ', Victoria, Australia', 'travelmode': 'bicycling'}),
        'returning': base + urlencode({'api': 1, 'origin': station + ', Victoria, Australia', 'destination': origin, 'travelmode': 'bicycling'})}
        for key, station in STATIONS]


def station_comparison(values):
    """Rank only complete user estimates, never infer home location or timetables."""
    rows = []
    for key, station in STATIONS:
        cycle, onward = values.get(key + '_cycle'), values.get(key + '_onward')
        complete = cycle is not None and onward is not None
        rows.append({'key': key, 'station': station, 'cycle': cycle, 'onward': onward,
            'total': cycle + onward + values['lock_minutes'] + values['arrival_buffer'] if complete else None,
            'return_station': station if not values['carry_bike'] else None})
    ranked = sorted((row for row in rows if row['total'] is not None), key=lambda row: row['total'])
    lowest = ranked[0]['total'] if ranked else None
    winners = [row['station'] for row in ranked if row['total'] == lowest]
    return {'rows': rows, 'winners': winners, 'complete': len(ranked) == len(STATIONS),
        'basis': 'Lowest supplied outward estimate including bicycle handling and arrival buffer; no timetable, route, safety or last-return verification.'}


def proposal(event, values, now):
    details = event.details
    source_clock = details.show_time if details else None
    clock = source_clock or values['assumed_show_time']
    start = instant(local_time(event.starts_at + 'T' + clock, 'Australia/Melbourne')) if clock else None
    origin = dict(ORIGINS)[values['origin']]
    arrival = dict(ORIGINS)[values['arrival']]
    outward = bicycle_rule(values['bike'], values['outward_mode'], values['carry_bike'], now)
    returning = bicycle_rule(values['bike'], values['return_mode'], values['carry_bike'], now)
    buffer = values['arrival_buffer'] + values['lock_minutes']
    depart = start - timedelta(minutes=values['outward_minutes'] + buffer) if start else None
    companion = dict(ORIGINS).get(values['companion_origin'])
    companion_depart = start - timedelta(minutes=values['companion_minutes'] + buffer) if start and companion else None
    return_day = datetime.fromisoformat(event.starts_at).date() + timedelta(days=values['return_day'])
    leave = instant(local_time(return_day.isoformat() + 'T' + values['leave_time'], 'Australia/Melbourne'))
    if start and not start < leave <= start + timedelta(hours=18):
        raise ValueError('Choose an explicit same-day or next-day return after the proposed event start, within 18 hours.')
    return {'event': event, 'origin': origin, 'arrival': arrival,
        'companion_origin': companion, 'companion_depart': companion_depart,
        'outward': outward, 'returning': returning,
        'blocked': any(r['status'] != 'conditional' for r in (outward, returning)),
        'start': start, 'start_basis': 'explicit source show wording' if source_clock else 'your planning assumption' if clock else 'unknown',
        'depart': depart, 'leave': leave,
        'return_arrival': leave + timedelta(minutes=values['return_minutes'] + values['lock_minutes']),
        'values': values, 'rules_reviewed_at': REVIEWED_AT, 'rules_url': RULES_URL,
        'journey_url': JOURNEY_URL, 'timetables_url': TIMETABLES_URL, 'works_url': WORKS_URL,
        'station_comparison': station_comparison(values), 'cycle_links': cycle_links(values.get('cycle_origin', '')), 
        'parked_bike_station': origin if not values['carry_bike'] else None,
        'watch_windows': preview_windows(depart,leave),
        'ready': False, 'timed_journey_verified': False}
