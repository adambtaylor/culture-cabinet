"""Fixed loopback HTTP adapter. No proxy, redirect, retry, logging or URL input."""
import http.client
import re
import socket
import time
from urllib.parse import urlencode
from uuid import uuid4
from xml.etree import ElementTree

from shelf.acquisition_contract import (
    ContractError, PublicRequest, ConfirmedSelection, MAX_NZB_BYTES, MAX_RESPONSE_BYTES,
    sab_name,
)

CONNECT_TIMEOUT = 2.0
REQUEST_TIMEOUT = 10.0


class TransportFailure(ContractError):
    pass


class _DeadlineSocket(socket.socket):
    """Apply one absolute deadline even to slowly arriving HTTP headers/body."""
    def __init__(self, connected, deadline):
        super().__init__(connected.family, connected.type, connected.proto,
                         fileno=connected.detach())
        self.deadline = deadline

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.settimeout(remaining)

    def recv_into(self, *args, **kwargs):
        self._remaining()
        return super().recv_into(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._remaining()
        return super().sendall(*args, **kwargs)


class _Connection(http.client.HTTPConnection):
    def connect(self):
        deadline = time.monotonic() + REQUEST_TIMEOUT
        super().connect()
        self.sock = _DeadlineSocket(self.sock, deadline)


def _xml_error(payload):
    """Newznab errors are often HTTP 200; never return their descriptions."""
    payload = payload.removeprefix(b'\xef\xbb\xbf').lstrip()
    if not payload.startswith(b'<'):
        return
    try:
        text = payload.decode('utf-8')
        if ('<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper() or '\0' in text):
            raise TransportFailure('Invalid upstream response')
        root = ElementTree.fromstring(text)
    except (UnicodeError, ElementTree.ParseError):
        raise TransportFailure('Invalid upstream response') from None
    if any(node.tag.rsplit('}', 1)[-1].lower() == 'error' for node in root.iter()):
        raise TransportFailure('Upstream request refused')


def _multipart(key, nzb, request):
    boundary = 'cc-' + uuid4().hex
    # Avoid even a synthetic adversarial payload terminating a multipart part.
    while boundary.encode() in nzb or boundary in key:
        boundary = 'cc-' + uuid4().hex
    parts = []
    fields = [('mode', 'addfile'), ('output', 'json'), ('apikey', key),
              ('nzbname', sab_name(request.request_id))]
    if request.job_options is not None:
        options = request.job_options
        fields.extend((('script', options.script), ('pp', str(options.pp)),
                       ('cat', options.category)))
    for name, value in fields:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                      f'\r\n\r\n{value}\r\n').encode('utf-8'))
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="name"; '
                  'filename="selection.nzb"\r\nContent-Type: application/x-nzb\r\n\r\n').encode())
    parts.extend((nzb, f'\r\n--{boundary}--\r\n'.encode()))
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


class LoopbackTransport:
    def __init__(self, *, _connection_factory=_Connection):
        self._connection_factory = _connection_factory

    def send(self, request, *, private_params, nzb=None):
        # Reconstruct instead of trusting endpoint/method properties on a subclass.
        if type(request) is not PublicRequest:
            raise ContractError('Unsupported public request')
        PublicRequest(request.operation, request.params, request.selection, request.request_id,
                      request.job_options)
        if (type(private_params) is not dict or set(private_params) != {'apikey'}
                or type(private_params['apikey']) is not str):
            raise ContractError('Invalid private credential')
        key = private_params['apikey']
        try:
            key_size = len(key.encode('utf-8'))
        except UnicodeError:
            raise ContractError('Invalid private credential') from None
        if (not key.strip() or key_size > 4096
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in key)):
            raise ContractError('Invalid private credential')
        body, headers = None, {'Accept': 'application/json, application/xml'}
        limit = MAX_NZB_BYTES if request.operation == 'get' else MAX_RESPONSE_BYTES
        if request.operation == 'get':
            headers['Accept'] = 'application/x-nzb'
        if request.operation == 'addfile':
            selection = request.selection
            ConfirmedSelection(**vars(selection))
            if type(nzb) is not bytes or not 0 < len(nzb) <= MAX_NZB_BYTES:
                raise ContractError('Invalid or oversized NZB payload')
            body, content_type = _multipart(key, nzb, request)
            headers['Content-Type'] = content_type
            method, port, path = 'POST', 8080, '/api'
        else:
            if nzb is not None:
                raise ContractError('Unexpected NZB payload')
            params = {'t': request.operation, **dict(request.params)}
            if request.operation == 'search':
                params.update(o='json', limit='20', offset='0')
            params['apikey'] = key
            method, port, path = 'GET', 5076, '/api?' + urlencode(params)
        connection = None
        try:
            connection = self._connection_factory('127.0.0.1', port, timeout=CONNECT_TIMEOUT)
            connection.connect()
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            # No redirect following; no retry, including 429 and failed POSTs.
            if response.status != 200:
                raise TransportFailure('Upstream HTTP failure')
            encoding = response.getheader('Content-Encoding', 'identity')
            length = response.getheader('Content-Length')
            if request.operation == 'get' and response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/x-nzb':
                raise TransportFailure('Unexpected NZB response type')
            if (encoding != 'identity' or (length is not None and
                    (not re.fullmatch(r'[0-9]{1,10}', length)
                     or int(length) > limit))):
                raise TransportFailure('Invalid or oversized upstream response')
            payload = response.read(limit + 1)
            if (len(payload) > limit
                    or (length is not None and len(payload) != int(length))):
                raise TransportFailure('Invalid or oversized upstream response')
            if request.operation == 'get':
                from shelf.nzb_parser import validate_nzb
                validate_nzb(payload)
            else:
                _xml_error(payload)
            return payload
        except TransportFailure:
            raise
        except Exception:
            raise TransportFailure('Upstream unavailable; outcome unknown') from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass  # Cleanup cannot replace a sanitized outcome.
