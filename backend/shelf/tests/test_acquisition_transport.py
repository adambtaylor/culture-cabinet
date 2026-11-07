"""Actual request construction over injected HTTP connections; no sockets."""
import io
import json
import pickle
import socket
import time
from email import policy
from email.parser import BytesParser
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from shelf.acquisition_contract import (
    Client, ContractError, Permissions, PublicRequest, MAX_RESPONSE_BYTES,
    search_request, parse_caps,
)
from shelf.acquisition_transport import LoopbackTransport, TransportFailure, _DeadlineSocket
from shelf.tests.test_acquisition_contract import CAPS, selection, INERT_NZB

KEY = '合成-é +/&?=#%'


class FakeHTTP:
    def __init__(self, payload=CAPS, status=200, headers=None, failure=None):
        self.payload, self.status = payload, status
        self.headers = {} if headers is None else headers
        self.failure = failure
        self.calls = []
        self.closed = False
        self.connections = []

    def factory(self, host, port, *, timeout):
        self.connections.append((host, port, timeout))
        return self

    def connect(self):
        pass

    def request(self, method, path, *, body, headers):
        self.calls.append((method, path, body, headers))
        if self.failure:
            raise self.failure

    def getresponse(self):
        return self

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, limit):
        self.read_limit = limit
        return self.payload[:limit]

    def close(self):
        self.closed = True


def client(http):
    return Client(Permissions(search=True, enqueue=True),
                  LoopbackTransport(_connection_factory=http.factory))


class TransportTests(TestCase):
    def test_caps_fixed_endpoint_key_encoded_once_and_public_separation(self):
        http = FakeHTTP()
        self.assertTrue(client(http).caps(credential=KEY).text_search)
        method, path, body, headers = http.calls[0]
        self.assertEqual((method, urlsplit(path).path, body), ('GET','/api',None))
        self.assertEqual(parse_qs(urlsplit(path).query), {'t':['caps'],'apikey':[KEY]})
        self.assertEqual(http.connections, [('127.0.0.1',5076,2.0)])
        self.assertNotIn(KEY, repr(PublicRequest('caps').public_dict()))
        self.assertNotIn('apikey', repr(headers))
        self.assertTrue(http.closed)

    def test_search_reserved_query_and_fixed_bound(self):
        http = FakeHTTP(b'{"items":[]}')
        query = 'synthetic +/&?=#%'
        client(http).search(parse_caps(CAPS),query,'film',credential=KEY)
        self.assertEqual(parse_qs(urlsplit(http.calls[0][1]).query),
                         {'t':['search'],'q':[query],'cat':['2000'],'o':['json'],
                          'limit':['20'],'offset':['0'],'apikey':[KEY]})

    def test_exact_add_only_multipart(self):
        http = FakeHTTP(b'{"status":true,"nzo_ids":["SABnzbd_nzo_fixture"]}')
        client(http).enqueue(selection(),'request_fixture',INERT_NZB,credential=KEY)
        method,path,body,headers = http.calls[0]
        self.assertEqual((method,path), ('POST','/api'))
        self.assertEqual(http.connections, [('127.0.0.1',8080,2.0)])
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: '+headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
        parts = list(message.iter_parts())
        self.assertEqual([p.get_param('name',header='content-disposition') for p in parts],
                         ['mode','output','apikey','nzbname','name'])
        self.assertEqual([p.get_payload(decode=True) for p in parts],
                         [b'addfile',b'json',KEY.encode(),b'CC-request_fixture',INERT_NZB])
        self.assertEqual(parts[-1].get_filename(),'selection.nzb')
        self.assertNotIn(b'api_key',body)
        self.assertNotIn(b'addurl',body)

    def test_http200_xml_error100_never_caps_success(self):
        for payload in (b'\xef\xbb\xbf<error code="100" description="SECRET"/>',
                        b'<error code="100" description="SECRET"/>',
                        b'<caps><error code="100" description="SECRET"/></caps>',
                        b'<x:error xmlns:x="urn:newznab" code="100"/>'):
            http=FakeHTTP(payload)
            with self.assertRaises(ContractError) as caught:
                client(http).caps(credential=KEY)
            self.assertNotIn('SECRET',str(caught.exception))
            self.assertNotIn(KEY,str(caught.exception))
            self.assertEqual(len(http.calls),1)

    def test_timeout_redirect_rate_oversize_and_malformed_are_sanitized(self):
        cases = [FakeHTTP(failure=TimeoutError(KEY)), FakeHTTP(status=302,headers={'Location':'http://evil.invalid/'}),
                 FakeHTTP(status=429),FakeHTTP(status=500),
                 FakeHTTP(b'x'*(MAX_RESPONSE_BYTES+1)),
                 FakeHTTP(headers={'Content-Length':str(MAX_RESPONSE_BYTES+1)}),
                 FakeHTTP(headers={'Content-Encoding':'gzip'}),
                 FakeHTTP(headers={'Content-Length':'-1'}),
                 FakeHTTP(headers={'Content-Length':'1'}),
                 FakeHTTP(b'<!DOCTYPE caps [<!ENTITY x "secret">]><caps/>')]
        for http in cases:
            with self.subTest(status=http.status), self.assertRaises(ContractError) as caught:
                client(http).caps(credential=KEY)
            self.assertNotIn(KEY,str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(http.closed)
            self.assertEqual(len(http.calls),1)

    def test_private_parameter_injection_never_connects(self):
        http=FakeHTTP()
        transport=LoopbackTransport(_connection_factory=http.factory)
        for private in ({}, {'apikey':KEY,'api_key':'admin'}, {'APIKEY':KEY}, {'apikey':'x\r\ny'}):
            with self.assertRaises(ContractError):
                transport.send(PublicRequest('caps'),private_params=private)
        self.assertEqual(http.calls,[])
        self.assertEqual(http.connections,[])

    def test_unconfirmed_selection_never_connects(self):
        http=FakeHTTP()
        with self.assertRaises(ContractError):
            client(http).enqueue(None,'id',INERT_NZB,credential=KEY)
        self.assertEqual(http.connections,[])

    def test_absolute_socket_deadline_without_connect_or_io(self):
        fake=Mock()
        fake.deadline=time.monotonic()-1
        with self.assertRaises(TimeoutError):
            _DeadlineSocket._remaining(fake)
        fake.settimeout.assert_not_called()
        fake.deadline=time.monotonic()+1
        _DeadlineSocket._remaining(fake)
        self.assertGreater(fake.settimeout.call_args.args[0],0)
        self.assertLessEqual(fake.settimeout.call_args.args[0],1)
