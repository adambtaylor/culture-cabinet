The normal acquisition runtime uses only Python's standard library plus the existing
credential_store. No shared environment, dependency lock or installation changed.
The one-time source tool uses existing system Python 3.14, PyYAML 6.0.3 and
cryptography 50.0.2; it is not imported by the app. Decoder provenance and limitations
are retained verbatim in hydra_key_source.py and test_hydra_key_source.py; supplied
fixtures were generated offline with OpenSSL 3.6.5. No OpenSSL process is invoked.
No repository licence or upstream redistribution clearance is inferred here.
