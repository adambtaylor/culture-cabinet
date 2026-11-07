# Accepted-client inactivity bound

The fixed single-threaded loopback catalogue server gives each accepted client socket a two-second I/O inactivity timeout through Django's supported `Command.server_cls` and `WSGIServer.get_request()` hooks. An idle preconnection or incomplete request therefore cannot monopolize the sole HTTP request loop indefinitely. The listening socket and global socket defaults are unchanged; no threaded HTTP server, reloader or network exposure is enabled.

This is an inactivity bound on socket reads/writes, not a two-second deadline for application processing or external provider operations. Normal login processing, CSRF, authentication, DB-backed sessions, cookies and the session secret retain their existing implementation. A connection that sends no further data for the bound may close and be retried by the client. It does not reset a user account or clear its persisted session.

Tests use real Django WSGI handlers on ephemeral loopback ports: the unmodified server demonstrates the idle-client block, while the bounded subclass releases a queued login request; incomplete request-line and POST-body cases also recover. Test-only handlers and session fixtures perform no production login. The live acceptance opens one idle connection through the existing local forward and verifies a separate login form response within the bound, without submitting credentials.

Deployment remains source-only with unchanged database/schema, keys, grants, loopback bind and November 6 runtime expiry. The internal weekly pending-review clock and zero automatic model/provider-call caps are unchanged.
