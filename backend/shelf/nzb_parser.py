"""Bounded NZB validation. Message IDs are opaque and never returned or logged."""
from xml.parsers import expat
import re

from shelf.acquisition_contract import ContractError, MAX_NZB_BYTES
from shelf.obtain_parser import words

NAMESPACES = {'http://www.newzbin.com/DTD/2003/nzb',
              'https://www.newzbin.com/DTD/2003/nzb'}
MAX_ELEMENTS = 20000
MAX_SEGMENTS = 10000


def validate_nzb(payload, *, album=False):
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_NZB_BYTES:
        raise ContractError('Invalid or oversized NZB')
    parser = expat.ParserCreate(encoding='UTF-8', namespace_separator='}')
    stack, numbers = [], set()
    elements = files = segments = 0
    message = ''

    def refuse(*args):
        raise ContractError('Invalid or unsupported NZB XML')

    def doctype(name, system_id, public_id, internal):
        # Conventional external declarations are inert. Never load any DTD.
        if name != 'nzb' or internal or not system_id:
            refuse()

    def start(tag, attrs):
        nonlocal elements, files, segments, message
        if '}' in tag:
            namespace, tag = tag.rsplit('}', 1)
            if namespace not in NAMESPACES:
                refuse()
        elements += 1
        if elements > MAX_ELEMENTS or len(stack) >= 8 or len(attrs) > 16:
            refuse()
        parent = stack[-1] if stack else None
        allowed = {None: {'nzb'}, 'nzb': {'head', 'file'}, 'head': {'meta'},
                   'file': {'groups', 'segments'}, 'groups': {'group'},
                   'segments': {'segment'}}
        if tag not in allowed.get(parent, set()):
            refuse()
        if tag == 'file':
            subject = attrs.get('subject', '')
            if album and (re.search(r'\b(password|passworded|encrypted|protected|discography|pack)\b',
                                    words(subject))
                          or re.search(r'(?i)\.(exe|com|msi|bat|cmd|ps1|sh|scr|apk)(?:\b|$)', subject)):
                refuse()
            files += 1
            numbers.clear()
        elif tag == 'meta' and album and attrs.get('type', '').casefold() in {
                'password', 'passworded', 'encrypted', 'protected'}:
            refuse()
        elif tag == 'segment':
            segments += 1
            if segments > MAX_SEGMENTS:
                refuse()
            for key in ('bytes', 'number'):
                raw = attrs.get(key, '')
                if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or not 0 < int(raw) < 2**63:
                    refuse()
            number = int(attrs['number'])
            if number in numbers:
                refuse()
            numbers.add(number)
            message = ''
        stack.append(tag)

    def data(value):
        nonlocal message
        if stack and stack[-1] == 'segment':
            message += value
            if len(message) > 1024:
                refuse()
        elif (not stack or stack[-1] not in {'meta', 'group'}) and value.strip():
            refuse()

    def end(tag):
        nonlocal message
        tag = stack.pop()
        if tag == 'segment':
            if not message.strip() or any(ord(c) < 32 and c not in '\t\r\n' for c in message):
                refuse()
            message = ''
        if tag == 'file' and not numbers:
            refuse()
        if tag == 'file' and album and max(numbers) != len(numbers):
            # Positive, unique numbers must cover 1..N. This catches known gaps,
            # but cannot establish the absent tail or provider article retention.
            refuse()

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    try:
        parser.Parse(payload, True)
    except (expat.ExpatError, UnicodeError, ValueError):
        raise ContractError('Invalid or unsupported NZB XML') from None
    if not files or not segments:
        refuse()
