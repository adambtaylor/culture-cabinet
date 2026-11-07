"""No environment-selected settings, database, import path, or public bind."""
import os
import secrets
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_ROOT = Path("/home/tang/Projects/culture-cabinet")
STORE = ROOT / ".demo-data"
DATABASE = STORE / "synthetic.sqlite3"
KEY_FILE = STORE / "session-key"
_secret = None
_demo = False
_provider = False


class BoundaryError(Exception):
    pass


def validate_paths(*, demo=None):
    runtime = _demo if demo is None else demo
    if runtime and ROOT != DEPLOYMENT_ROOT:
        raise BoundaryError
    if any(p.is_symlink() for p in (ROOT, *ROOT.parents, STORE, DATABASE, KEY_FILE)):
        raise BoundaryError
    if STORE.resolve() != STORE or (STORE.exists() and not STORE.is_dir()):
        raise BoundaryError
    if runtime and not STORE.is_dir():
        raise BoundaryError
    for path in (DATABASE, KEY_FILE):
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise BoundaryError


def validate_environment(environ):
    # Inspect names only; values never enter settings or diagnostic output.
    forbidden = {"DATABASE_URL", "DATABASE_PATH", "SQLITE_PATH", "DB_PATH",
                 "SECRET_KEY", "DEBUG", "ALLOWED_HOSTS", "PYTHONPATH",
                 "PYTHONHOME"}
    for name in environ:
        if (name in forbidden or name.startswith(("DJANGO_", "DATABASE_", "SQLITE_",
                                                  "DB_", "CC_", "CABINET_"))):
            raise BoundaryError


def validate_command(args):
    if not args:
        raise BoundaryError
    command, *options = args
    common = {"--no-color", "--noinput", "--no-input"}
    if command == "test":
        # Serial, ephemeral tests only; no keepdb, alternate settings or runners.
        allowed = common | {"--verbosity=0", "--verbosity=1", "--verbosity=2"}
    elif command == "check":
        allowed = common
    elif command == "makemigrations":
        if "--check" not in options or "--dry-run" not in options:
            raise BoundaryError
        allowed = common | {"--check", "--dry-run"}
    elif command in {"catalogue_runserver", "catalogue_prepare"}:
        if "--provider-demo" not in options:
            raise BoundaryError
        allowed = common | {"--provider-demo"}
        if command == "catalogue_prepare":
            allowed |= {"--plan", "--check", "--preserve-intake"}
    elif command in {"demo_migrate", "demo_bootstrap", "demo_runserver"}:
        if "--synthetic-demo" not in options:
            raise BoundaryError
        allowed = common | {"--synthetic-demo"}
        if command == "demo_migrate":
            allowed |= {"--plan", "--check"}
    else:
        raise BoundaryError
    if any(option not in allowed for option in options):
        raise BoundaryError
    return command


def demo_secret():
    validate_paths(demo=True)
    # Exclusive creation, no follow, and no operator-supplied key/path.
    try:
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(64))
    fd = os.open(KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise BoundaryError
        key = handle.read(256)
    if len(key) < 64 or len(key) > 128:
        raise BoundaryError
    return key


def prepare(args, environ):
    global _secret, _demo, _provider
    validate_environment(environ)
    command = validate_command(args)
    _demo = command in {"demo_migrate", "demo_bootstrap", "demo_runserver"}
    _provider = command in {"catalogue_runserver", "catalogue_prepare"}
    validate_paths(demo=_demo or _provider)
    if _provider and not (DATABASE.is_file() and KEY_FILE.is_file()):
        # The provider runtime never initializes or repairs the old store.
        raise BoundaryError
    if _demo or _provider:
        os.umask(0o077)
        _secret = demo_secret()
    else:
        _secret = secrets.token_urlsafe(64)


def runtime_database():
    # Verification commands cannot open or initialize the disposable demo store.
    return str(DATABASE) if _demo or _provider else ":memory:"


def runtime_urlconf():
    return "cabinet.provider_urls" if _provider else "cabinet.urls"


def settings_secret():
    if _secret is None:
        # No WSGI/ASGI/settings-module bypass of the explicit entry point.
        raise BoundaryError("Use backend/manage.py with fixed synthetic settings.")
    return _secret
