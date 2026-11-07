"""Explicit manual credential transport check; no account or source authority."""
import json
import os
import resource
import subprocess

CURL = '/usr/bin/curl'
CA = '/home/tang/Projects/culture-cabinet/.dependencies/provider-ca/certifi-2026.10.19.pem'
MAX_BODY = 256 * 1024
_ENDPOINTS = {
    'hardcover': 'https://api.hardcover.app/v1/graphql',
    'tmdb': 'https://api.themoviedb.org/3/authentication',
}
_QUERY = '{"query":"{ books(limit:1) { id } }"}'


def _command(provider):
    command = [CURL, '-q', '--silent', '--proto', '=https', '--proto-redir', '=https',
               '--cacert', CA, '--proxy', '', '--noproxy', '*', '--no-netrc',
               '--max-redirs', '0', '--retry', '0', '--connect-timeout', '10',
               '--max-time', '10', '--max-filesize', str(MAX_BODY),
               '--header', '@-', '--header', 'Accept: application/json',
               '--write-out', '\n%{http_code}']
    if provider == 'hardcover':
        command += ['--request', 'POST', '--header', 'Content-Type: application/json',
                    '--data-binary', _QUERY]
    else:
        command += ['--request', 'GET']
    return command + ['--url', _ENDPOINTS[provider]]


def _response(provider, returncode, output):
    if returncode:
        if returncode == 28:
            return 'timeout', 0, 0
        if returncode in (35, 51, 58, 60, 77, 80, 82, 83, 90, 91):
            return 'tls', 0, 0
        return 'bad_response', 0, 0
    if len(output) > MAX_BODY + 4:
        return 'bad_response', 0, 0
    body, separator, code = output.rpartition(b'\n')
    if not separator or len(code) != 3 or not code.isdigit():
        return 'bad_response', 0, 0
    code = int(code)
    if code in (401, 403):
        return 'auth', code, 0
    if code == 429:
        return 'rate', code, 0
    if code != 200 or len(body) > MAX_BODY:
        return 'bad_response', code, 0
    try:
        value = json.loads(body)
        if type(value) is not dict:
            return 'bad_response', code, 0
        if provider == 'tmdb':
            if value.get('success') is True:
                return 'ok', code, 1
        elif 'errors' not in value:
            data = value.get('data')
            books = data.get('books') if type(data) is dict else None
            if (type(books) is list and len(books) <= 1
                    and all(type(book) is dict and type(book.get('id')) in (int, str) for book in books)):
                return 'ok', code, len(books)
    except (ValueError, UnicodeError, RecursionError):
        pass
    return 'bad_response', code, 0


def _send(provider, secret):
    header = bytearray(b'Authorization: Bearer ')
    header.extend(secret)
    header.extend(b'\n')
    try:
        # Only stdin carries the token. curl bounds transfer output; stderr is
        # discarded even on failure because it is not a diagnostic authority.
        child = subprocess.Popen(_command(provider), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env={'LANG': 'C', 'LC_ALL': 'C'},
                                 close_fds=True)
        try:
            try:
                output, _ = child.communicate(input=header, timeout=11)
            except subprocess.TimeoutExpired:
                return 'timeout', 0, 0
            return _response(provider, child.returncode, output)
        finally:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=1)
            finally:
                child.stdin.close()
                child.stdout.close()
    finally:
        header[:] = b'\0' * len(header)


def run_live(module, provider):
    store = record = None
    result = ('metadata', 0, 0)
    if provider not in _ENDPOINTS:
        return 2
    try:
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (1000, 1000, 1000, 1000):
            raise module.Refused('invalid_input')
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        context = module._provider_context(provider)
        store = module.open_store(provider=provider)
        if store.context != context or (store.uid, store.gid) != (1000, 1000):
            raise module.Refused('invalid_input')
        module.lock_store(store)
        record = module.read_record(store)
        module.validate_metadata(record.metadata, now=module.utc_now(), provider=provider)
        binding = record.metadata.binding
        if (binding.actor_id, binding.profile_id) != ('1', '1'):
            raise module.Refused('invalid_input')
        module._secret_text(record.secret)
        result = _send(provider, record.secret)
    except module.Refused:
        result = ('metadata', 0, 0)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        result = ('bad_response', 0, 0)
    finally:
        if record is not None:
            record.close()
        if store is not None:
            try:
                store.close()
            except OSError:
                result = ('bad_response', 0, 0)
    status, code, count = result
    try:
        os.write(1, f'provider-smoke status={status} provider={provider} http={code} count={count}\n'.encode('ascii'))
    except OSError:
        return 2
    return 0 if status == 'ok' else 2
