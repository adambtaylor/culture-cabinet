#!/usr/bin/python3
"""One-time private operator tool; never imported by app runtime.

No secret CLI argument, network, subprocess, directory creation or source writes.
The operator separately prepares the credential_store fixed private directories.
"""
import argparse
import configparser
import re
import sys
from pathlib import Path

import yaml

from hydra_key_source import effective_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from shelf import credential_store as store

MAX_SOURCE_BYTES = 1024 * 1024


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError('Invalid credential source')
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def source_key(provider, raw):
    """Pure bounded source selection; original source bytes are never rewritten."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_SOURCE_BYTES:
            raise ValueError
        text = raw.decode('utf-8')
        if provider == 'hydra':
            document = yaml.load(text, Loader=UniqueLoader)
            value = document['main']['apiKey']
            return bytearray(effective_key(value), 'utf-8')
        if provider == 'sab':
            # ConfigObj's preamble is not an INI section. ConfigParser handles
            # duplicate keys/sections strictly after that preamble is excluded.
            match = re.search(r'^\s*\[', text, flags=re.MULTILINE)
            if match is None:
                raise ValueError
            parser = configparser.ConfigParser(interpolation=None, strict=True,
                                               inline_comment_prefixes=('#', ';'))
            parser.optionxform = str
            parser.read_string(text[match.start():])
            if parser.defaults():
                raise ValueError
            # Never select misc.api_key (the full administrative credential).
            value = parser.get('misc', 'nzb_key', raw=True)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not re.fullmatch(r'[a-fA-F0-9]{32}', value):
                raise ValueError
            return bytearray(value, 'ascii')
        raise ValueError
    except (ValueError, TypeError, KeyError, yaml.YAMLError, configparser.Error,
            UnicodeError, RecursionError):
        raise ValueError('Invalid credential source') from None


def provision(*, provider, source, account_id, actor_id, profile_id, expires_at):
    """Internal explicit owner binding and local expiry; enter only, no rotation."""
    context = store._provider_context(provider)
    if provider not in ('hydra', 'sab'):
        raise store.Refused('invalid_input')
    now = store.utc_now()
    metadata = store.EntryMetadata(
        store.Binding(account_id, actor_id, profile_id, provider, context.purpose),
        now, expires_at, expires_at, context.scopes)
    store.validate_metadata(metadata, now=now, provider=provider)
    # Validate binding/expiry before opening even the explicitly chosen source.
    with open(source, 'rb') as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    secret = source_key(provider, raw)
    handle = None
    try:
        handle = store.open_store(provider=provider)
        store.lock_store(handle)
        return store.commit_entry(handle, metadata, secret)
    finally:
        secret[:] = b'\0' * len(secret)
        if handle is not None:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('hydra', 'sab'), required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--actor-id', required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--expires-at', required=True, help='UTC YYYY-MM-DDTHH:MM:SSZ; <=90 days')
    args = parser.parse_args()
    try:
        receipt = provision(provider=args.provider, source=args.source,
                            account_id=args.account_id, actor_id=args.actor_id,
                            profile_id=args.profile_id, expires_at=store.parse_time(args.expires_at))
        if receipt.outcome != 'stored':
            raise store.Refused()
    except Exception:
        print('Private acquisition entry refused', file=sys.stderr)
        return 1
    print('Private acquisition entry stored')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
