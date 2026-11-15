"""Dormant portable server boundary. No launcher or listener is activated here.

The future launcher must use this root's database and retain this object for the
whole process. Tests explicitly substitute their isolated Django database. The
marker identifies a root; it never authenticates an owner or authorizes disclosure.
"""
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import uuid

from django.conf import settings
from django.http import JsonResponse


class BoundaryFailure(Exception):
    def __init__(self):
        super().__init__('CAPABILITY_UNAVAILABLE')


@dataclass(repr=False)
class BoundRoot:
    installation_id: uuid.UUID
    root: Path
    root_identity: str
    process_generation: uuid.UUID
    origin: str
    _lock: int = field(repr=False)
    _socket: socket.socket = field(repr=False)
    active: bool = True
    _closing: list = field(default_factory=list, repr=False)

    @property
    def database_name(self):
        return str(self.root / 'portable.sqlite3')

    @property
    def namespace(self):
        return 'cc_' + self.installation_id.hex + '_' + self.process_generation.hex

    @property
    def session_cookie(self):
        return self.namespace + '_session'

    @property
    def csrf_cookie(self):
        return self.namespace + '_csrf'

    def close(self):
        if self.active:
            self.active = False
            try:
                for callback in self._closing:
                    callback()
            finally:
                self._socket.close()
                os.close(self._lock)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise BoundaryFailure()


def open_boundary(root, *, port=0):
    """Acquire a live OS lock and bind, but never listen or start a service."""
    root = Path(root).absolute()
    lock = None
    sock = None
    transferred = False
    try:
        if type(port) is not int or not 0 <= port <= 65535:
            raise BoundaryFailure()
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise BoundaryFailure()
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.stat()
        if not root.is_dir() or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BoundaryFailure()
        lock = os.open(root / '.portable-lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        _private_file(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = root / '.portable-installation.json'
        if not marker.exists():
            if {p.name for p in root.iterdir()} != {'.portable-lock'}:
                raise BoundaryFailure()
            installation_id = uuid.uuid4()
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'schema': 1, 'installation_id': str(installation_id)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            _private_file(stream.fileno())
            value = json.loads(stream.read(257))
        if (type(value) is not dict or set(value) != {'schema', 'installation_id'}
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['installation_id']) is not str or len(value['installation_id']) != 36):
            raise BoundaryFailure()
        installation_id = uuid.UUID(value['installation_id'])
        if str(installation_id) != value['installation_id']:
            raise BoundaryFailure()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', port))
        origin = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        identity = hashlib.sha256(f'{info.st_dev}:{info.st_ino}'.encode()).hexdigest()
        boundary = BoundRoot(installation_id, root, identity, uuid.uuid4(), origin, lock, sock)
        transferred = True
        return boundary
    except (OSError, ValueError, TypeError, BoundaryFailure):
        raise BoundaryFailure() from None
    finally:
        # Every unsuccessful construction releases resources, including unexpected
        # parser/constructor errors. Only the returned BoundRoot takes ownership.
        if not transferred:
            try:
                if sock is not None:
                    sock.close()
            finally:
                if lock is not None:
                    os.close(lock)


class PortableSecurityMiddleware:
    """Only the separately selected server fixture/launcher supplies authority."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        authority = getattr(settings, 'PORTABLE_AUTHORITY', None)
        boundary = getattr(authority, 'boundary', None)
        valid = (boundary is not None and boundary.active
                 and settings.ROOT_URLCONF == 'cabinet.portable_urls'
                 and settings.SESSION_COOKIE_NAME == boundary.session_cookie
                 and settings.CSRF_COOKIE_NAME == boundary.csrf_cookie
                 and settings.SESSION_COOKIE_DOMAIN is None and settings.CSRF_COOKIE_DOMAIN is None
                 and settings.SESSION_COOKIE_HTTPONLY and settings.CSRF_COOKIE_HTTPONLY
                 and settings.SESSION_COOKIE_SAMESITE == settings.CSRF_COOKIE_SAMESITE == 'Strict'
                 and not settings.SESSION_COOKIE_SECURE and not settings.CSRF_COOKIE_SECURE)
        if not valid:
            response = JsonResponse({'ok': False, 'code': 'CAPABILITY_UNAVAILABLE'}, status=503)
        elif (request.META.get('HTTP_HOST') != boundary.origin.removeprefix('http://')
              or any(k == 'HTTP_FORWARDED' or k.startswith('HTTP_X_FORWARDED_')
                     or k == 'HTTP_X_REAL_IP' for k in request.META)
              or request.META.get('HTTP_SEC_FETCH_SITE') not in {None, 'same-origin', 'none'}
              or (request.method not in {'GET', 'HEAD', 'OPTIONS'}
                  and request.META.get('HTTP_ORIGIN') != boundary.origin)):
            response = JsonResponse({'ok': False, 'code': 'AUTHORITY_DENIED'}, status=403)
        else:
            request.portable_authority = authority
            response = self.get_response(request)
        response['Cache-Control'] = 'no-store'
        response['X-Frame-Options'] = 'DENY'
        response['Content-Security-Policy'] = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        if valid and request.method in {'GET', 'HEAD'} and response.status_code == 200:
            from shelf import enhancement_views, portable_obtain_views
            match = getattr(request, 'resolver_match', None)
            documents = {'portable_cabinet': enhancement_views.portable_cabinet,
                         'portable_obtain_job': portable_obtain_views.job_page}
            # A path prefix, response flag or HTML-looking denial cannot enable
            # scripts. Only these exact resolved native document handlers can.
            if (getattr(match, 'url_name', None) in documents
                    and match.func is documents[match.url_name]
                    and response.get('Content-Type', '').split(';')[0].strip().lower() == 'text/html'):
                scripts = f'{boundary.origin}/cabinet/obtain/status.js'
                connections = f'{boundary.origin}/cabinet/obtain/jobs/'
                if match.func is enhancement_views.portable_cabinet:
                    scripts += f' {boundary.origin}/cabinet/actions/script.js'
                    # Only this document can enhance the existing POST/303 flow.
                    # CSP paths are not method or redirect authorization; native
                    # action guards and the fixed server destination still apply.
                    connections += f' {boundary.origin}/cabinet/actions/ {boundary.origin}/cabinet'
                response['Content-Security-Policy'] += (
                    f"; script-src {scripts}; script-src-attr 'none'"
                    f"; connect-src {connections}; worker-src 'none'")
        if valid and request.method in {'GET','HEAD'} and response.status_code==200:
            from shelf import portable_journey_views
            match=getattr(request,'resolver_match',None)
            if (getattr(match,'url_name',None)=='portable_journey' and match.func is portable_journey_views.page
                and response.get('Content-Type','').split(';')[0].strip().lower()=='text/html'):
                # Only the packaged deferred guide asset; no source/model or
                # Obtain connect permission follows from this document handler.
                response['Content-Security-Policy']+=(
                    f"; script-src {boundary.origin}/cabinet/actions/script.js; script-src-attr 'none'")
        return response
