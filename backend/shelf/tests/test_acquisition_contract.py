"""Owned, inert fixtures only. No HTTP, sockets, subprocesses or private config."""
from dataclasses import FrozenInstanceError
from unittest import TestCase
from unittest.mock import Mock

from shelf.acquisition_contract import (
    Blocked, CATEGORIES, Client, ConfirmedSelection, ContractError,
    HYDRA, MAX_CAPS_ELEMENTS, MAX_CATEGORIES, MAX_NZB_BYTES,
    MAX_RESPONSE_BYTES, NoFetch, Permissions, PublicRequest, SAB,
    parse_caps, search_request,
)


# Hand-written Newznab subset, not a captured or verified Hydra response.
CAPS = b'''<caps><searching>
  <search available="yes" supportedParams="q"/>
  <tv-search available="yes" supportedParams="q,season,ep"/>
  <movie-search available="yes" supportedParams="q"/>
  <book-search available="yes" supportedParams="q"/>
</searching><categories>
  <category id="2000" name="Movies"/>
  <category id="5000" name="TV"/>
  <category id="3000" name="Audio"><subcat id="3030" name="Audiobooks"/></category>
  <category id="7000" name="Books"><subcat id="7020" name="Ebooks"/></category>
</categories></caps>'''
INERT_NZB = b'OWNED INERT TEST BYTES: no NZB, no articles, no media'
FAKE_KEY = 'FICTIONAL-TEST-ONLY-KEY'


def selection(media="film", **changes):
    values = dict(item_id="owned-fiction-1", media=media, edition="Verified full edition",
                  storage="Checked local destination", authorized_content=True,
                  user_confirmed=True, edition_verified=True, complete_work_verified=True,
                  storage_checked=True, duplicate_checked=True,
                  episode="S01E01" if media == "episode" else None)
    values.update(changes)
    return ConfirmedSelection(**values)


class AcquisitionContractTests(TestCase):
    def test_default_permissions_and_no_fetch_are_blocked(self):
        client = Client()
        self.assertEqual(client.permissions, Permissions(False, False, False, False))
        self.assertIsInstance(client.transport, NoFetch)
        for action in (lambda: client.caps(credential=FAKE_KEY),
                       lambda: client.search(parse_caps(CAPS), "Owned film", "film"),
                       lambda: client.enqueue(selection(), "request-1", INERT_NZB),
                       client.monitor, client.cancel):
            with self.assertRaises(Blocked):
                action()
        with self.assertRaises(Blocked):
            Client(Permissions(search=True)).caps(credential=FAKE_KEY)
        with self.assertRaises(ContractError):
            Permissions(search="Allow")

    def test_caps_enumerates_supported_queries_and_categories(self):
        caps = parse_caps(CAPS)
        self.assertEqual(caps.query_types, frozenset({"search", "tvsearch", "movie", "book"}))
        self.assertIn((3030, "Audiobooks"), caps.categories)
        self.assertIn((7020, "Ebooks"), caps.categories)
        self.assertTrue(caps.text_search)
        self.assertFalse(parse_caps(b"<caps/>").text_search)

    def test_caps_rejects_dtd_entities_and_other_encodings(self):
        for payload in (b'<!DOCTYPE caps [<!ENTITY a "x">]><caps>&a;</caps>',
                        b'<!ENTITY a "x"><caps/>', b'\xff',
                        '<!DOCTYPE caps><caps/>'.encode("utf-16"),
                        '<!DOCTYPE caps><caps/>'.encode("utf-16-le")):
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                parse_caps(payload)

    def test_caps_rejects_invalid_xml_and_excess_bytes_elements_categories(self):
        for payload in (b"<caps>", b"<notcaps/>", b"x" * (MAX_RESPONSE_BYTES + 1),
                        b"<caps>" + b"<x/>" * MAX_CAPS_ELEMENTS + b"</caps>",
                        b'<caps><categories><category id="0" name="Bad"/></categories></caps>',
                        b'<caps><categories><category id="a" name="Bad"/></categories></caps>',
                        b'<caps><categories><category id="2"/></categories></caps>',
                        b'<caps><categories><category id="2" name="A"/>'
                        b'<category id="2" name="B"/></categories></caps>'):
            with self.subTest(payload=payload[:80]), self.assertRaises(ContractError):
                parse_caps(payload)
        many = ''.join(f'<category id="{i+1}" name="Cat"/>' for i in range(MAX_CATEGORIES + 1))
        with self.assertRaises(ContractError):
            parse_caps(f'<caps><categories>{many}</categories></caps>'.encode())

    def test_textsearch_fallback_never_assumes_ids_or_advanced_operations(self):
        caps = parse_caps(CAPS.replace(b'</categories>',
            b'<category id="3040" name="Lossless"/></categories>'))
        for media, category in CATEGORIES.items():
            request = search_request(caps, "Owned fiction S01E01", media)
            self.assertEqual(request.public_dict()["params"], {
                "t": "search", "q": "Owned fiction S01E01", "cat": str(category), "o": "json"})
            self.assertEqual((request.endpoint, request.method), (HYDRA, "GET"))
        generic_only = CAPS.replace(b'available="yes" supportedParams="q,season,ep"',
                                    b'available="no" supportedParams="q,season,ep"')
        self.assertEqual(search_request(parse_caps(generic_only), "Owned", "episode").operation,
                         "search")

    def test_search_requires_advertised_text_query_and_exact_category(self):
        for payload in (CAPS.replace(b'<search available="yes"', b'<search available="no"'),
                        CAPS.replace(b'supportedParams="q"', b'supportedParams="imdbid"'),
                        CAPS.replace(b'id="3030"', b'id="3031"')):
            with self.assertRaises(Blocked):
                search_request(parse_caps(payload), "Owned audiobook", "audiobook")
        with self.assertRaises(ContractError):
            search_request(parse_caps(CAPS), "Owned ebook", "ebook")

    def test_query_bounded_no_controls_no_url_input_or_parameter_injection(self):
        caps = parse_caps(CAPS)
        for query in ("", "  ", "x" * 201, "q\nmode=addfile", "q\x00", "q\u202e"):
            with self.subTest(query=query), self.assertRaises(ContractError):
                search_request(caps, query, "film")
        request = search_request(caps, "http://elsewhere/?apikey=fake&mode=addfile", "film")
        self.assertEqual(request.endpoint, HYDRA)
        self.assertEqual(len(request.params), 2)  # Query is data, never an endpoint or extra params.
        for operation in ("http://elsewhere/", "queue", "history", "pause", "config", "get"):
            with self.assertRaises(ContractError):
                PublicRequest(operation)
        with self.assertRaises(ContractError):
            PublicRequest("search", (("apikey", FAKE_KEY), ("cat", "2000")))

    def test_search_and_enqueue_permissions_are_independent(self):
        transport = Mock()
        transport.send.return_value = CAPS
        search = Client(Permissions(search=True), transport)
        search.caps()
        transport.reset_mock()
        with self.assertRaises(Blocked):
            search.enqueue(selection(), "req-1", INERT_NZB)
        transport.send.assert_not_called()
        enqueue = Client(Permissions(enqueue=True), transport)
        with self.assertRaises(Blocked):
            enqueue.caps()
        with self.assertRaises(Blocked):
            enqueue.search(parse_caps(CAPS), "Owned", "film")
        transport.send.assert_not_called()

    def test_private_key_separate_from_public_request_and_client_repr(self):
        transport = Mock()
        transport.send.return_value = CAPS
        client = Client(Permissions(search=True), transport)
        client.caps(credential=FAKE_KEY)
        args, kwargs = transport.send.call_args
        self.assertEqual(kwargs["private_params"], {"apikey": FAKE_KEY})
        request = args[0]
        self.assertEqual(request.public_dict()["params"], {"t": "caps"})
        self.assertNotIn(FAKE_KEY, repr(request))
        self.assertNotIn(FAKE_KEY, repr(request.public_dict()))
        self.assertNotIn(FAKE_KEY, repr(client))

    def test_transport_exceptions_do_not_expose_credential_or_response(self):
        for failure in (RuntimeError(FAKE_KEY), Blocked(FAKE_KEY)):
            transport = Mock()
            transport.send.side_effect = failure
            with self.assertRaises(ContractError) as caught:
                Client(Permissions(search=True), transport).caps(credential=FAKE_KEY)
            self.assertNotIn(FAKE_KEY, repr(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_search_response_is_bounded_opaque_not_available_or_joined(self):
        transport = Mock()
        transport.send.return_value = b'OWNED OPAQUE FIXTURE: unknown live result schema'
        client = Client(Permissions(search=True), transport)
        self.assertEqual(client.search(parse_caps(CAPS), "Owned", "film"),
                         transport.send.return_value)
        self.assertEqual(transport.send.call_count, 1)
        for response in (b"x" * (MAX_RESPONSE_BYTES + 1), {"available": True}):
            transport.send.return_value = response
            with self.assertRaises(ContractError):
                client.search(parse_caps(CAPS), "Owned", "film")

    def test_selection_authorization_required_without_impossible_preverification(self):
        for flag in ("authorized_content", "user_confirmed", "storage_checked", "duplicate_checked"):
            for value in (False, None, "confirmed", 1):
                with self.subTest(flag=flag, value=value), self.assertRaises(Blocked):
                    selection("audiobook", **{flag: value})
        with self.assertRaises(ContractError):
            selection("audiobook", edition="")
        self.assertEqual(selection("audiobook").media, "audiobook")
        pending = selection("audiobook", edition_verified=False, complete_work_verified=False)
        self.assertFalse(pending.edition_verified)
        self.assertFalse(pending.complete_work_verified)
        for flag in ("edition_verified", "complete_work_verified"):
            with self.assertRaises(ContractError):
                selection("audiobook", **{flag: "verified"})

    def test_exact_episode_rejects_packs_multi_episode_and_ranges(self):
        self.assertEqual(selection("episode").episode, "S01E01")
        for episode in (None, "S01", "S01E01E02", "S01E01-E03", "S01E01-02",
                        "S000E01", "S01E00", "S01E001", "S01E01 season pack", "s01e01"):
            with self.subTest(episode=episode), self.assertRaises(ContractError):
                selection("episode", episode=episode)
        self.assertEqual(selection("episode", episode="S02E02").episode, "S02E02")
        with self.assertRaises(ContractError):
            selection("film", episode="S01E01")

    def test_enqueue_immutable_confirmed_request_multipart_owned_bytes_only(self):
        transport = Mock()
        transport.send.return_value = b'OWNED MOCK RECEIPT; not SAB schema'
        client = Client(Permissions(enqueue=True), transport)
        chosen = selection()
        result = client.enqueue(chosen, "tracked-req-1", INERT_NZB, credential=FAKE_KEY)
        self.assertEqual(result, transport.send.return_value)
        args, kwargs = transport.send.call_args
        request = args[0]
        self.assertEqual((request.method, request.endpoint), ("POST", SAB))
        self.assertEqual(request.public_dict()["params"], {"mode": "addfile"})
        self.assertEqual(request.request_id, "tracked-req-1")
        self.assertIs(request.selection, chosen)
        self.assertEqual(kwargs, {"private_params": {"apikey": FAKE_KEY}, "nzb": INERT_NZB})
        with self.assertRaises(FrozenInstanceError):
            chosen.edition = "Changed edition"
        with self.assertRaises(FrozenInstanceError):
            request.request_id = "new"

    def test_invalid_enqueue_never_reaches_transport(self):
        transport = Mock()
        client = Client(Permissions(enqueue=True), transport)
        for chosen, request_id, nzb in ((None, "req", INERT_NZB),
                                       (selection(), "", INERT_NZB),
                                       (selection(), "req\n", INERT_NZB),
                                       (selection(), "req", b""),
                                       (selection(), "req", b"x" * (MAX_NZB_BYTES + 1))):
            with self.assertRaises(ContractError):
                client.enqueue(chosen, request_id, nzb)
        transport.send.assert_not_called()

    def test_no_false_idempotency_claim_caller_reuses_receipt(self):
        transport = Mock()
        transport.send.return_value = b'OWNED MOCK RECEIPT'
        client = Client(Permissions(enqueue=True), transport)
        receipt = client.enqueue(selection(), "caller-tracked-1", INERT_NZB)
        # Caller stores and returns receipt for duplicate intent; no broker here.
        caller_receipts = {"caller-tracked-1": receipt}
        self.assertEqual(caller_receipts["caller-tracked-1"], receipt)
        self.assertEqual(transport.send.call_count, 1)
        transport.send.side_effect = RuntimeError("ambiguous enqueue")
        with self.assertRaises(ContractError):
            client.enqueue(selection(), "caller-tracked-2", INERT_NZB)
        self.assertEqual(transport.send.call_count, 2)  # No automatic retry.

    def test_monitor_cancel_remain_unsupported_even_with_flags(self):
        transport = Mock()
        client = Client(Permissions(monitor=True, cancel=True), transport)
        for action in (client.monitor, client.cancel):
            with self.assertRaises(Blocked):
                action()
        transport.send.assert_not_called()
