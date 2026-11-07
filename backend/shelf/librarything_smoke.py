"""Explicit backend-only read-only smoke. No activation or account identity proof.

The fixed documentation seed is neutral test input, never a user preference.
Response content is discarded: never use it in prompts, embeddings, grounding,
imports or review packets. Clearing mutable buffers is best effort, not erasure.
"""
import json
import os
import resource
import selectors
import subprocess
import time
from urllib.parse import quote_from_bytes

CURL = '/usr/bin/curl'
CA = '/home/tang/Projects/culture-cabinet/.dependencies/provider-ca/certifi-2026.10.19.pem'
ENDPOINT = 'https://www.librarything.com/api/multirecommendations.php'
MAX_BODY = 256 * 1024
MAX_OUTPUT = MAX_BODY + 1029  # Body plus bounded Content-Type and status trailer.
QUOTA_DIRECTORY = '/home/tang/Projects/culture-cabinet/.librarything-smoke'
RECEIPT = 'last-attempt.json'
_DAY = 86400


def _command():
    # Secret URL exists only in stdin config. No curlrc, proxy, netrc, redirects,
    # retries or inherited environment. Do not add verbose/trace/error output.
    return [CURL, '-q', '--config', '-', '--silent', '--proto', '=https',
            '--proto-redir', '=https', '--cacert', CA, '--proxy', '',
            '--noproxy', '*', '--no-netrc', '--max-redirs', '0', '--retry', '0',
            '--connect-timeout', '10', '--max-time', '10', '--max-filesize', str(MAX_BODY),
            '--request', 'GET', '--header', 'Accept: application/json',
            '--write-out', '\n%{http_code}\n%{content_type}']


def _config(secret):
    # Encoding prevents config injection, even for punctuation in accepted keys.
    return bytearray(('url = "' + ENDPOINT + '?v=2&isbns=0300178727&maxItems=5'
                      '&maxPerAuthor=2&showtitles=false&apiKey='
                      + quote_from_bytes(secret, safe='') + '"\n').encode('ascii'))


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _identifier(value):
    return ((type(value) is int and 0 < value < 10**18)
            or (type(value) is str and 0 < len(value) <= 64 and value.isascii() and value.isdigit()))


def _response(returncode, output):
    if returncode:
        status = ('timeout' if returncode == 28 else
                  'tls' if returncode in (35, 51, 58, 60, 77, 80, 82, 83, 90, 91) else
                  'size' if returncode == 63 else 'bad_response')
        return status, 0, 0, 'transport_' + (status if status != 'bad_response' else 'failure'), 'unavailable'
    if len(output) > MAX_OUTPUT:
        return 'size', 0, 0, 'transport_size', 'unavailable'
    parts = output.rsplit(b'\n', 2)
    if len(parts) != 3 or len(parts[1]) != 3 or not parts[1].isdigit():
        return 'bad_response', 0, 0, 'transport_format', 'unavailable'
    body, code, raw_type = parts
    media_type = raw_type.split(b';', 1)[0].strip().lower()
    content_type = ('missing' if not raw_type else
                    'json' if media_type == b'application/json' else
                    'html' if media_type == b'text/html' else 'other')
    if len(body) > MAX_BODY or len(raw_type) > 1024:
        return 'size', 0, 0, 'transport_size', 'unavailable'
    code = int(code)
    if code in (401, 403):
        return 'auth', code, 0, 'http_auth', content_type
    if code == 429:
        return 'rate', code, 0, 'http_rate', content_type
    if code != 200:
        return 'bad_response', code, 0, 'http_status', content_type
    try:
        value = json.loads(body, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError, TypeError):
        return 'bad_response', code, 0, 'json_invalid', content_type
    try:
        if type(value) is not dict:
            raise ValueError()
        if 'errors' in value or 'errorCode' in value:
            if (('errors' in value and 'errorCode' in value)
                    or 'request' in value or 'recommendations' in value):
                return 'bad_response', code, 0, 'envelope_conflict', content_type
            errors = value['errors'] if 'errors' in value else value
            if type(errors) is dict:
                errors = [errors]
            if type(errors) is not list or not 1 <= len(errors) <= 8:
                raise ValueError()
            codes = []
            for error in errors:
                if type(error) is not dict or type(error.get('errorCode')) is not str:
                    raise ValueError()
                codes.append(error['errorCode'])
            if 'aut00001' in codes:
                return 'auth', code, 0, 'api_auth', content_type
            if codes == ['bnf00003']:
                return 'no_recommendations', code, 0, 'api_no_recommendations', content_type
            if codes == ['bnf00005']:
                return 'invalid_seed', code, 0, 'api_invalid_seed', content_type
            return 'bad_response', code, 0, 'api_unknown', content_type
        requests, recommendations = value.get('request'), value.get('recommendations')
        if (type(requests) is not list or len(requests) != 1 or type(requests[0]) is not dict
                or not ('isbn' in requests[0] or 'upc' in requests[0])
                or type(recommendations) is not list or len(recommendations) > 5):
            raise ValueError()
        for item in recommendations:
            if (type(item) is not dict or type(item.get('rank')) is not int or not 1 <= item['rank'] <= 5
                    or not _identifier(item.get('work'))
                    or ('author_id' in item and not (
                        (type(item['author_id']) is str and 0 < len(item['author_id']) <= 256)
                        or _identifier(item['author_id'])))
                    or type(item.get('fromworks')) is not list or len(item['fromworks']) > 5
                    or not all(_identifier(work) for work in item['fromworks'])):
                raise ValueError()
            for field in ('isbns', 'upcs'):
                if (field not in item or (item[field] is not None and (
                        type(item[field]) is not list or len(item[field]) > 100
                        or not all(type(v) is str and len(v) <= 32 for v in item[field])))):
                    raise ValueError()
        return 'ok', code, len(recommendations), 'none', content_type
    except (ValueError, UnicodeError, RecursionError, TypeError):
        return 'bad_response', code, 0, 'shape_invalid', content_type


def _send(secret):
    config = _config(secret)
    output = bytearray()
    child = None
    try:
        child = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env={'LANG': 'C', 'LC_ALL': 'C'}, close_fds=True)
        # Bound parent memory as well as curl's transfer, including unknown-length
        # responses. Nonblocking stdin avoids a hung child blocking the deadline.
        deadline = time.monotonic() + 11
        with selectors.DefaultSelector() as ready:
            os.set_blocking(child.stdin.fileno(), False)
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdin, selectors.EVENT_WRITE)
            ready.register(child.stdout, selectors.EVENT_READ)
            offset = 0
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 'timeout', 0, 0, 'transport_timeout', 'unavailable'
                events = ready.select(remaining)
                if not events:
                    return 'timeout', 0, 0, 'transport_timeout', 'unavailable'
                for key, mask in events:
                    if key.fileobj is child.stdin:
                        offset += os.write(child.stdin.fileno(), memoryview(config)[offset:])
                        if offset == len(config):
                            ready.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        block = os.read(child.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
                        if not block:
                            ready.unregister(child.stdout)
                        else:
                            output.extend(block)
                            if len(output) > MAX_OUTPUT:
                                return 'size', 0, 0, 'transport_size', 'unavailable'
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
        return _response(child.returncode, output)
    except subprocess.TimeoutExpired:
        return 'timeout', 0, 0, 'transport_timeout', 'unavailable'
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'bad_response', 0, 0, 'transport_failure', 'unavailable'
    finally:
        config[:] = b'\0' * len(config)
        output[:] = b'\0' * len(output)
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=1)
            finally:
                child.stdin.close()
                child.stdout.close()


def _quota_check(module, handle):
    # Separate fixed private leaf; never add usage files to the credential leaf.
    for descriptor, expected in handle._directories.items():
        if module._inode(os.fstat(descriptor))[:-1] != expected:
            raise module.Refused()
    for parent, name, child in handle._chain:
        module._no_acl(parent)
        module._no_acl(child)
        if module._inode(os.stat(name, dir_fd=parent, follow_symlinks=False)) != module._inode(os.fstat(child)):
            raise module.Refused()
    module._directory(handle.fd, 1000, 1000, True)
    handle._lock._check()
    if set(module._names(handle)) != {module.LOCK, RECEIPT}:
        raise module.Refused()


def _open_quota(module):
    return module._walk(QUOTA_DIRECTORY.strip('/').split('/'),
                        [(0, 0, False), (1000, 1000, False), (1000, 1000, False),
                         (1000, 1000, False), (1000, 1000, True)], provider='librarything')


def _charge(module):
    """At most one local attempt per rolling 24h, including failed sends.

    This conservative receipt is not provider usage truth: other clients sharing
    the API key are unknown. Missing/corrupt/partial/future receipts fail closed.
    O provisions {"schema":1,"attempted_at":0}; no automatic reset or repair.
    """
    handle = None
    try:
        handle = _open_quota(module)
        module.lock_store(handle)
        _quota_check(module, handle)
        raw, identity = module._read_file(handle, RECEIPT, 128)
        value = module._json(raw)
        module._keys(value, ('schema', 'attempted_at'))
        now = int(time.time())
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['attempted_at']) is not int or not 0 <= value['attempted_at'] <= now
                or not 0 < now < 10**11):
            raise module.Refused()
        if now - value['attempted_at'] < _DAY:
            return False
        raw = json.dumps({'schema': 1, 'attempted_at': now}, separators=(',', ':')).encode('ascii')
        _quota_check(module, handle)
        fd, current = module._open_file(handle, RECEIPT, 128, os.O_RDWR)
        try:
            if current != identity:
                raise module.Refused()
            # No truncate until all checks pass. Interrupted writes leave an
            # invalid or conservative receipt; they never authorize a send.
            os.ftruncate(fd, 0)
            if os.write(fd, raw) != len(raw):
                raise module.Refused()
            module._validate_file(handle, fd, RECEIPT, 128)
            os.fsync(fd)
            os.fsync(handle.fd)
            _quota_check(module, handle)
        finally:
            os.close(fd)
        return True
    finally:
        if handle is not None:
            handle.close()


def run_live(module, provider):
    if provider != 'librarything':
        return 2
    store = record = None
    result = ('metadata', 0, 0, 'metadata', 'unavailable')
    try:
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (1000, 1000, 1000, 1000):
            raise module.Refused()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        context = module._provider_context(provider)
        store = module.open_store(provider=provider)
        if store.context != context or (store.uid, store.gid) != (1000, 1000):
            raise module.Refused()
        module.lock_store(store)
        record = module.read_record(store)
        module.validate_metadata(record.metadata, now=module.utc_now(), provider=provider)
        if record.metadata.binding != module.Binding('unverified', '1', '1', provider, context.purpose):
            raise module.Refused()
        module._secret_text(record.secret)
        result = _send(record.secret) if _charge(module) else ('local_limit', 0, 0, 'local_limit', 'unavailable')
    except module.Refused:
        result = ('metadata', 0, 0, 'metadata', 'unavailable')
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        result = ('bad_response', 0, 0, 'transport_failure', 'unavailable')
    finally:
        if record is not None:
            record.close()
        if store is not None:
            try:
                store.close()
            except OSError:
                result = ('bad_response', 0, 0, 'transport_failure', 'unavailable')
    status, code, count, reason, content_type = result
    try:
        os.write(1, (f'provider-smoke status={status} provider=librarything http={code} count={count}'
                     f' reason={reason} content_type={content_type}\n').encode('ascii'))
    except OSError:
        return 2
    return 0 if status == 'ok' else 2
