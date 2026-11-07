from cabinet.boundary import ROOT, runtime_database, runtime_urlconf, settings_secret

SECRET_KEY = settings_secret()
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "shelf",
]
MIDDLEWARE = [
    "cabinet.middleware.LocalSecurityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = runtime_urlconf()
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": runtime_database(),
    "TEST": {"NAME": ":memory:"},
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [ROOT / "templates"],
    "APP_DIRS": False,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.template.context_processors.csrf",
    ]},
}]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "cc_synthetic_session"
if ROOT_URLCONF == "cabinet.provider_urls":
    SESSION_COOKIE_NAME = "cc_provider_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_NAME = "cc_synthetic_csrf"
if ROOT_URLCONF == "cabinet.provider_urls":
    CSRF_COOKIE_NAME = "cc_provider_csrf"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_FAILURE_VIEW = "cabinet.errors.csrf_failure"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# Loopback HTTP demo only. Cookies must work without a TLS listener.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
STATIC_URL = "/static/"
STATICFILES_DIRS = [ROOT / "static"]
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"

# Explicit local operator activation; verification/test runtimes stay inert.
if ROOT_URLCONF == 'cabinet.provider_urls':
    OBTAIN_SUBMISSION_POLICY = {'enabled': True, 'complete_root': '/home/tang/Downloads/Usenet/Complete', 'max_bytes': 2147483648, 'reserve_bytes': 2147483648, 'test_only': False, 'registry_path': '/home/tang/Projects/culture-cabinet/.acquisition-control/requests.json', 'fixed_layout': True, 'script': 'culture-cabinet-completion', 'pp': 2, 'category': '*'}
    OBTAIN_COMPLETION_RECEIPTS = '/home/tang/Projects/culture-cabinet/.acquisition-control/receipts'
