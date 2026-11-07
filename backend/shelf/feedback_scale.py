"""Explicit anticipated interest and unchanged historical response meanings.

Weights guide ordering only: they are neither ratings nor evidence of consumption.
Owner notes are opaque data, never selector instructions.
"""
import unicodedata

REACTION_LABELS = (
    ('love_it', 'Love it'),
    ('intrigued', 'Intrigued'),
    ('mildly_interested', 'Mildly interested'),
    ('sounds_boring', 'Sounds boring'),
    ('hate_it', 'Hate it'),
)
LEGACY_CHOICES = ('enjoyed', 'interested', 'not_for_me', 'unknown')
ALL_LABELS = REACTION_LABELS + (
    ('enjoyed', 'Enjoyed (experienced positive)'),
    ('interested', 'Interested (anticipated positive)'),
    ('not_for_me', 'Not for me (negative)'),
    ('unknown', 'Unknown (no taste signal)'),
    ('skipped', 'Skipped (no taste preference)'),
)
ALL_CHOICES = tuple(choice for choice, _ in ALL_LABELS)
WEIGHTS = dict(love_it=3, intrigued=2, mildly_interested=1, sounds_boring=-1,
               hate_it=-3, enjoyed=3, interested=1, not_for_me=-3, unknown=0, skipped=0)


def weight(choice):
    return WEIGHTS[choice]


def is_positive(choice):
    return weight(choice) > 0


def is_negative(choice):
    return weight(choice) < 0


def clean_note(value):
    """Validate and return exact owner text, including boundary whitespace.

    LF, tab and standard CRLF are allowed; control, format and surrogate characters are not.
    Both limits apply before any persistence or request fingerprinting.
    """
    if (not isinstance(value, str) or len(value) > 500
            or any(unicodedata.category(c).startswith('C') and c not in '\n\t'
                   and not (c == '\r' and i + 1 < len(value) and value[i + 1] == '\n')
                   for i, c in enumerate(value))):
        raise ValueError('Note must be at most 500 characters of plain text.')
    if len(value.encode('utf-8')) > 2000:
        raise ValueError('Note exceeds the UTF-8 byte limit.')
    return value
