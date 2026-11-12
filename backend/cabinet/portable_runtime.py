"""Selected-root bootstrap and socket custody for the separate portable preview.

No existing settings, household identity or demo runtime is imported. Only a new
store is migrated; an existing incompatible/partial store needs an explicit
future upgrade/recovery procedure, never a guessed cleanup or automatic adoption.
"""
from contextlib import closing, ExitStack
import logging
import os
from pathlib import Path
import resource
import secrets
import signal
import sqlite3
import stat
import sys


class PortableStop(BaseException):
    """Process cancellation, not an application error or retryable provider fault.

    Bypass Exception-based request recovery. Transport/socket finalizers still
    run; a committed sending reservation remains unknown/blocked on restart.
    """


class RuntimeFailure(Exception):
    """A safe fixed diagnostic, without filesystem or transient input details."""


class PortableAlreadyRunning(Exception):
    """Local handoff for the fixed duplicate-installation status."""


_FRESH_DATABASES = {}


def _consume_native_first_start(boundary):
    """Only this process's exclusive creation can authorize first provisioning."""
    proof = _FRESH_DATABASES.pop(id(boundary), None)
    if proof is None or proof[0] is not boundary or not boundary.active:
        raise RuntimeFailure('PORTABLE_RECOVERY_CONTINUITY_REQUIRED')
    root, database = boundary.root.stat(), Path(boundary.database_name).stat()
    if proof[1:] != ((root.st_dev, root.st_ino), (database.st_dev, database.st_ino)):
        raise RuntimeFailure('PORTABLE_RECOVERY_CONTINUITY_REQUIRED')
    return proof[1:]


def protect_core():
    # Authority imports models, so its accepted guard cannot run until setup().
    # Bootstrap must protect the earlier Django/session-secret creation as well.
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        if resource.getrlimit(resource.RLIMIT_CORE)[0] != 0:
            raise OSError
    except (OSError, ValueError):
        raise RuntimeFailure('PORTABLE_SECRET_PROTECTION_REQUIRED') from None


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeFailure('PORTABLE_STORE_REFUSED')


def prepare_database(boundary):
    """Exclusivity proves freshness; existence never means permission to migrate."""
    path = Path(boundary.database_name)
    if not path.exists() and not path.is_symlink():
        if {p.name for p in boundary.root.iterdir()} != {'.portable-lock', '.portable-installation.json'}:
            raise RuntimeFailure('PORTABLE_STORE_REFUSED')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            root_info, database_info = boundary.root.stat(), os.fstat(fd)
            _FRESH_DATABASES[id(boundary)] = (boundary,
                (root_info.st_dev, root_info.st_ino), (database_info.st_dev, database_info.st_ino))
        finally:
            os.close(fd)
        return True
    private_file(path)
    for suffix in ['-wal', '-shm', '-journal']:
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            private_file(sidecar)
    return False


def configure(boundary):
    from django.conf import settings
    root = Path(__file__).resolve().parents[2]
    if settings.configured:
        raise RuntimeFailure('PORTABLE_SETTINGS_REFUSED')
    settings.configure(
        SECRET_KEY=secrets.token_urlsafe(48), DEBUG=False,
        ALLOWED_HOSTS=['127.0.0.1'], ROOT_URLCONF='cabinet.portable_urls',
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes',
                        'django.contrib.sessions', 'django.contrib.staticfiles', 'shelf'],
        MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                    'django.contrib.sessions.middleware.SessionMiddleware',
                    'django.middleware.csrf.CsrfViewMiddleware'],
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': boundary.database_name}},
        DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
        TEMPLATES=[{'BACKEND': 'django.template.backends.django.DjangoTemplates',
                    'DIRS': [root / 'templates'], 'APP_DIRS': False,
                    'OPTIONS': {'context_processors': []}}],
        SESSION_ENGINE='django.contrib.sessions.backends.db',
        SESSION_COOKIE_NAME=boundary.session_cookie, CSRF_COOKIE_NAME=boundary.csrf_cookie,
        SESSION_COOKIE_HTTPONLY=True, CSRF_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict', CSRF_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
        SESSION_COOKIE_DOMAIN=None, CSRF_COOKIE_DOMAIN=None,
        SESSION_COOKIE_AGE=1800, SESSION_EXPIRE_AT_BROWSER_CLOSE=True,
        AUTH_PASSWORD_VALIDATORS=[{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
                                    'OPTIONS': {'min_length': 12}},
                                  {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
                                  {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}],
        DATA_UPLOAD_MAX_MEMORY_SIZE=16384, DATA_UPLOAD_MAX_NUMBER_FIELDS=16,
        CSRF_FAILURE_VIEW='cabinet.errors.csrf_failure',
        STATIC_URL='/static/', STATICFILES_DIRS=[root / 'static'],
        USE_TZ=True, TIME_ZONE='UTC', LANGUAGE_CODE='en', LOGGING_CONFIG=None,
    )
    import django
    django.setup()


def validate_existing(boundary):
    """Read the live SQLite view, including committed WAL, before ORM writes.

    immutable=1 would ignore WAL and can mistake an old checkpoint for current
    ownership/schema. Migration records alone are insufficient: also check the
    concrete columns, integrity, root binding and singleton installation.
    """
    from django.apps import apps
    from django.db.migrations.loader import MigrationLoader
    expected = set(MigrationLoader(None).disk_migrations)
    try:
        with closing(sqlite3.connect(Path(boundary.database_name).as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError
            if set(db.execute('SELECT app,name FROM django_migrations')) != expected:
                raise ValueError
            for model in apps.get_models(include_auto_created=True):
                table = model._meta.db_table.replace('"', '""')
                columns = {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}
                if columns != {f.column for f in model._meta.local_fields}:
                    raise ValueError
            rows = db.execute('SELECT id,root_identity,data_schema_version,owner_id FROM shelf_portableinstallation').fetchall()
            if len(rows) != 1 or rows[0][:3] != (boundary.installation_id.hex, boundary.root_identity, 1):
                raise ValueError
            if rows[0][3] is None and (db.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM shelf_profile').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM shelf_grant').fetchone()[0]):
                raise ValueError
    except (sqlite3.Error, ValueError):
        raise RuntimeFailure('PORTABLE_STORE_UPGRADE_OR_RECOVERY_REQUIRED') from None


def remove_fresh_fixtures():
    """Delete exact historical demo rows only behind the exclusive fresh-store gate.

    Never call on restart. Verify every shelf table first: deleting known IDs in
    an arbitrary store could remove real references or conceal foreign content.
    RecoveryAnchor is installation infrastructure and must remain intact.
    """
    from django.apps import apps
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from shelf.models import FeedbackTarget, RecoveryAnchor
    expected = {
        ('00000000000040008000000000000101', 'paper-observatory', 'The Paper Observatory', 'Fictional film'),
        ('00000000000040008000000000000102', 'mosslight-station', 'Mosslight Station', 'Fictional book'),
        ('00000000000040008000000000000103', 'clockwork-lagoon', 'The Clockwork Lagoon', 'Fictional series'),
    }
    with transaction.atomic():
        actual = {(row.pk.hex, row.source_key, row.title, row.category) for row in FeedbackTarget.objects.all()
                  if row.source_namespace == 'fiction-p1101' and row.kind == 'item'
                  and row.parent_id is None and not row.deleted}
        if actual != expected or FeedbackTarget.objects.count() != 3 or get_user_model().objects.exists():
            raise RuntimeFailure('PORTABLE_FRESH_STORE_REFUSED')
        if RecoveryAnchor.objects.count() != 1 or not RecoveryAnchor.objects.filter(pk=1).exists():
            raise RuntimeFailure('PORTABLE_FRESH_STORE_REFUSED')
        for model in apps.get_app_config('shelf').get_models():
            if model not in {FeedbackTarget, RecoveryAnchor} and model.objects.exists():
                raise RuntimeFailure('PORTABLE_FRESH_STORE_REFUSED')
        FeedbackTarget.objects.all().delete()


def make_server(boundary, application):
    from wsgiref.simple_server import WSGIServer, WSGIRequestHandler

    class QuietHandler(WSGIRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *args):
            pass

        def get_stderr(self):
            # wsgiref's exception path bypasses Django logging. Discard diagnostic
            # frames here too: transient form/provider values must not hit a log.
            return self.server.error_stream

    server = WSGIServer(('127.0.0.1', 0), QuietHandler, bind_and_activate=False)
    try:
        server.socket.close()
        # Transfer the *same* bound socket; a second bind could change port/authority.
        # BoundRoot also closes it on all failures, so double-close must be harmless.
        server.socket = boundary._socket
        server.server_address = server.socket.getsockname()
        server.server_name, server.server_port = '127.0.0.1', server.server_address[1]
        server.setup_environ()  # Avoid server_bind()/getfqdn() and its resolver call.
        server.error_stream = open(os.devnull, 'w')
        server.set_app(application)
        server.server_activate()
        server.timeout = 0.2
        return server
    except BaseException:
        # Factory owns resources until return, including interrupted startup.
        try:
            server.server_close()
        finally:
            stream = getattr(server, 'error_stream', None)
            if stream is not None:
                stream.close()
        raise


def _compose_enhance(authority, connections, *, approved_policies=None, approved_policies_factory=None):
    from shelf.native_source_policy import build_native_source_policy
    from shelf.portable_enhance_service import EnhanceService
    if (approved_policies is not None and approved_policies_factory is not None
            or approved_policies_factory is not None and not callable(approved_policies_factory)):
        raise RuntimeFailure('PORTABLE_SOURCE_POLICY_REFUSED')
    # Trusted host code can import model-dependent rights only after Django
    # setup. Consume it once here; requests never retain a factory/loader hook.
    entries = approved_policies_factory() if approved_policies_factory is not None else (
        {} if approved_policies is None else approved_policies)
    if type(entries) is not dict:
        raise RuntimeFailure('PORTABLE_SOURCE_POLICY_REFUSED')
    policy = build_native_source_policy(authority, approved_policies=entries)
    for rights in entries.values():
        rights.validate(authority.now())
        if rights.source in ('musicbrainz_recording', 'musicbrainz_artist'):
            from shelf.musicbrainz_source import MusicBrainzOperation
            MusicBrainzOperation('Contact validation', rights.application_identity, source=rights.source)
    EnhanceService.compose(authority, connections, policy_provider=policy)
    if not entries:
        print('PORTABLE_SOURCE_POLICY_UNAVAILABLE', flush=True)
    return policy


def run(root, *, port=0, approved_policies=None, approved_policies_factory=None, saved_store=None):
    os.umask(0o077)
    boundary = server = None
    lifetime = ExitStack()
    previous = {}
    stopping = False
    closing = False
    checkpoint = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if not closing:
            # A per-read inactivity timeout cannot stop a steadily dripping
            # request. Unwind the active handler immediately into owned cleanup.
            raise PortableStop()

    try:
        if (approved_policies is not None and approved_policies_factory is not None
                or approved_policies_factory is not None and not callable(approved_policies_factory)):
            raise RuntimeFailure('PORTABLE_SOURCE_POLICY_REFUSED')
        protect_core()
        # This process is a dedicated local server. Disabling logging avoids
        # accidental exception-value disclosure; terminal output is fixed below.
        logging.disable(logging.CRITICAL)
        for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
            previous[sig] = signal.signal(sig, stop)
        from cabinet.portable_boundary import BoundaryAlreadyRunning, open_boundary
        try:
            boundary = open_boundary(root, port=port)
        except BoundaryAlreadyRunning:
            raise PortableAlreadyRunning() from None
        fresh = prepare_database(boundary)
        configure(boundary)
        if fresh:
            from django.core.management import call_command
            call_command('migrate', interactive=False, verbosity=0)
            remove_fresh_fixtures()
        else:
            validate_existing(boundary)
        from django.conf import settings
        from django.core.wsgi import get_wsgi_application
        from shelf.portable_authority import PortableAuthority
        from shelf.secret_leases import MemoryCredentials
        from shelf.connection_validation import ConnectionValidation
        from shelf.recovery import NativeCleanCheckpoint, native_first_start, native_clean_restart
        from shelf.saved_credentials import SavedCredentials, secret_service_request
        store_request = secret_service_request if saved_store is None else saved_store
        checkpoint = NativeCleanCheckpoint(boundary, store_request)
        if fresh:
            authority = PortableAuthority(boundary)
            lifetime.enter_context(native_first_start(boundary))
        else:
            # Consume the independent CLEAN anchor before PortableAuthority can
            # update installation generation, sessions or any other SQL row.
            lifetime.enter_context(native_clean_restart(boundary, checkpoint))
            authority = PortableAuthority(boundary)
        lifetime.callback(checkpoint.seal)
        from shelf.obtain_runtime import selected_supervisor
        # Close selected children/monitors before the original recovery witness.
        # Effects stay on this serving thread, outside every request's scopes.
        supervisor = lifetime.enter_context(selected_supervisor(authority))
        checkpoint.writers_stopped = lambda: supervisor.closed and not supervisor.failed and not authority._obtain_workers
        settings.PORTABLE_AUTHORITY = authority
        settings.PORTABLE_CONNECTIONS = ConnectionValidation(authority, MemoryCredentials(authority))
        settings.PORTABLE_SAVED_CREDENTIALS = SavedCredentials(authority,
            settings.PORTABLE_CONNECTIONS.vault, request=store_request, before_save=checkpoint.enroll)
        from shelf.portable_disclosure import DisclosureServices
        # Inference grants use the same validation instance registered here;
        # ordinary construction also closes its previews with the boundary.
        DisclosureServices(authority, settings.PORTABLE_CONNECTIONS)
        # Only trusted host construction supplies reviewed entries; CLI defaults
        # deny. Credentials and HTTP input cannot manufacture source permissions.
        settings.PORTABLE_SOURCE_POLICY = _compose_enhance(authority,
            settings.PORTABLE_CONNECTIONS, approved_policies=approved_policies,
            approved_policies_factory=approved_policies_factory)
        server = make_server(boundary, get_wsgi_application())
        if not stopping:
            print('Origin: ' + boundary.origin, flush=True)
            if authority._installation().owner_id is None:
                print('Claim code (terminal only, expires in 5 minutes):', flush=True)
                authority.issue_challenge(sys.stdout)
            print('PORTABLE_READY', flush=True)
        # shutdown() from this serving thread deadlocks. The signal cancellation
        # interrupts active reads/work; timeout only bounds the idle accept wait.
        # WSGI may consume a BaseException after closing a failed response, so the
        # flag also prevents another request if its handler returns normally.
        while not stopping:
            server.handle_request()
            if not stopping:
                # A bounded synchronous pass may delay the next request. Return
                # to serving between passes, without a polling/draining loop.
                supervisor.tick_once()
        checkpoint.clean_stop = True
        return 0
    except PortableStop:
        if checkpoint is not None:
            checkpoint.clean_stop = True
        return 0
    except PortableAlreadyRunning:
        print('PORTABLE_ALREADY_RUNNING', file=sys.stderr, flush=True)
        return 1
    except Exception:
        print('PORTABLE_START_OR_RUNTIME_REFUSED', file=sys.stderr, flush=True)
        return 1
    finally:
        closing = True
        try:
            try:
                if server is not None:
                    server.server_close()
                lifetime.close()
            finally:
                if boundary is not None:
                    _FRESH_DATABASES.pop(id(boundary), None)
                    boundary.close()
        finally:
            if server is not None:
                server.server_close()
                server.error_stream.close()
            from django.conf import settings
            if settings.configured:
                from django.db import connections
                connections.close_all()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def trusted_synthetic_restart(boundary, *, confirmed, fresh=False):
    """Explicit disposable-store entry; normal run() never infers owner acceptance.

    The recovery factory enforces the existing verification environment and
    private temporary root. Real activation remains a separate owner trust gate.
    """
    from shelf.recovery import trusted_restart_fixture
    return trusted_restart_fixture(boundary, confirmed=confirmed, fresh=fresh)
