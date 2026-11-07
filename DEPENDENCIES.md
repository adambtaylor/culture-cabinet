# Selected dependencies

Private synthetic P1.1-01 setup; original application code is not yet assigned a publication licence.

| Component | Version | Licence | Artifact SHA-256 |
|---|---|---|---|
| asgiref | 3.13.0 | BSD-3-Clause | `f9596a2434512686473d50f96ff6207fbba49ae1a785dfac614df4aeb085674d` |
| Django | 5.2.18 | BSD-3-Clause | `92ed81d500be6408ecd704d7bd1366c534f30427bffcc63c5fefb129561aec7c` |
| sqlparse | 0.6.0 | BSD-3-Clause, inspected LICENSE | `b861c0288ce2fa56209a9a6412d2e066ac664b3873b89c26c9d8415e8e32996f` |

Artifacts downloaded from official PyPI and each SHA-256 matched its official release metadata. Complete pins/hashes are in requirements.lock. Adopted wheels are unchanged. Full licence texts, including Django’s bundled Python licence, are retained in THIRD_PARTY_NOTICES.txt and in the original wheel artifacts. Preserve notices when redistributing; no publication is authorized.

Upstream records:

* asgiref: Documentation, https://asgi.readthedocs.io/; Further Documentation, https://docs.djangoproject.com/en/stable/topics/async/#async-adapter-functions; Changelog, https://github.com/django/asgiref/blob/master/CHANGELOG.txt

* Django: Homepage, https://www.djangoproject.com/; Documentation, https://docs.djangoproject.com/; Release notes, https://docs.djangoproject.com/en/stable/releases/; Funding, https://www.djangoproject.com/fundraising/; Source, https://github.com/django/django; Tracker, https://code.djangoproject.com/

* sqlparse: Home, https://github.com/andialbrecht/sqlparse; Documentation, https://sqlparse.readthedocs.io/; Release Notes, https://sqlparse.readthedocs.io/en/latest/changes.html; Source, https://github.com/andialbrecht/sqlparse; Tracker, https://github.com/andialbrecht/sqlparse/issues

Runtime: guest Python 3.14.8, linked SQLite 3.54.0; Django 5.2 officially supports Python 3.14 starting in 5.2.8. Runtime compatibility was checked by importing the installed Django wheel.

Actual guest application tests remain separately recorded. This inventory does not claim a vulnerability audit, public licensing clearance for original code, or household deployment.
