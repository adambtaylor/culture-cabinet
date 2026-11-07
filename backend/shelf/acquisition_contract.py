"""Bounded Newznab caps/search/get and SAB addfile requests, without credentials.

The backend supplies keys privately through the fixed loopback transport. Runtime
submission still requires a trusted operator factory; request data grants nothing.
"""
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Protocol
from xml.etree import ElementTree


HYDRA = "http://127.0.0.1:5076/api"
SAB = "http://127.0.0.1:8080/api"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CAPS_ELEMENTS = 256
MAX_CATEGORIES = 128
MAX_NZB_BYTES = 1024 * 1024
CATEGORIES = {"film": 2000, "episode": 5000, "audiobook": 3030, "music": 3040}
QUERY_TYPES = {"search": "search", "tv-search": "tvsearch",
               "movie-search": "movie", "book-search": "book", "music-search": "music"}


class ContractError(ValueError):
    """Only static, credential-free messages cross this boundary."""


class Blocked(ContractError):
    pass


def _text(value, maximum=200):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise ContractError("Invalid bounded text")
    return value.strip()


def _bounded_response(value):
    if type(value) is not bytes or len(value) > MAX_RESPONSE_BYTES:
        raise ContractError("Invalid or oversized response")
    return value


@dataclass(frozen=True)
class Permissions:
    search: bool = False
    enqueue: bool = False
    monitor: bool = False
    cancel: bool = False

    def __post_init__(self):
        if any(type(v) is not bool for v in
               (self.search, self.enqueue, self.monitor, self.cancel)):
            raise ContractError("Permissions require explicit booleans")


@dataclass(frozen=True)
class Capabilities:
    query_types: frozenset[str]
    categories: tuple[tuple[int, str], ...]
    text_search: bool


def parse_caps(payload: bytes) -> Capabilities:
    """Bounded UTF-8 Newznab caps subset; reject declarations before XML parsing."""
    payload = _bounded_response(payload)
    try:
        xml = payload.decode("utf-8")
    except UnicodeError:
        raise ContractError("Caps must be UTF-8") from None
    if "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ContractError("DTD and entities are forbidden")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise ContractError("Invalid caps XML") from None
    if root.tag != "caps" or sum(1 for _ in root.iter()) > MAX_CAPS_ELEMENTS:
        raise ContractError("Invalid or oversized caps tree")
    types = set()
    text_search = False
    for node in root.findall("./searching/*"):
        if node.tag in QUERY_TYPES and node.get("available", "").lower() == "yes":
            types.add(QUERY_TYPES[node.tag])
            if node.tag == "search":
                text_search = "q" in node.get("supportedParams", "").split(",")
    categories = []
    for node in root.findall("./categories//category") + root.findall("./categories//subcat"):
        raw_id = node.get("id", "")
        if not re.fullmatch(r"[0-9]{1,6}", raw_id):
            raise ContractError("Invalid caps category")
        category = (int(raw_id), _text(node.get("name"), 100))
        if category[0] <= 0 or any(c[0] == category[0] for c in categories):
            raise ContractError("Invalid or duplicate caps category")
        categories.append(category)
    if len(categories) > MAX_CATEGORIES:
        raise ContractError("Too many caps categories")
    return Capabilities(frozenset(types), tuple(categories), text_search)


@dataclass(frozen=True)
class ConfirmedSelection:
    """Authorization to obtain the selected release, not evidence of its contents.

    The legacy verification fields remain readable in durable receipts, but are
    never prerequisites or proof of postdownload identity/completeness. Exact
    release/scope binding, rights, storage and duplicate checks still gate sends.
    """
    item_id: str
    media: str
    edition: str
    storage: str
    authorized_content: bool = False
    user_confirmed: bool = False
    edition_verified: bool = False
    complete_work_verified: bool = False
    storage_checked: bool = False
    duplicate_checked: bool = False
    episode: str | None = None

    def __post_init__(self):
        for value in (self.item_id, self.edition, self.storage):
            _text(value)
        if self.media not in CATEGORIES:
            raise ContractError("Unsupported media")
        if self.media == "episode":
            from shelf.episode_scope import ScopeError, episode_code
            try:
                valid = (type(self.episode) is str and
                         episode_code('episode_' + self.episode.lower()) == self.episode)
            except ScopeError:
                valid = False
            if not valid:
                raise ContractError("Exact canonical selected episode required")
        elif self.episode is not None:
            raise ContractError("Episode selector requires episode media")
        if any(value is not True for value in (
                self.authorized_content, self.user_confirmed,
                self.storage_checked, self.duplicate_checked)):
            raise Blocked("Confirmed selection, rights, storage and duplicate checks required")
        if any(type(value) is not bool for value in
               (self.edition_verified, self.complete_work_verified)):
            raise ContractError("Invalid legacy verification flags")


COMPLETION_SCRIPT = 'culture-cabinet-completion'


@dataclass(frozen=True)
class SABJobOptions:
    """The single supported operator-approved layout and postprocessing contract."""
    script: str = COMPLETION_SCRIPT
    pp: int = 2
    category: str = '*'

    def __post_init__(self):
        if (self.script != COMPLETION_SCRIPT or type(self.pp) is not int
                or self.pp != 2 or self.category != '*'):
            raise ContractError('Unsupported SAB job policy')


def sab_name(request_id):
    if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
        raise ContractError('Invalid request identity')
    return 'CC-' + request_id


@dataclass(frozen=True)
class PublicRequest:
    """Public metadata only. No credential or secret-bearing URL field exists."""
    operation: str
    params: tuple[tuple[str, str], ...] = ()
    selection: ConfirmedSelection | None = None
    request_id: str | None = None
    job_options: SABJobOptions | None = None

    def __post_init__(self):
        if self.operation == "caps":
            valid = self.params == () and self.selection is None and self.request_id is None
        elif self.operation == "search":
            valid = (type(self.params) is tuple and len(self.params) == 2
                     and all(type(p) is tuple and len(p) == 2 for p in self.params)
                     and self.params[0][0] == "q" and self.params[1][0] == "cat"
                     and self.params[1][1] in {str(c) for c in CATEGORIES.values()}
                     and self.selection is None and self.request_id is None)
            if valid:
                _text(self.params[0][1])
        elif self.operation == "addfile":
            valid = (self.params == () and type(self.selection) is ConfirmedSelection
                     and type(self.request_id) is str
                     and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.request_id))
        elif self.operation == "get":
            valid = (type(self.params) is tuple and len(self.params) == 1
                     and type(self.params[0]) is tuple and len(self.params[0]) == 2
                     and self.params[0][0] == 'id' and type(self.params[0][1]) is str
                     and re.fullmatch(r'-?(?:0|[1-9][0-9]{0,18})', self.params[0][1])
                     and -(2**63) <= int(self.params[0][1]) < 2**63
                     and str(int(self.params[0][1])) == self.params[0][1]
                     and self.selection is None and self.request_id is None)
        else:
            valid = False
        if self.job_options is not None:
            if self.operation != 'addfile' or type(self.job_options) is not SABJobOptions:
                raise ContractError('Unsupported SAB job policy')
            SABJobOptions(**vars(self.job_options))
        if not valid:
            raise ContractError("Unsupported public request")

    @property
    def endpoint(self):
        return SAB if self.operation == "addfile" else HYDRA

    @property
    def method(self):
        return "POST" if self.operation == "addfile" else "GET"

    def public_dict(self):
        params = {"mode": "addfile"} if self.operation == "addfile" else {
            "t": self.operation, **dict(self.params)}
        if self.operation == "search":
            params["o"] = "json"
        return {"endpoint": self.endpoint, "method": self.method, "params": params,
                "selection": self.selection, "request_id": self.request_id}


def search_request(caps: Capabilities, query: str, media: str) -> PublicRequest:
    if media not in CATEGORIES:
        raise ContractError("Unsupported media")
    category = CATEGORIES[media]
    if ("search" not in caps.query_types or not caps.text_search
            or category not in {c[0] for c in caps.categories}):
        raise Blocked("Advertised text search and category required")
    # NZBIndex is TEXTSEARCH: never invent IDs or assume advanced search support.
    return PublicRequest("search", (("q", _text(query)), ("cat", str(category))))


class Transport(Protocol):
    def send(self, request: PublicRequest, *, private_params: dict[str, str],
             nzb: bytes | None = None) -> bytes:
        """Adapter adds apikey privately; addfile is multipart name/nzbfile.

        No redirect, arbitrary URI, key header assumption, logging or retry on an
        ambiguous enqueue outcome.
        """
        ...


class NoFetch:
    def send(self, request, *, private_params, nzb=None):
        raise Blocked("Acquisition transport disabled")


@dataclass(frozen=True)
class Client:
    permissions: Permissions = field(default_factory=Permissions)
    transport: Transport = field(default_factory=NoFetch, repr=False, compare=False)

    def _send(self, request, *, credential, nzb=None):
        # Credential is caller-supplied transient input, never public request state.
        if credential is not None and (type(credential) is not str or not credential):
            raise ContractError("Invalid private credential")
        try:
            response = self.transport.send(request, private_params=(
                {} if credential is None else {"apikey": credential}), nzb=nzb)
        except Blocked:
            raise Blocked("Acquisition transport disabled") from None
        except Exception:
            raise ContractError("Acquisition transport failed; outcome unknown") from None
        if request.operation == 'get':
            if type(response) is not bytes or not 0 < len(response) <= MAX_NZB_BYTES:
                raise ContractError('Invalid or oversized NZB response')
            return response
        return _bounded_response(response)

    def caps(self, *, credential=None):
        if not self.permissions.search:
            raise Blocked("Search permission required")
        return parse_caps(self._send(PublicRequest("caps"), credential=credential))

    def search(self, caps, query, media, *, credential=None):
        if not self.permissions.search:
            raise Blocked("Search permission required")
        return self._send(search_request(caps, query, media), credential=credential)

    def enqueue(self, selection, request_id, nzb, *, credential=None, job_options=None):
        """User-triggered backend call only; caller owns durable idempotency.

        No broker here: reuse the caller's existing receipt without invoking this
        method again. An ambiguous response must not be automatically retried.
        No GET/discovery/heart handler or feedback writer is installed.
        """
        if not self.permissions.enqueue:
            raise Blocked("Separate enqueue permission required")
        request = PublicRequest("addfile", selection=selection, request_id=request_id,
                                job_options=job_options)
        if type(nzb) is not bytes or not 0 < len(nzb) <= MAX_NZB_BYTES:
            raise ContractError("Invalid or oversized NZB payload")
        return self._send(request, credential=credential, nzb=nzb)

    def retrieve(self, guid, *, credential=None):
        if not self.permissions.search:
            raise Blocked('Hydra permission required')
        return self._send(PublicRequest('get', (('id', guid),)), credential=credential)

    def monitor(self):
        raise Blocked("Monitoring unsupported; separate full API grant required")

    def cancel(self):
        raise Blocked("Cancellation unsupported; separate full API grant required")
